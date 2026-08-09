"""Tests for the IK-Nummer directory and matcher (story E1-S6).

Everything here runs offline: the parser is fed literal IDK segments and the
matcher an in-memory directory. The precision cases are the important ones —
a wrong IK is silent in the data, so the matcher must refuse rather than guess.
"""

from __future__ import annotations

import json

import pytest

from pipelines.parsers.ik_verzeichnis import (
    IKVerzeichnis,
    MatchReport,
    _is_too_generic,
    _tokens,
)


def directory(*names_and_iks: tuple[str, str]) -> IKVerzeichnis:
    """Build a directory from (name, ik) pairs without touching the network."""
    v = IKVerzeichnis(overrides_path="/nonexistent.json")
    v.add_from_text("".join(f"IDK+{ik}+99+{name}'\n" for name, ik in names_and_iks))
    return v


# ------------------------------------------------------------------- parsing


def test_parses_idk_segments():
    v = directory(("Brandenburgische BKK", "100820488"))
    assert len(v) == 1
    assert v.lookup("Brandenburgische BKK") == "100820488"


def test_ignores_everything_that_is_not_an_idk_segment():
    v = IKVerzeichnis(overrides_path="/nonexistent.json")
    added = v.add_from_text("UNB+UNOC:3+104027544+999999999'\nVDT+20100701'\nFKT+01'\n")
    assert added == 0 and len(v) == 0


def test_umlauts_survive_iso_8859_1_content():
    v = directory(("Südzucker BKK", "106936311"))
    assert v.lookup("Südzucker BKK") == "106936311"


# ------------------------------------------------------------------ matching


def test_exact_match():
    v = directory(("AOK Bayern", "108616568"))
    assert v.lookup("AOK Bayern") == "108616568"


def test_marketing_suffixes_are_ignored():
    """'AOK Bayern - Die Gesundheitskasse' is the same insurer as 'AOK Bayern'."""
    v = directory(("AOK Bayern", "108616568"))
    assert v.lookup("AOK Bayern - Die Gesundheitskasse") == "108616568"


def test_filler_words_are_ignored():
    v = directory(("AOK Niedersachsen", "102114819"))
    assert v.lookup("AOK - Die Gesundheitskasse für Niedersachsen") == "102114819"


@pytest.mark.parametrize(
    "insurer, entry",
    [
        ("Merck BKK", "BKK Merck"),
        ("TUI BKK", "BKK TUI"),
        ("KARL MAYER BKK", "BKK Karl Mayer"),
        ("BKK B. Braun Aesculap", "BKK Braun B. Aesculap"),
    ],
)
def test_word_order_differences_are_tolerated(insurer, entry):
    """The directory routinely reverses the name; a string key cannot survive it."""
    v = directory((entry, "123456789"))
    assert v.lookup(insurer) == "123456789"


def test_parenthetical_annotations_are_dropped():
    v = directory(("BKK mkk (ehem. BKK VBU)", "109723913"))
    assert v.lookup("BKK mkk - meine krankenkasse") == "109723913"


def test_alias_table_resolves_short_forms():
    v = directory(("TK", "101575519"))
    assert v.lookup("Techniker Krankenkasse") == "101575519"


def test_unknown_insurer_returns_none():
    v = directory(("AOK Bayern", "108616568"))
    assert v.lookup("Völlig Andere Kasse") is None


def test_empty_name_returns_none():
    assert directory(("AOK Bayern", "1")).lookup("") is None


# ----------------------------------------------------------------- precision


def test_care_fund_entries_never_win():
    """A Pflegekasse has its own IK — attaching it to the Krankenkasse is wrong."""
    v = directory(
        ("VIACTIV Pflegekasse West", "111111111"),
        ("VIACTIV Krankenkasse", "222222222"),
    )
    assert v.lookup("VIACTIV Krankenkasse") == "222222222"


def test_regional_split_entries_never_win():
    v = directory(("SECURVITA BKK/Ost", "111111111"), ("SECURVITA BKK", "222222222"))
    assert v.lookup("SECURVITA BKK") == "222222222"


def test_care_fund_only_yields_no_match_rather_than_the_wrong_one():
    v = directory(("VIACTIV Pflegekasse West", "111111111"))
    assert v.lookup("VIACTIV Krankenkasse") is None


def test_generic_directory_entries_cannot_match():
    """Regression: 'BKK S-H' reduces to {'bkk'} and matched every other BKK."""
    v = directory(("BKK S-H", "101320043"))
    assert v.lookup("EY Betriebskrankenkasse") is None


def test_generic_query_names_cannot_match():
    """Regression: 'R+V Betriebskrankenkasse' also reduces to {'bkk'}."""
    v = directory(("BKK DEMAG KRAUSS-MAFFEI", "999999999"))
    assert v.lookup("R+V Betriebskrankenkasse") is None


def test_similar_but_different_insurers_do_not_match():
    """AOK Schwarzwald-Baar-Kreis is not BKK Schwarzwald-Baar-Heuberg."""
    v = directory(("AOK Schwarzwald-Baar-Kreis", "108018007"))
    assert v.lookup("BKK Schwarzwald-Baar-Heuberg") is None


def test_most_specific_candidate_wins():
    """A bare 'AOK' must not shadow the regional entry."""
    v = directory(("AOK Rheinland", "111111111"), ("AOK Rheinland/Hamburg", "222222222"))
    assert v.lookup("AOK Rheinland/Hamburg - Die Gesundheitskasse") == "222222222"


@pytest.mark.parametrize("tokens", [frozenset(), frozenset({"bkk"}), frozenset({"aok", "kk"})])
def test_generic_token_sets_are_recognised(tokens):
    assert _is_too_generic(tokens)


def test_knappschaft_is_not_generic():
    """Regression: listing it as generic made KNAPPSCHAFT unmatchable."""
    assert not _is_too_generic(_tokens("KNAPPSCHAFT"))


# ----------------------------------------------------------------- overrides


def test_overrides_take_precedence(tmp_path):
    path = tmp_path / "ik_overrides.json"
    path.write_text(json.dumps({"BKK24": "999888777"}), encoding="utf-8")

    v = IKVerzeichnis(overrides_path=path)
    v.add_from_text("IDK+111111111+99+BKK 24 Something'\n")

    assert v.lookup("BKK24") == "999888777"


def test_documentation_keys_are_not_treated_as_insurers(tmp_path):
    path = tmp_path / "ik_overrides.json"
    path.write_text(json.dumps({"_comment": "notes", "BKK24": "1"}), encoding="utf-8")
    assert len(IKVerzeichnis(overrides_path=path).overrides) == 1


def test_missing_overrides_file_is_fine():
    assert IKVerzeichnis(overrides_path="/definitely/absent.json").overrides == {}


# -------------------------------------------------------------------- report


def test_report_counts_and_rate():
    v = directory(("AOK Bayern", "108616568"))
    report = v.match_all(["AOK Bayern", "Unbekannte Kasse"])

    assert report.matched == {"AOK Bayern": "108616568"}
    assert report.unmatched == ["Unbekannte Kasse"]
    assert report.rate == pytest.approx(0.5)
    assert "matched=1/2" in report.summary()


def test_unmatched_are_reported_not_dropped():
    """The acceptance criterion: misses must stay visible."""
    report = directory(("AOK Bayern", "1")).match_all(["X", "Y", "Z"])
    assert len(report.unmatched) == 3


def test_empty_report_has_no_division_error():
    assert MatchReport().rate == 0.0


# ------------------------------------------------- Kassensitz-IK (primary)

BA_ROWS = """Schlüsselverzeichnis 8a: Verzeichnis der Abrechnungs-IK
Version: 035
Abrechnungs-IK Kassensitz-IK Name Bemerkung
109519005 109519005 AOKNordost
102122660 102122660 BKK24
107531187 107531187 BKKSchwarzwald-Baar-Heuberg
108033244 108433248 Siemens-BKK(SBK)
100602360 100602360 IKKBrandenburgundBerlin
"""


def kassensitz_directory() -> IKVerzeichnis:
    v = IKVerzeichnis(overrides_path="/nonexistent.json")
    v.add_kassensitz_from_text(BA_ROWS)
    return v


def test_kassensitz_rows_are_parsed():
    assert kassensitz_directory().lookup("BKK24") == "102122660"


def test_second_column_is_used_not_the_billing_ik():
    """An insurer has many Abrechnungs-IKs but one Kassensitz-IK."""
    assert kassensitz_directory().lookup("Siemens-Betriebskrankenkasse (SBK)") == "108433248"


def test_pdf_glued_names_still_match():
    """Text extraction drops spaces: 'AOKNordost' must meet 'AOK Nordost'."""
    assert kassensitz_directory().lookup("AOK Nordost - Die Gesundheitskasse") == "109519005"


def test_partially_glued_names_still_match():
    """'BKKSchwarzwald-Baar-Heuberg' glues only the first word."""
    v = kassensitz_directory()
    assert v.lookup("BKK Schwarzwald-Baar-Heuberg") == "107531187"


def test_innungskrankenkasse_abbreviates_to_ikk():
    v = kassensitz_directory()
    assert v.lookup("INNUNGSKRANKENKASSE BRANDENBURG UND BERLIN") == "100602360"


def test_kassensitz_wins_over_the_exchange_files():
    """Load order is the guarantee: the authoritative value must not be replaced."""
    v = IKVerzeichnis(overrides_path="/nonexistent.json")
    v.add_kassensitz_from_text("111111111 222222222 BKK Beispiel\n")
    v.add_from_text("IDK+999999999+99+BKK Beispiel'\n")
    assert v.lookup("BKK Beispiel") == "222222222"


def test_concat_key_is_order_preserving():
    """A sorted key would break on partially glued names."""
    from pipelines.parsers.ik_verzeichnis import _concat

    assert _concat("BKK Schwarzwald-Baar-Heuberg") == _concat("BKKSchwarzwald-Baar-Heuberg")
    assert _concat("AOK - Die Gesundheitskasse für Niedersachsen") == _concat("AOK Niedersachsen")
