"""Tests for the PostGIS loader (story E1-S4).

Split in two: the parameter mapping and SQL shape are pure and always run;
the integration tests need a real PostGIS database and skip without one.

    docker compose up -d db
    CAREGRAPH_TEST_DSN=postgres://caregraph:caregraph@localhost:5433/caregraph \\
        uv run --project pipelines pytest pipelines/tests/test_postgres_loader.py
"""

from __future__ import annotations

import os
import re
from datetime import date

import pytest

from pipelines.load.postgres_loader import (
    _MAX_PROVIDER_BATCH_SIZE,
    _PROVIDER_VALUE_COLUMNS,
    _UPSERT_COLUMNS,
    LoadReport,
    PostgresLoader,
)
from pipelines.scrapers.osm_provider_scraper import ProviderRecord

params = PostgresLoader._provider_params


def record(**kwargs) -> ProviderRecord:
    base = dict(
        type="pflegeheim_stationaer",
        name="Testheim",
        details={"osm_type": "node", "osm_id": 1, "source": "openstreetmap"},
    )
    base.update(kwargs)
    return ProviderRecord(**base)


# ---------------------------------------------------------------- parameters


def test_maps_a_provider_to_query_parameters():
    p = params(record(strasse="Teststr 1", plz="28195", ort="Bremen",
                      bundesland="Bremen", lat=53.07, lon=8.79))

    assert p["source_id"] == "osm:node/1"
    assert p["type"] == "pflegeheim_stationaer"
    assert p["plz"] == "28195"
    assert (p["lat"], p["lon"]) == (53.07, 8.79)


def test_details_are_serialised_as_json():
    p = params(record())
    assert isinstance(p["details"], str)
    assert "openstreetmap" in p["details"]


def test_provenance_becomes_the_scraping_status():
    assert params(record())["scraping_status"] == "openstreetmap"


def test_records_without_a_name_are_rejected():
    assert params(record(name="")) is None
    assert params(record(name="   ")) is None


def test_records_without_a_usable_source_id_are_rejected():
    """A half-formed key like 'osm:None/None' must not reach the database."""
    assert params(record(details={"source": "openstreetmap"})) is None


def test_missing_address_is_carried_through_as_null():
    """30% of real records have no address; they must still load."""
    p = params(record(lat=1.0, lon=2.0))
    assert p["plz"] is None and p["ort"] is None and p["strasse"] is None


def test_missing_coordinates_are_carried_through_as_null():
    p = params(record())
    assert p["lat"] is None and p["lon"] is None


def test_accepts_plain_dicts_as_well_as_dataclasses():
    """run_load reads JSON, so dicts must map identically."""
    p = params({
        "type": "pflegedienst_ambulant", "name": "Aus JSON",
        "details": {"osm_type": "way", "osm_id": 7, "source": "openstreetmap"},
        "lat": 1.0, "lon": 2.0,
    })
    assert p["source_id"] == "osm:way/7"
    assert p["name"] == "Aus JSON"


# ----------------------------------------------------------------- SQL shape


def test_upsert_is_keyed_on_source_id():
    sql = PostgresLoader._upsert_sql()
    assert "ON CONFLICT (source_id) DO UPDATE" in sql
    assert "updated_at = now()" in sql


def test_upsert_does_not_overwrite_created_at_or_the_key():
    sql = PostgresLoader._upsert_sql()
    assert "created_at = EXCLUDED" not in sql
    assert "source_id = EXCLUDED" not in sql


def test_coordinates_are_cast_so_all_null_rows_work():
    """Regression: insurers have no coordinates, and an untyped NULL made
    Postgres fail with 'could not determine data type of parameter'."""
    sql = PostgresLoader._upsert_sql()
    assert "::double precision" in sql


def test_location_is_built_by_postgis_not_in_python():
    assert "ST_SetSRID(" in PostgresLoader._upsert_sql()


def test_provider_batch_sql_contains_multiple_rows_and_one_returning():
    values = ", ".join(PostgresLoader._provider_values_sql(f"_{i}") for i in range(2))
    sql = PostgresLoader._upsert_sql(values)

    assert sql.count("INSERT INTO care_infrastructure") == 1
    assert sql.count("RETURNING id") == 1
    assert "%(source_id_0)s" in sql
    assert "%(source_id_1)s" in sql


def _values_shape(sql: str) -> tuple[int, list[int]]:
    """Column count, and the expression count of each top-level VALUES tuple."""
    columns = re.search(
        r"INSERT INTO care_infrastructure \((.*?)\)\s*VALUES", sql, re.S
    ).group(1)
    body = re.sub(r"--[^\n]*", "", sql.split("VALUES", 1)[1].split("ON CONFLICT", 1)[0])

    depth, expressions, current = 0, [], 0
    for char in body:
        if char == "(":
            depth += 1
            if depth == 1:
                current = 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                expressions.append(current)
        elif char == "," and depth == 1:
            current += 1
    return len([c for c in columns.split(",") if c.strip()]), expressions


# The substring test above passed while the SQL was unusable: every tuple carries
# its own parentheses, and the VALUES keyword wrapped them in another pair, so
# Postgres read the whole list as one row expression. Only an integration test
# with a live database caught it. This counts the shape instead, which needs no
# database and fails on the same mistake.
@pytest.mark.parametrize("rows", [1, 2, 500])
def test_values_tuples_match_the_insert_column_count(rows):
    if rows == 1:
        sql = PostgresLoader._upsert_sql()
    else:
        sql = PostgresLoader._upsert_sql(
            ", ".join(PostgresLoader._provider_values_sql(f"_{i}") for i in range(rows))
        )

    columns, expressions = _values_shape(sql)
    assert len(expressions) == rows, "one tuple per row"
    assert set(expressions) == {columns}, (
        f"every tuple must hold {columns} expressions, got {sorted(set(expressions))}"
    )


def test_provider_batch_limit_matches_postgres_parameter_limit():
    # Derived rather than written out: adding a column lowers the real ceiling,
    # and a hardcoded number would stay above it and keep passing without ever
    # testing the new one.
    with pytest.raises(ValueError, match="at most"):
        PostgresLoader("unused").load_providers([], batch_size=_MAX_PROVIDER_BATCH_SIZE + 1)


def test_provider_value_columns_are_derived_from_upsert_columns():
    assert _PROVIDER_VALUE_COLUMNS == ("source_id", *_UPSERT_COLUMNS)


# -------------------------------------------------------------------- report


def test_report_is_ok_only_without_skips():
    report = LoadReport(inserted=3)
    assert report.ok
    report.skipped.append("broken")
    assert not report.ok
    assert "skipped=1" in report.summary()


# --------------------------------------------------------------- integration

DSN = os.environ.get("CAREGRAPH_TEST_DSN")
integration = pytest.mark.skipif(not DSN, reason="CAREGRAPH_TEST_DSN not set")


@pytest.fixture
def loader():
    return PostgresLoader(DSN)


@pytest.fixture(autouse=True)
def clean_table():
    """Remove only this test's rows, so a developer's local data survives."""
    if not DSN:
        yield
        return
    import psycopg

    def purge():
        with psycopg.connect(DSN) as conn:
            conn.execute("DELETE FROM care_infrastructure WHERE source_id LIKE 'test:%'")
            conn.execute("DELETE FROM care_infrastructure WHERE name LIKE 'PyTest %'")
            conn.commit()

    purge()
    yield
    purge()


def provider(source_id: str, **kwargs) -> dict:
    osm_type, osm_id = source_id.split(":")[1].split("/")
    base = dict(
        type="pflegeheim_stationaer", name="PyTest Heim",
        details={"osm_type": osm_type, "osm_id": osm_id, "source": "test"},
        lat=53.07, lon=8.79,
    )
    base.update(kwargs)
    return base


@integration
def test_load_inserts_then_updates(loader):
    """The core promise: a second run must not duplicate."""
    rows = [provider("osm:test/1"), provider("osm:test/2")]

    first = loader.load_providers(rows)
    assert (first.inserted, first.updated) == (2, 0)

    second = loader.load_providers(rows)
    assert (second.inserted, second.updated) == (0, 2)


@integration
def test_load_writes_a_queryable_postgis_point(loader):
    import psycopg

    loader.load_providers([provider("osm:test/3", lat=53.0758, lon=8.8072)])
    with psycopg.connect(DSN) as conn:
        found = conn.execute(
            """
            SELECT name FROM care_infrastructure
            WHERE ST_DWithin(location, ST_MakePoint(8.8072, 53.0758)::geography, 100)
              AND source_id = 'osm:test/3'
            """
        ).fetchone()
    assert found is not None


@integration
def test_record_without_coordinates_loads(loader):
    """Insurers have no coordinates — an all-NULL point must not break."""
    report = loader.load_providers([provider("osm:test/4", lat=None, lon=None)])
    assert report.inserted == 1 and report.ok


@integration
def test_insurer_load_links_states_and_appends_history(loader):
    insurer = {
        "name": "PyTest Kasse", "website": "x.de", "zusatzbeitrag": 2.5,
        "geoffnet_in": "Bayern, Hessen", "is_bundesweit": False,
        "bundeslaender": ["Bayern", "Hessen"],
    }
    report = loader.load_insurers([insurer], gueltig_ab=date(2026, 1, 1))

    assert report.inserted == 1
    assert report.state_links == 2
    assert report.history_rows == 1


@integration
def test_reloading_the_same_publication_does_not_duplicate_history(loader):
    insurer = {"name": "PyTest Kasse", "zusatzbeitrag": 2.5, "geoffnet_in": "Bayern",
               "is_bundesweit": False, "bundeslaender": ["Bayern"]}

    loader.load_insurers([insurer], gueltig_ab=date(2026, 1, 1))
    again = loader.load_insurers([insurer], gueltig_ab=date(2026, 1, 1))

    assert again.history_rows == 0, "history must be append-only per publication"


@integration
def test_a_new_publication_appends_instead_of_overwriting(loader):
    """The whole point of the history table: last year's rate survives."""
    import psycopg

    base = {"name": "PyTest Kasse", "geoffnet_in": "Bayern", "is_bundesweit": False,
            "bundeslaender": ["Bayern"]}
    loader.load_insurers([{**base, "zusatzbeitrag": 2.5}], gueltig_ab=date(2026, 1, 1))
    loader.load_insurers([{**base, "zusatzbeitrag": 3.1}], gueltig_ab=date(2027, 1, 1))

    with psycopg.connect(DSN) as conn:
        rates = conn.execute(
            """
            SELECT h.zusatzbeitrag FROM zusatzbeitrag_historie h
            JOIN care_infrastructure k ON k.id = h.krankenkasse_id
            WHERE k.name = 'PyTest Kasse' ORDER BY h.gueltig_ab
            """
        ).fetchall()
        current = conn.execute(
            """
            SELECT a.zusatzbeitrag FROM zusatzbeitrag_aktuell a
            JOIN care_infrastructure k ON k.id = a.krankenkasse_id
            WHERE k.name = 'PyTest Kasse'
            """
        ).fetchone()

    assert [float(r[0]) for r in rates] == [2.5, 3.1]
    assert float(current[0]) == 3.1


@integration
def test_removed_state_coverage_is_applied_on_reload(loader):
    """An insurer leaving a state must lose the link, not keep it forever."""
    import psycopg

    base = {"name": "PyTest Kasse", "zusatzbeitrag": 2.0, "is_bundesweit": False}
    loader.load_insurers([{**base, "geoffnet_in": "Bayern, Hessen",
                           "bundeslaender": ["Bayern", "Hessen"]}], gueltig_ab=date(2026, 1, 1))
    loader.load_insurers([{**base, "geoffnet_in": "Bayern",
                           "bundeslaender": ["Bayern"]}], gueltig_ab=date(2026, 1, 1))

    with psycopg.connect(DSN) as conn:
        states = conn.execute(
            """
            SELECT b.name FROM krankenkasse_bundesland kb
            JOIN bundeslaender b ON b.id = kb.bundesland_id
            JOIN care_infrastructure k ON k.id = kb.krankenkasse_id
            WHERE k.name = 'PyTest Kasse'
            """
        ).fetchall()

    assert [s[0] for s in states] == ["Bayern"]


# ------------------------------------------------- IK enrichment regressions
#
# All of these reproduce a defect observed on 2026-08-10: a run whose IK
# enrichment came back thinner than the database silently added duplicate
# insurers (91 in one run, 16 in another) and still exited 0.


def _insurer_rows(name: str = "PyTest IK Kasse"):
    import psycopg

    with psycopg.connect(DSN) as conn:
        return conn.execute(
            "SELECT source_id, ik_nummer FROM care_infrastructure"
            " WHERE type = 'krankenkasse' AND name = %s ORDER BY source_id",
            (name,),
        ).fetchall()


@integration
def test_insurer_without_ik_does_not_duplicate_an_ik_keyed_row(loader):
    """The core defect: a failed IK lookup must not create a second row."""
    base = {"name": "PyTest IK Kasse", "zusatzbeitrag": 1.5,
            "geoffnet_in": "Bayern", "bundeslaender": ["Bayern"]}

    first = loader.load_insurers(
        [{**base, "ik_nummer": "999999801"}], gueltig_ab=date(2026, 1, 1))
    assert (first.inserted, first.updated) == (1, 0)
    assert _insurer_rows() == [("ik:999999801", "999999801")]

    # Same insurer, enrichment failed this time — no IK on the record.
    second = loader.load_insurers([base], gueltig_ab=date(2026, 1, 1))

    assert (second.inserted, second.updated) == (0, 1), "a duplicate row was inserted"
    assert second.key_preserved == 1, "the degraded run was not reported"
    # The key stays, and the previously resolved IK is not erased.
    assert _insurer_rows() == [("ik:999999801", "999999801")]


@integration
def test_ik_is_never_overwritten_with_null(loader):
    """`ik_nummer = EXCLUDED.ik_nummer` would blank a resolved IK.

    Invisible until the key stopped flapping: before, a run without an IK
    inserted a new row instead of updating the existing one.
    """
    base = {"name": "PyTest IK Kasse", "zusatzbeitrag": 1.5,
            "geoffnet_in": "Bayern", "bundeslaender": ["Bayern"]}

    loader.load_insurers([{**base, "ik_nummer": "999999802"}], gueltig_ab=date(2026, 1, 1))
    loader.load_insurers([base], gueltig_ab=date(2026, 1, 1))

    rows = _insurer_rows()
    assert len(rows) == 1
    assert rows[0][1] == "999999802", "a failed enrichment erased the stored IK"


@integration
def test_name_key_is_upgraded_once_an_ik_appears(loader):
    """The intended direction still works: name key → IK key, in place."""
    base = {"name": "PyTest IK Kasse", "zusatzbeitrag": 1.5,
            "geoffnet_in": "Bayern", "bundeslaender": ["Bayern"]}

    loader.load_insurers([base], gueltig_ab=date(2026, 1, 1))
    assert _insurer_rows() == [("gkv:PyTest IK Kasse", None)]

    report = loader.load_insurers(
        [{**base, "ik_nummer": "999999803"}], gueltig_ab=date(2026, 1, 1))

    assert report.rekeyed == 1
    assert (report.inserted, report.updated) == (0, 1)
    assert _insurer_rows() == [("ik:999999803", "999999803")]


@integration
def test_a_corrected_ik_moves_the_row_rather_than_adding_one(loader):
    """The official list does revise IKs between publications."""
    base = {"name": "PyTest IK Kasse", "zusatzbeitrag": 1.5,
            "geoffnet_in": "Bayern", "bundeslaender": ["Bayern"]}

    loader.load_insurers([{**base, "ik_nummer": "999999804"}], gueltig_ab=date(2026, 1, 1))
    loader.load_insurers([{**base, "ik_nummer": "999999805"}], gueltig_ab=date(2026, 1, 1))

    assert _insurer_rows() == [("ik:999999805", "999999805")]


@integration
def test_history_stays_attached_to_the_same_row(loader):
    """A duplicated insurer splits its own time series — check it does not."""
    import psycopg

    base = {"name": "PyTest IK Kasse", "geoffnet_in": "Bayern",
            "bundeslaender": ["Bayern"]}

    loader.load_insurers(
        [{**base, "ik_nummer": "999999806", "zusatzbeitrag": 1.5}],
        gueltig_ab=date(2026, 1, 1))
    loader.load_insurers(
        [{**base, "zusatzbeitrag": 1.9}], gueltig_ab=date(2027, 1, 1))

    with psycopg.connect(DSN) as conn:
        rates = conn.execute(
            """
            SELECT h.gueltig_ab, h.zusatzbeitrag
              FROM zusatzbeitrag_historie h
              JOIN care_infrastructure c ON c.id = h.krankenkasse_id
             WHERE c.name = 'PyTest IK Kasse'
             ORDER BY h.gueltig_ab
            """
        ).fetchall()

    assert [str(r[0]) for r in rates] == ["2026-01-01", "2027-01-01"], (
        "the time series was split across duplicate rows"
    )


@integration
def test_count_insurers_with_ik_is_the_regression_baseline(loader):
    before = loader.count_insurers_with_ik()
    loader.load_insurers(
        [{"name": "PyTest IK Kasse", "ik_nummer": "999999807", "zusatzbeitrag": 1.0,
          "geoffnet_in": "Bayern", "bundeslaender": ["Bayern"]}],
        gueltig_ab=date(2026, 1, 1))
    assert loader.count_insurers_with_ik() == before + 1
