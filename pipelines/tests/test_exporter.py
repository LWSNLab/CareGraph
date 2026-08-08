"""Unit tests for the data exporter (CSV / JSON / PostgreSQL upsert script)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from pipelines.load.exporter import DataExporter


@pytest.fixture
def exporter(tmp_path) -> DataExporter:
    return DataExporter(output_dir=tmp_path)


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "name": "AOK PLUS",
                "website": "aok.de/aokplus",
                "zusatzbeitrag": 3.10,
                "geoffnet_in": "Sachsen, Thüringen",
                "is_bundesweit": False,
                "strasse": "Sternplatz 7",
                "plz": "01067",
                "ort": "Dresden",
                "scraping_status": "Success",
            },
            {
                "name": "BARMER",
                "website": "barmer.de",
                "zusatzbeitrag": 3.29,
                "geoffnet_in": "bundesweit",
                "is_bundesweit": True,
                "strasse": "Axel-Springer-Straße 44",
                "plz": "10969",
                "ort": "Berlin",
                "scraping_status": "Success",
            },
        ]
    )


# ------------------------------------------------------------- SQL literals


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Bayern", "'Bayern'"),
        ("  padded  ", "'padded'"),
        ("", "NULL"),
        ("   ", "NULL"),
        (None, "NULL"),
        (float("nan"), "NULL"),
    ],
)
def test_sql_str(value, expected):
    assert DataExporter._sql_str(value) == expected


def test_sql_str_escapes_single_quotes():
    """A name with an apostrophe must not break out of the literal."""
    assert DataExporter._sql_str("BKK O'Neill") == "'BKK O''Neill'"


@pytest.mark.parametrize(
    "value, expected",
    [(3.1, "3.10"), (2, "2.00"), (2.987, "2.99"), (float("nan"), "NULL")],
)
def test_sql_num_matches_numeric_4_2(value, expected):
    assert DataExporter._sql_num(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(True, "TRUE"), (False, "FALSE"), (float("nan"), "NULL")],
)
def test_sql_bool(value, expected):
    assert DataExporter._sql_bool(value) == expected


# ------------------------------------------------------- Bundesland parsing


def test_parses_a_comma_separated_region_list(exporter):
    result = exporter._parse_bundeslaender("Berlin, Brandenburg, Mecklenburg-Vorpommern", False, False)
    assert result == ["Berlin", "Brandenburg", "Mecklenburg-Vorpommern"]


def test_sachsen_anhalt_is_not_swallowed_by_sachsen(exporter):
    """Longest match wins, otherwise 'Sachsen-Anhalt' would map to 'Sachsen'."""
    assert exporter._parse_bundeslaender("Sachsen-Anhalt", False, False) == ["Sachsen-Anhalt"]


def test_region_with_trailing_qualifier_still_maps(exporter):
    """Real data: 'Schleswig-Holstein branchenbezogen'."""
    assert exporter._parse_bundeslaender("Schleswig-Holstein branchenbezogen", False, False) == [
        "Schleswig-Holstein"
    ]


def test_bundesweit_yields_no_links_by_default(exporter):
    """The information already lives in the is_bundesweit flag."""
    assert exporter._parse_bundeslaender("bundesweit", True, False) == []


def test_bundesweit_can_be_expanded_to_all_states(exporter):
    result = exporter._parse_bundeslaender("bundesweit", True, True)
    assert len(result) == 16
    assert result == exporter.BUNDESLAENDER


def test_company_insurers_yield_no_links(exporter):
    company_only = "betriebsbezogen (nur für Mitarbeitende wählbar)"
    assert exporter._parse_bundeslaender(company_only, False, False) == []


def test_duplicates_are_removed(exporter):
    assert exporter._parse_bundeslaender("Bayern, Bayern", False, False) == ["Bayern"]


# --------------------------------------------------------------- CSV / JSON


def test_export_csv_uses_excel_friendly_bom(exporter, df):
    path = exporter.export_csv(df, "k.csv")
    assert path.exists()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_export_csv_omits_the_index(exporter, df):
    path = exporter.export_csv(df, "k.csv")
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("name,")


def test_export_json_keeps_umlauts_readable(exporter, df):
    path = exporter.export_json(df, "k.json")
    records = json.loads(path.read_text(encoding="utf-8"))

    assert len(records) == 2
    assert "Axel-Springer-Straße 44" in path.read_text(encoding="utf-8")
    assert records[0]["name"] == "AOK PLUS"


def test_output_directory_is_created(tmp_path, df):
    target = tmp_path / "deep" / "nested"
    DataExporter(output_dir=target).export_json(df, "k.json")
    assert (target / "k.json").exists()


# ---------------------------------------------------------------------- SQL


def sql_for(exporter, df, **kwargs) -> str:
    return exporter.export_sql(df, filename="k.sql", **kwargs).read_text(encoding="utf-8")


def test_sql_uses_identity_not_serial(exporter, df):
    """SERIAL is legacy; identity columns are the modern Postgres form."""
    sql = sql_for(exporter, df)
    assert "generated always as identity" in sql
    assert "serial" not in sql.lower()


def test_sql_is_an_idempotent_upsert(exporter, df):
    sql = sql_for(exporter, df)
    assert "on conflict (name) do update set" in sql
    assert "updated_at = now()" in sql
    assert "name            text not null unique" in sql


def test_sql_emits_one_bulk_insert(exporter, df):
    """A single statement keeps the load atomic."""
    assert sql_for(exporter, df).count("insert into krankenkassen (") == 1


def test_sql_escapes_and_nulls_correctly(exporter):
    df = pd.DataFrame([{
        "name": "BKK O'Neill", "website": None, "zusatzbeitrag": float("nan"),
        "geoffnet_in": "bundesweit", "is_bundesweit": True,
        "strasse": "", "plz": None, "ort": None, "scraping_status": "Failed / Manual Check",
    }])
    sql = sql_for(exporter, df)

    assert "'BKK O''Neill'" in sql
    assert "NULL" in sql


def test_sql_keeps_leading_zero_postcodes_as_text(exporter, df):
    """PLZ 01067 must not degrade to 1067 — hence text, not integer."""
    sql = sql_for(exporter, df)
    assert "'01067'" in sql
    assert "plz             text" in sql


def test_sql_normalises_states_into_a_junction_table(exporter, df):
    sql = sql_for(exporter, df)

    assert "create table if not exists bundeslaender" in sql
    assert "create table if not exists krankenkasse_bundesland" in sql
    assert "references krankenkassen(id) on delete cascade" in sql
    assert "('AOK PLUS', 'Sachsen')" in sql
    assert "('AOK PLUS', 'Thüringen')" in sql


def test_sql_seeds_all_sixteen_states(exporter, df):
    sql = sql_for(exporter, df)
    for state in exporter.BUNDESLAENDER:
        assert f"('{state}')" in sql
    assert "on conflict (name) do nothing" in sql


def test_sql_rebuilds_the_junction_so_removals_are_applied(exporter, df):
    assert "truncate table krankenkasse_bundesland" in sql_for(exporter, df)


def test_sql_can_skip_normalisation(exporter, df):
    sql = sql_for(exporter, df, normalize_states=False)
    assert "bundeslaender" not in sql
    assert "insert into krankenkassen (" in sql


def test_sql_enables_row_level_security_on_every_table(exporter, df):
    sql = sql_for(exporter, df)
    for table in ("krankenkassen", "bundeslaender", "krankenkasse_bundesland"):
        assert f"alter table {table} enable row level security;" in sql
    # policy creation must be re-runnable
    assert sql.count('drop policy if exists "Public read access"') == 3


def test_sql_rls_can_be_disabled(exporter, df):
    assert "row level security" not in sql_for(exporter, df, enable_rls=False)


def junction_section(sql: str) -> str:
    """Only the state-link INSERT — the main INSERT also contains insurer names."""
    marker = "insert into krankenkasse_bundesland"
    return sql[sql.index(marker):] if marker in sql else ""


def test_bundesweit_insurer_gets_no_junction_rows_by_default(exporter, df):
    """BARMER is nationwide; it carries the flag instead of 16 link rows."""
    assert "('BARMER'," not in junction_section(sql_for(exporter, df))


def test_expand_bundesweit_links_all_states(exporter, df):
    sql = sql_for(exporter, df, expand_bundesweit=True)
    assert "('BARMER', 'Bayern')" in sql
    assert "('BARMER', 'Thüringen')" in sql
