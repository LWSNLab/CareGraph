"""Tests for the Bundes-Klinik-Atlas parser (story E1-S9).

Driven by literal XML rather than the real export: the file is a user-supplied
download and is deliberately not in the repository, so the suite must not depend
on it being present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.common import BUNDESLAENDER
from pipelines.parsers.klinik_atlas import (
    LAND_TO_BUNDESLAND,
    KlinikAtlasParser,
)

REAL_EXPORT = next(Path("pipelines/data/raw").glob("*_TVERZ_Export.xml"), None)

MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<Standorte>
  <Standort>
    <StandortKontaktDaten STOID="771003" Land="MV" Name="Klinikum Musterstadt"
      Strasse="Südring 81" PLZ="18059" Ort="Rostock" URL="http://example.de"
      Telefon="+49 381 1" EMail="a@example.de" TraegerArt="öffentlich"
      Kinderklinik="1" Sicherstellungsauftrag="0"
      Laengengrad="12.107577323914" Breitengrad="54.071629513465"/>
    <StandortStrukturDaten AnzahlBetten="533" AnzahlFaelle="24500"
      PflegePersonalQuotient="69.38"/>
    <StandortNotfallversorgung Stufe="2" StrokeUnit="0"/>
    <Barrierefreiheit><BF Schluessel="BF34"/><BF Schluessel="BF35"/></Barrierefreiheit>
    <Zertifizierungen>
      <Zertifikat Name="Brustkrebszentrum" GueltigkeitEnde="08.10.2027"/>
    </Zertifizierungen>
    <Fachabteilungen>
      <Fachabteilung FABID="3600" Bezeichnung="Intensivmedizin" AnzahlFaelle="508.725"/>
    </Fachabteilungen>
    <Erkrankungen>
      <Erkrankung Name="Schlaganfall" Gruppe="KASA0" Anzahl="167"/>
    </Erkrankungen>
  </Standort>
</Standorte>
"""


def parse(xml: str, tmp_path: Path) -> tuple[list[dict], KlinikAtlasParser]:
    path = tmp_path / "export.xml"
    path.write_text(xml, encoding="utf-8")
    parser = KlinikAtlasParser(path)
    return parser.parse(), parser


def test_maps_a_hospital_into_a_loader_record(tmp_path):
    (record,), _ = parse(MINIMAL, tmp_path)

    # STOID, not the name: unique, stable, and the join key to the
    # Standortverzeichnis if the IK is added later.
    assert record["source_id"] == "stoid:771003"
    assert record["type"] == "krankenhaus"
    assert record["name"] == "Klinikum Musterstadt"
    assert record["bundesland"] == "Mecklenburg-Vorpommern"
    assert record["lat"] == pytest.approx(54.071629513465)
    assert record["lon"] == pytest.approx(12.107577323914)
    # The Atlas carries no IK; that is expected, not a parse failure.
    assert record["ik_nummer"] is None


def test_latitude_and_longitude_are_not_transposed(tmp_path):
    """Germany sits near 51°N, 10°E — a swap puts every hospital in Somalia."""
    (record,), _ = parse(MINIMAL, tmp_path)
    assert 47 < record["lat"] < 56, "latitude out of range for Germany"
    assert 5 < record["lon"] < 16, "longitude out of range for Germany"


def test_quality_blocks_are_preserved(tmp_path):
    (record,), _ = parse(MINIMAL, tmp_path)
    details = record["details"]

    assert details["source"] == "bundes-klinik-atlas"
    assert "§ 135d" in details["attribution"]
    assert details["struktur"]["AnzahlBetten"] == 533
    assert details["struktur"]["PflegePersonalQuotient"] == pytest.approx(69.38)
    assert details["notfallversorgung"]["Stufe"] == 2
    assert details["barrierefreiheit"] == ["BF34", "BF35"]
    assert details["zertifizierungen"][0]["Name"] == "Brustkrebszentrum"
    assert details["fachabteilungen"][0]["Bezeichnung"] == "Intensivmedizin"
    assert details["erkrankungen"][0]["Gruppe"] == "KASA0"


def test_flags_become_booleans_not_strings(tmp_path):
    (record,), _ = parse(MINIMAL, tmp_path)
    assert record["details"]["kinderklinik"] is True
    assert record["details"]["sicherstellungsauftrag"] is False


def test_every_state_code_maps_to_a_canonical_name():
    """`Land` is not ISO 3166-2: Bayern is `BA`, not `BY`.

    Mapping by ISO would have dropped 277 hospitals from the real export.
    """
    assert LAND_TO_BUNDESLAND["BA"] == "Bayern"
    assert "BY" not in LAND_TO_BUNDESLAND
    assert len(LAND_TO_BUNDESLAND) == 16
    # Every target must be a name the bundeslaender table knows, or the state
    # link silently fails to resolve.
    assert set(LAND_TO_BUNDESLAND.values()) == set(BUNDESLAENDER)


def test_an_unknown_state_code_is_reported_not_swallowed(tmp_path):
    """The export schema is alpha; a new code must not quietly lose a region."""
    records, parser = parse(MINIMAL.replace('Land="MV"', 'Land="XX"'), tmp_path)

    assert records[0]["bundesland"] is None
    assert parser.report.unknown_land == {"XX"}
    assert not parser.report.ok


def test_rows_without_an_identifier_are_skipped_and_counted(tmp_path):
    xml = MINIMAL.replace('STOID="771003"', 'STOID=""')
    records, parser = parse(xml, tmp_path)

    assert records == []
    assert len(parser.report.skipped) == 1


def test_a_foreign_xml_is_rejected_with_a_readable_error(tmp_path):
    path = tmp_path / "other.xml"
    path.write_text("<Qualitaetsbericht><A/></Qualitaetsbericht>", encoding="utf-8")

    with pytest.raises(ValueError, match="expected <Standorte>"):
        KlinikAtlasParser(path).parse()


def test_non_numeric_attributes_do_not_raise(tmp_path):
    xml = MINIMAL.replace('AnzahlBetten="533"', 'AnzahlBetten="k.A."')
    (record,), _ = parse(xml, tmp_path)
    # Kept as given rather than dropped: the value is information even when it
    # is not a number.
    assert record["details"]["struktur"]["AnzahlBetten"] == "k.A."


@pytest.mark.skipif(REAL_EXPORT is None, reason="Bundes-Klinik-Atlas export not present")
def test_the_real_export_parses_completely():
    parser = KlinikAtlasParser(REAL_EXPORT)
    records = parser.parse()

    assert len(records) > 1000
    assert parser.report.ok, parser.report.summary()
    assert all(r["lat"] and r["lon"] for r in records), "a hospital lost its coordinates"
    assert all(r["bundesland"] in BUNDESLAENDER for r in records)
    assert len({r["source_id"] for r in records}) == len(records), "STOID not unique"
