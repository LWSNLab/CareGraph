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
    """End-to-end against the official list: every insurer must be captured.

    The expected count is derived from the **homepage** column, not from the
    contribution rate. Counting `\\d,\\d\\d %` was the original approach and it
    hid a defect for months: it used the very signal the parser used to detect
    an entry, so the two agreed that an insurer is by definition something with
    a numeric rate. The SVLFG has none, its row was folded into the one above,
    and this test still passed.
    """
    import re

    import pdfplumber

    with pdfplumber.open(REAL_PDF) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Every insurer lists exactly one homepage.
    expected = len(re.findall(r"www\.", text))
    # Cross-check from the other direction: numeric rates plus the ones stated
    # in words must add up to the same number.
    numeric = len(re.findall(r"\d,\d\d\s*%", text))
    stated_in_words = len(re.findall(r"wird\s+nicht\s+erhoben", text))
    assert numeric + stated_in_words == expected, (
        f"{numeric} numeric + {stated_in_words} textual rates != {expected} homepages"
    )

    df = GKVParser(REAL_PDF, output_filename=REAL_PDF.name).parse_pdf()

    assert len(df) == expected
    assert (df["name"].str.len() > 0).all()
    assert (df["website"].str.len() > 0).all()
    # One insurer levies no Zusatzbeitrag; the rest must all have one.
    assert df["zusatzbeitrag"].notna().sum() == numeric
    assert df["zusatzbeitrag"].isna().sum() == stated_in_words


# ------------------------------------------- non-numeric Zusatzbeitrag (SVLFG)
#
# Regression tests for a defect found 2026-08-10: two insurers were merged into
# one row for months. The old rule treated "a line whose Zusatzbeitrag column
# contains a digit" as the start of an entry. The SVLFG levies none — its cell
# reads "wird nicht erhoben" — so its line looked like a continuation and was
# folded into the entry above, concatenating both names and both URLs.


def svlfg_page() -> FakePage:
    """The real layout of page 4 around SKD BKK and the SVLFG.

    Line-for-line as pdfplumber reports it, including the wrapped region lines
    of SKD BKK and the wrapped name lines of the SVLFG.
    """
    return FakePage(
        header_words()
        + [
            # SKD BKK — a normal entry with a numeric rate
            word("SKD", X_NAME, 447.0), word("BKK", X_NAME + 22, 447.0),
            word("www.skd-bkk.de", X_WEB, 447.0),
            word("2,98", X_BEITRAG, 447.0), word("%", X_BEITRAG + 22, 447.0),
            word("Baden-Württemberg,", X_REGION, 447.0),
            # …its region wraps over two lines (name column empty)
            word("Hamburg,", X_REGION, 459.0), word("Hessen", X_REGION + 40, 459.0),
            word("Schleswig-Holstein", X_REGION, 471.0),
            # SVLFG — a new entry whose rate is text, not a number
            word("Sozialversicherung", X_NAME, 501.0), word("für", X_NAME + 90, 501.0),
            word("www.svlfg.de", X_WEB, 501.0),
            word("wird", X_BEITRAG, 501.0), word("nicht", X_BEITRAG + 20, 501.0),
            word("erhoben", X_BEITRAG + 44, 501.0),
            word("branchenbezogen", X_REGION, 501.0),
            # …its name wraps, starting at the *same* x as a new entry
            word("Landwirtschaft,", X_NAME, 513.0), word("Forsten", X_NAME + 70, 513.0),
            word("Gartenbau", X_NAME, 525.0), word("(SVLFG)", X_NAME + 50, 525.0),
        ]
    )


def test_entry_with_a_non_numeric_rate_is_not_folded_into_the_one_above():
    rows = parser()._parse_page(svlfg_page())

    assert len(rows) == 2, f"expected two entries, got {len(rows)}: {rows}"
    assert rows[0][0] == "SKD BKK"
    assert rows[0][1] == "www.skd-bkk.de"
    assert rows[1][0] == "Sozialversicherung für Landwirtschaft, Forsten Gartenbau (SVLFG)"
    assert rows[1][1] == "www.svlfg.de"
    # The tell-tale symptom: two URLs glued together.
    assert "www" not in rows[0][1].removeprefix("www.")
    assert rows[1][2] == "wird nicht erhoben"


def test_wrapped_region_still_attaches_to_the_entry_above():
    """The fix must not break the continuation handling it replaces."""
    rows = parser()._parse_page(svlfg_page())
    assert "Hamburg" in rows[0][3] and "Schleswig-Holstein" in rows[0][3]
    # …and must not leak into the next entry.
    assert "Hamburg" not in rows[1][3]


def test_a_non_numeric_rate_becomes_nan_not_zero():
    """No Zusatzbeitrag is missing data, not a rate of 0.00 %."""
    p = parser()
    p.raw_df = pd.DataFrame(
        [["SVLFG", "www.svlfg.de", "wird nicht erhoben", "branchenbezogen"]],
        columns=[0, 1, 2, 3],
    )
    out = p._clean_data()
    assert pd.isna(out.iloc[0]["zusatzbeitrag"])


def test_starts_entry_needs_both_name_and_rate():
    p = parser()
    assert p._starts_entry(["SKD BKK", "www.skd-bkk.de", "2,98 %", "Bayern"])
    assert p._starts_entry(["SVLFG", "www.svlfg.de", "wird nicht erhoben", ""])
    # A wrapped name line: same x-position as a new entry, but no rate.
    assert not p._starts_entry(["Gartenbau (SVLFG)", "", "", ""])
    # A wrapped region line.
    assert not p._starts_entry(["", "", "", "Hamburg, Hessen"])
    # A wrapped URL line.
    assert not p._starts_entry(["", "kasse.de/pfad", "", ""])
    assert not p._starts_entry(["", "", "", ""])


def test_merge_artefacts_are_warned_about(caplog):
    """The guard that would have caught this on day one."""
    p = parser()
    p.raw_df = pd.DataFrame(
        [["SKD BKK Sozialversicherung für Landwirtschaft, Forsten und Gartenbau (SVLFG)",
          "www.skd-bkk.dewww.svlfg.de", "2,98 %", "Bayern"]],
        columns=[0, 1, 2, 3],
    )
    with caplog.at_level("WARNING"):
        p._clean_data()

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "concatenated" in messages, messages
    assert "merged row" in messages, messages


@pytest.mark.skipif(not REAL_PDF.exists(), reason="official PDF not present")
def test_real_list_has_no_merge_artefacts():
    df = GKVParser(REAL_PDF).parse_pdf()

    # 93, not 92: the SVLFG is its own insurer.
    assert len(df) == 93, f"expected 93 insurers, got {len(df)}"

    glued = df[df["website"].str.contains("www.", regex=False, na=False)]
    assert glued.empty, f"concatenated URLs remain: {list(glued['website'])}"

    longest = df["name"].str.len().max()
    assert longest <= 80, f"suspiciously long name ({longest} chars)"

    names = set(df["name"])
    assert "SKD BKK" in names
    assert "Sozialversicherung für Landwirtschaft, Forsten und Gartenbau (SVLFG)" in names


# --------------------------------------------------- download_if_url side effects


def test_a_local_path_creates_no_download_directory(tmp_path):
    """Parsing a file that is already on disk must not touch the filesystem.

    `download_if_url` used to `mkdir` its target unconditionally, before checking
    whether there was anything to download. Every parse of a local PDF — every
    run of this test suite included — left an empty directory behind, and the
    parse needed write permission on a path it never wrote to. In the ingestion
    container that became `PermissionError: Permission denied: 'data'` for a PDF
    sitting on a mounted volume.
    """
    target = tmp_path / "raw"
    parser = GKVParser("/some/local/gkv.pdf", output_filename="gkv.pdf")

    assert parser.download_if_url(target_dir=target) == Path("/some/local/gkv.pdf")
    assert not target.exists(), "a local parse created a download directory"


def test_the_default_download_directory_is_package_relative():
    """It must not depend on the working directory.

    The default was `Path("data/raw")`, resolved against wherever the command was
    started. Everything runs from the repository root, so it pointed at
    `<root>/data/raw` while every real input lives in `<root>/pipelines/data/raw` —
    two directories, one always empty, and a URL download landing in the wrong
    one. In the container it pointed outside the bind mount, so a download would
    not have survived the run.
    """
    from pipelines.common.paths import RAW_DIR

    assert RAW_DIR.is_absolute()
    assert RAW_DIR.parts[-3:] == ("pipelines", "data", "raw")
    assert RAW_DIR == REAL_PDF.resolve().parent
