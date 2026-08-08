"""Unit tests for the OSM provider mapping (no network required)."""

from __future__ import annotations

import pytest

from pipelines.scrapers.osm_provider_scraper import (
    OSMProviderScraper,
    OverpassError,
    ProviderRecord,
    RunReport,
)

map_element = OSMProviderScraper.map_element


def node(**tags) -> dict:
    return {"type": "node", "id": 1, "lat": 53.1, "lon": 8.8, "tags": {"name": "Test", **tags}}


# ------------------------------------------------------------------ mapping


@pytest.mark.parametrize(
    "tags, expected",
    [
        # Unambiguous facility tags need no audience tag.
        ({"social_facility": "nursing_home"}, "pflegeheim_stationaer"),
        ({"social_facility": "ambulatory_care"}, "pflegedienst_ambulant"),
        ({"healthcare": "nurse"}, "pflegedienst_ambulant"),
        # Generic ones only count with an explicit senior audience.
        ({"social_facility": "outreach", "social_facility:for": "senior"}, "pflegedienst_ambulant"),
        ({"social_facility": "advice", "social_facility:for": "senior"}, "pflegestuetzpunkt"),
    ],
)
def test_maps_known_facility_types(tags, expected):
    assert map_element(node(**tags)).type == expected


@pytest.mark.parametrize("facility", ["outreach", "advice"])
def test_generic_facilities_without_audience_are_skipped(facility):
    """'outreach'/'advice' alone is generic social work, not elderly care.

    Real examples this excludes: early-childhood intervention centres,
    women's centres, cancer support groups.
    """
    assert map_element(node(social_facility=facility)) is None


@pytest.mark.parametrize(
    "audience, kept",
    [
        ("senior;disabled", True),    # senior among several -> keep
        ("disabled;senior", True),
        ("juvenile;abuse", False),    # multi-value, none senior -> drop
        ("homeless;underprivileged", False),
    ],
)
def test_multi_value_audience_tag_is_split(audience, kept):
    """`social_facility:for` is multi-value; exact matching would leak records."""
    element = node(social_facility="ambulatory_care", **{"social_facility:for": audience})
    assert (map_element(element) is not None) is kept


@pytest.mark.parametrize(
    "facility",
    ["assisted_living", "group_home", "day_care", "shelter", "food_bank"],
)
def test_skips_facility_types_outside_the_enum(facility):
    assert map_element(node(social_facility=facility)) is None


def test_skips_non_senior_audiences():
    assert map_element(node(social_facility="outreach", **{"social_facility:for": "refugee"})) is None
    assert map_element(node(social_facility="advice", **{"social_facility:for": "child"})) is None
    assert map_element(node(social_facility="ambulatory_care", **{"social_facility:for": "child"})) is None


def test_skips_unnamed_facilities():
    element = {"type": "node", "id": 2, "lat": 1.0, "lon": 2.0,
               "tags": {"social_facility": "nursing_home"}}
    assert map_element(element) is None


def test_skips_element_without_tags():
    assert map_element({"type": "node", "id": 3}) is None


# ------------------------------------------------------------------- fields


def test_extracts_address_and_contact_fields():
    element = node(
        social_facility="nursing_home",
        operator="AWO",
        website="https://example.de",
        phone="+49 421 123",
        **{
            "addr:street": "Oslebshauser Landstraße",
            "addr:housenumber": "20",
            "addr:postcode": "28239",
            "addr:city": "Bremen",
        },
    )
    record = map_element(element, "Bremen")

    assert record.strasse == "Oslebshauser Landstraße 20"
    assert record.plz == "28239"
    assert record.ort == "Bremen"
    assert record.bundesland == "Bremen"
    assert record.parent_organization == "AWO"
    assert record.website == "https://example.de"
    assert record.details["phone"] == "+49 421 123"


def test_street_without_housenumber_still_maps():
    record = map_element(node(social_facility="nursing_home", **{"addr:street": "Hauptstraße"}))
    assert record.strasse == "Hauptstraße"


def test_missing_address_yields_none_fields_not_crash():
    record = map_element(node(social_facility="nursing_home"))
    assert record.strasse is None and record.plz is None and record.ort is None


def test_ik_nummer_is_none_because_osm_has_none():
    assert map_element(node(social_facility="nursing_home")).ik_nummer is None


def test_carries_osm_provenance_and_attribution():
    record = map_element(node(social_facility="nursing_home"))
    assert record.details["source"] == "openstreetmap"
    assert "OpenStreetMap" in record.details["attribution"]
    assert record.source_id == "osm:node/1"


def test_uses_center_for_ways_without_direct_coordinates():
    element = {
        "type": "way",
        "id": 42,
        "center": {"lat": 52.5, "lon": 13.4},
        "tags": {"name": "Haus", "social_facility": "nursing_home"},
    }
    record = map_element(element)
    assert (record.lat, record.lon) == (52.5, 13.4)
    assert record.source_id == "osm:way/42"


def test_contact_prefixed_tags_are_used_as_fallback():
    record = map_element(node(social_facility="nursing_home", **{"contact:phone": "030 1"}))
    assert record.details["phone"] == "030 1"


# -------------------------------------------------------------------- query


def test_query_contains_region_and_returns_centers():
    query = OSMProviderScraper.build_query("Bayern")
    assert '"name"="Bayern"' in query
    assert '"admin_level"="4"' in query
    assert "out center tags;" in query


# ------------------------------------------------------------------- report


def test_report_is_ok_only_without_failures():
    report = RunReport(requested=["Bremen"], succeeded=["Bremen"], records=5)
    assert report.ok

    report.failed["Hamburg"] = "timeout"
    assert not report.ok
    assert "Hamburg" in report.summary()


# ------------------------------------------------------------ fetch/failures


class _Scraper(OSMProviderScraper):
    """Test double: no network, no sleeping."""

    def __init__(self, behaviour: dict):
        super().__init__(delay=0)
        self.behaviour = behaviour

    def fetch_bundesland(self, bundesland: str):
        outcome = self.behaviour[bundesland]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fetch_collects_failures_instead_of_aborting():
    """One bad region must not lose the results of the good ones."""
    scraper = _Scraper(
        {
            "Bremen": ([ProviderRecord(type="pflegeheim_stationaer", name="A")], 2),
            "Hamburg": OverpassError("server busy"),
            "Berlin": ([ProviderRecord(type="pflegedienst_ambulant", name="B")], 0),
        }
    )

    records, report = scraper.fetch(["Bremen", "Hamburg", "Berlin"])

    assert [r.name for r in records] == ["A", "B"]
    assert report.succeeded == ["Bremen", "Berlin"]
    assert "Hamburg" in report.failed
    assert report.records == 2
    assert report.skipped_unmapped == 2
    assert not report.ok
