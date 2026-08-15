"""Parser for the Bundes-Klinik-Atlas open-data export (story E1-S9).

The Federal Ministry of Health publishes the hospital transparency directory
under § 135d Abs. 1 SGB V, prepared by the IQTIG. The export
(`*_TVERZ_Export.xml`) carries every hospital location with its address,
coordinates and structure data.

**The file is a user-supplied input, not a repository asset.** CareGraph ships
this parser, not the data: the open-data page states the public right to receive
the export but says nothing about redistribution, so a self-hoster downloads it
themselves under the same § 135d right. See E1-S9.

    https://bundes-klinik-atlas.de/open-data/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

# The export uses its own two-letter codes, and they are *not* ISO 3166-2.
# Bayern is `BA`, not `BY` — mapping by ISO would silently drop 277 hospitals.
# Verified against the 2026-07-28 distribution: NW 328 (largest state), BA 277,
# HB 12 (smallest), which matches reality.
LAND_TO_BUNDESLAND = {
    "BW": "Baden-Württemberg",
    "BA": "Bayern",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}

ATTRIBUTION = "Bundes-Klinik-Atlas (BMG/IQTIG), § 135d SGB V"


@dataclass
class ParseReport:
    """What a parse produced, and what it had to leave out."""

    total: int = 0
    skipped: list[str] = field(default_factory=list)
    unknown_land: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.skipped and not self.unknown_land

    def summary(self) -> str:
        return (f"{self.total} hospitals, skipped={len(self.skipped)}, "
                f"unknown_land={sorted(self.unknown_land) or 'none'}")


class KlinikAtlasParser:
    """Turns the export into loader-ready provider records."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.report = ParseReport()

    def parse(self) -> list[dict]:
        root = ET.parse(self.path).getroot()
        if root.tag != "Standorte":
            raise ValueError(
                f"{self.path.name}: root element is <{root.tag}>, expected <Standorte>. "
                "Is this a Bundes-Klinik-Atlas export?"
            )

        records = []
        for standort in root.findall("Standort"):
            record = self._record(standort)
            if record is None:
                continue
            records.append(record)

        self.report.total = len(records)
        if self.report.unknown_land:
            # Loud: an unmapped code means those hospitals lose their state, and
            # a silent miss is exactly how a region quietly disappears from the
            # data. The export is schema-alpha, so new codes are plausible.
            log.error("unmapped federal-state codes: %s — those rows carry no Bundesland",
                      ", ".join(sorted(self.report.unknown_land)))
        log.info("Bundes-Klinik-Atlas: %s", self.report.summary())
        return records

    def _record(self, standort: ET.Element) -> dict | None:
        contact = standort.find("StandortKontaktDaten")
        if contact is None:
            self.report.skipped.append("<Standort without StandortKontaktDaten>")
            return None

        stoid = (contact.get("STOID") or "").strip()
        name = (contact.get("Name") or "").strip()
        if not stoid or not name:
            self.report.skipped.append(name or stoid or "<unnamed>")
            return None

        land = (contact.get("Land") or "").strip()
        bundesland = LAND_TO_BUNDESLAND.get(land)
        if land and bundesland is None:
            self.report.unknown_land.add(land)

        return {
            # STOID, not the name: unique across the file, stable between
            # publications, and the join key to the Standortverzeichnis should
            # the IK be added later.
            "source_id": f"stoid:{stoid}",
            "ik_nummer": None,   # the Atlas carries none; see E1-S9 "Later"
            "type": "krankenhaus",
            "name": name,
            "parent_organization": None,
            "website": contact.get("URL") or None,
            "strasse": contact.get("Strasse") or None,
            "plz": contact.get("PLZ") or None,
            "ort": contact.get("Ort") or None,
            "bundesland": bundesland,
            "lat": _number(contact.get("Breitengrad")),
            "lon": _number(contact.get("Laengengrad")),
            "details": self._details(standort, contact, stoid),
        }

    def _details(self, standort: ET.Element, contact: ET.Element, stoid: str) -> dict:
        details: dict = {
            "source": "bundes-klinik-atlas",
            "attribution": ATTRIBUTION,
            "stoid": stoid,
            "traeger_art": contact.get("TraegerArt") or None,
            "telefon": contact.get("Telefon") or None,
            "email": contact.get("EMail") or None,
            "kinderklinik": _flag(contact.get("Kinderklinik")),
            "sicherstellungsauftrag": _flag(contact.get("Sicherstellungsauftrag")),
        }

        # Structure and emergency blocks are flat attribute sets; keep them whole
        # rather than cherry-picking, so a later question does not need a re-ingest.
        for block, key in (("StandortStrukturDaten", "struktur"),
                           ("StandortNotfallversorgung", "notfallversorgung")):
            element = standort.find(block)
            if element is not None and element.attrib:
                details[key] = {k: _number(v) if _looks_numeric(v) else v
                                for k, v in element.attrib.items()}

        for block, key, fields in (
            ("Fachabteilungen", "fachabteilungen", ("FABID", "Bezeichnung", "AnzahlFaelle")),
            ("Erkrankungen", "erkrankungen", ("Name", "Gruppe", "Anzahl")),
            ("Zertifizierungen", "zertifizierungen", ("Name", "Shortener", "GueltigkeitEnde")),
        ):
            element = standort.find(block)
            if element is None:
                continue
            entries = [{f: child.get(f) for f in fields if child.get(f)} for child in element]
            if entries:
                details[key] = entries

        barrierefreiheit = standort.find("Barrierefreiheit")
        if barrierefreiheit is not None:
            codes = [c.get("Schluessel") for c in barrierefreiheit if c.get("Schluessel")]
            if codes:
                details["barrierefreiheit"] = codes

        return {k: v for k, v in details.items() if v not in (None, [], {})}


def _number(value: str | None) -> float | None:
    """Parse a numeric attribute, or None. Never raises on unexpected text."""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _flag(value: str | None) -> bool | None:
    """`1`/`0` attributes become booleans; anything else stays unknown."""
    if value == "1":
        return True
    if value == "0":
        return False
    return None
