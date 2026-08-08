"""Unit tests for the GKV insurer-list parser (story E1-S1).

The PDF has no table grid lines and entries wrap across visual lines, so the
parser reconstructs columns from word coordinates. These tests drive that logic
with synthetic word boxes — no PDF needed — plus one integration test against
the real list when it is present.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pipelines.parsers.gkv_parser import GKVParser

REAL_PDF = Path("pipelines/data/raw/gkv_liste_2026.pdf")

# x-positions of the four columns in the official layout
X_NAME, X_WEB, X_BEITRAG, X_REGION = 38.0, 165.1, 286.6, 373.6


def word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "top": top}


def header_words(top: float = 201.0) -> list[dict]:
    return [
        word("Krankenkassenname", X_NAME, top),
        word("Homepage", X_WEB, top),
        word("Zusatzbeitrag", X_BEITRAG, top),
        word("geöffnet", X_REGION, top),
    ]


class FakePage:
    """Minimal stand-in for a pdfplumber page."""

    def __init__(self, words: list[dict]):
        self._words = words

    def extract_words(self) -> list[dict]:
        return self._words


def parser() -> GKVParser:
    return GKVParser("dummy.pdf")


# ------------------------------------------------------------- column edges


def test_column_edges_are_derived_from_the_header_row():
    assert parser()._column_edges(header_words()) == [X_NAME, X_WEB, X_BEITRAG, X_REGION]


def test_column_edges_fall_back_when_header_is_missing():
    """A layout change must degrade to known positions, not crash."""
    edges = parser()._column_edges([word("Irgendwas", 10.0, 5.0)])
    assert edges == [38.0, 165.1, 286.6, 373.6]


def test_column_edges_fall_back_on_partial_header():
    partial = header_words()[:2]
    assert parser()._column_edges(partial) == [38.0, 165.1, 286.6, 373.6]


# -------------------------------------------------------------- page parsing


def test_parses_a_single_entry_into_four_columns():
    page = FakePage(
        header_words()
        + [
            word("AOK", X_NAME, 221.0),
            word("www.aok.de/bayern", X_WEB, 221.0),
            word("2,69", X_BEITRAG, 221.0),
            word("%", 306.0, 221.0),
            word("Bayern", X_REGION, 221.0),
        ]
    )
    rows = parser()._parse_page(page)

    assert rows == [["AOK", "www.aok.de/bayern", "2,69 %", "Bayern"]]


def test_wrapped_lines_are_appended_to_the_entry_above():
    """A line without a contribution value continues the previous entry."""
    page = FakePage(
        header_words()
        + [
            word("AOK", X_NAME, 221.0),
            word("www.aok.de/nordost", X_WEB, 221.0),
            word("3,50", X_BEITRAG, 221.0),
            word("Berlin,", X_REGION, 221.0),
            # wrapped continuation on the next visual line
            word("Nordost", X_NAME, 233.0),
            word("Brandenburg", X_REGION, 233.0),
        ]
    )
    rows = parser()._parse_page(page)

    assert len(rows) == 1
    assert rows[0][0] == "AOK Nordost"
    assert rows[0][3] == "Berlin, Brandenburg"


def test_header_and_page_furniture_are_skipped():
    page = FakePage(
        header_words()
        + [
            word("Krankenkassenliste", X_NAME, 170.0),
            word("Seite", X_NAME, 800.0),
            word("1", 60.0, 800.0),
            word("Echte", X_NAME, 221.0),
            word("kasse.de", X_WEB, 221.0),
            word("2,00", X_BEITRAG, 221.0),
            word("Bayern", X_REGION, 221.0),
        ]
    )
    rows = parser()._parse_page(page)

    assert len(rows) == 1
    assert rows[0][0] == "Echte"


def test_page_without_words_yields_no_rows():
    assert parser()._parse_page(FakePage([])) == []


def test_continuation_before_any_entry_is_dropped_not_crashing():
    """Leading orphan lines must not raise (no previous row to attach to)."""
    page = FakePage(header_words() + [word("Waise", X_NAME, 221.0)])
    assert parser()._parse_page(page) == []


# --------------------------------------------------------------- cleaning


def clean(rows: list[list[str]]) -> pd.DataFrame:
    p = parser()
    p.raw_df = pd.DataFrame(rows, columns=[0, 1, 2, 3])
    return p._clean_data()


def test_contribution_rate_becomes_a_float():
    df = clean([["Kasse", "k.de", "2,98 %", "Bayern"]])
    assert df.loc[0, "zusatzbeitrag"] == pytest.approx(2.98)
    assert df["zusatzbeitrag"].dtype == "float64"


def test_bundesweit_flag_is_derived_from_the_region_text():
    df = clean([
        ["A", "a.de", "2,00 %", "bundesweit"],
        ["B", "b.de", "2,00 %", "Bayern"],
    ])
    assert list(df["is_bundesweit"]) == [True, False]


def test_website_is_normalised():
    df = clean([["Kasse", "HTTPS://WWW.Kasse.de/", "2,00 %", "Bayern"]])
    assert df.loc[0, "website"] == "kasse.de"


def test_website_line_break_is_removed():
    """Regression: wrapped URLs used to keep the space from the line break."""
    df = clean([["AOK", "www.aok.de/baden- wuerttemberg/index.php", "2,99 %", "BW"]])
    assert df.loc[0, "website"] == "aok.de/baden-wuerttemberg/index.php"


def test_wrapped_hyphen_in_region_is_rejoined():
    """Regression: 'Mecklenburg- Vorpommern' came from a line break."""
    df = clean([["Kasse", "k.de", "2,00 %", "Mecklenburg- Vorpommern"]])
    assert df.loc[0, "geoffnet_in"] == "Mecklenburg-Vorpommern"


def test_name_separator_with_spaces_around_hyphen_is_preserved():
    """'AOK - Die Gesundheitskasse' must NOT be glued together."""
    df = clean([["AOK - Die Gesundheitskasse", "k.de", "2,00 %", "Bayern"]])
    assert df.loc[0, "name"] == "AOK - Die Gesundheitskasse"


def test_insurer_named_krankenkasse_is_not_filtered_out():
    """Regression: a broad filter used to delete VIACTIV, BERGISCHE etc."""
    df = clean([
        ["VIACTIV Krankenkasse", "viactiv.de", "4,19 %", "bundesweit"],
        ["BERGISCHE KRANKENKASSE", "b.de", "3,79 %", "Hessen"],
    ])
    assert len(df) == 2


def test_repeated_header_rows_are_removed():
    df = clean([
        ["Krankenkassenname", "Homepage", "Zusatzbeitrag", "geöffnet in"],
        ["Echte Kasse", "k.de", "2,00 %", "Bayern"],
    ])
    assert list(df["name"]) == ["Echte Kasse"]


def test_output_has_the_expected_columns():
    df = clean([["Kasse", "k.de", "2,00 %", "Bayern"]])
    assert list(df.columns) == ["name", "website", "zusatzbeitrag", "geoffnet_in", "is_bundesweit"]


def test_too_few_columns_raises():
    p = parser()
    p.raw_df = pd.DataFrame([["a", "b"]], columns=[0, 1])
    with pytest.raises(ValueError, match="Spaltenanzahl"):
        p._clean_data()


# ------------------------------------------------------------- integration


@pytest.mark.skipif(not REAL_PDF.exists(), reason="official GKV PDF not present")
def test_parses_the_real_gkv_list_completely():
    """End-to-end against the official list: every insurer must be captured."""
    import re

    import pdfplumber

    with pdfplumber.open(REAL_PDF) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    expected = len(re.findall(r"\d,\d\d\s*%", text))

    df = GKVParser(REAL_PDF, output_filename=REAL_PDF.name).parse_pdf()

    assert len(df) == expected
    assert df["zusatzbeitrag"].notna().all()
    assert (df["name"].str.len() > 0).all()
    assert (df["website"].str.len() > 0).all()
