"""Tests for the PostGIS loader (story E1-S4).

Split in two: the parameter mapping and SQL shape are pure and always run;
the integration tests need a real PostGIS database and skip without one.

    docker compose up -d db
    CAREGRAPH_TEST_DSN=postgres://caregraph:caregraph@localhost:5433/caregraph \\
        uv run --project pipelines pytest pipelines/tests/test_postgres_loader.py
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from pipelines.load.postgres_loader import LoadReport, PostgresLoader
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
