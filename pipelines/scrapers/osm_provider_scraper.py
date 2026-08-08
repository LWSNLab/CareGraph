"""Care-provider collection from OpenStreetMap via the Overpass API.

Story E1-S2. OSM is used as the nationwide base source because it is openly
licensed (ODbL) and explicitly intended for programmatic bulk access — unlike
the insurer portals (Pflegelotse, AOK Pflegenavigator), whose ``robots.txt``
disallows their search/result pages and whose directories are protected
databases (§ 87a UrhG). See docs `legal/data-licensing.md`.

Trade-offs of this source, stated plainly:

* Coverage is incomplete (roughly a third of the ~30k German facilities).
* There is no IK-Nummer in OSM; ``ik_nummer`` stays ``None`` for these records.
* Coordinates come for free, which is why records from here need no geocoding.

Attribution is mandatory: "© OpenStreetMap contributors (ODbL)".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"

BUNDESLAENDER = (
    "Baden-Württemberg",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Mecklenburg-Vorpommern",
    "Niedersachsen",
    "Nordrhein-Westfalen",
    "Rheinland-Pfalz",
    "Saarland",
    "Sachsen",
    "Sachsen-Anhalt",
    "Schleswig-Holstein",
    "Thüringen",
)

# OSM social_facility value -> CareGraph provider_type.
# Derived from the actual tag distribution in German OSM data, not guessed.
# Deliberately unmapped: assisted_living, group_home, day_care, shelter —
# they are care-adjacent but do not fit the four provider_type enum values.
TYPE_MAPPING = {
    "nursing_home": "pflegeheim_stationaer",
    "outreach": "pflegedienst_ambulant",
    "ambulatory_care": "pflegedienst_ambulant",
    "advice": "pflegestuetzpunkt",
}

# Facilities whose tag alone identifies elderly/nursing care. These may keep an
# unset `social_facility:for`.
UNAMBIGUOUS_FACILITIES = {"nursing_home", "ambulatory_care"}

# Facilities whose tag is generic social work ("aufsuchende Hilfe", counselling).
# Without an explicit senior audience these pull in unrelated services — early
# childhood intervention, women's centres, addiction support — so they require
# `social_facility:for=senior`.
AUDIENCE_REQUIRED_FACILITIES = {"outreach", "advice"}

SENIOR_AUDIENCE = "senior"


class OverpassError(RuntimeError):
    """Overpass could not serve the query (busy, timeout, malformed)."""


@dataclass
class ProviderRecord:
    """One care provider, shaped for the ``care_infrastructure`` table."""

    type: str
    name: str
    ik_nummer: str | None = None
    parent_organization: str | None = None
    website: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    bundesland: str | None = None
    lat: float | None = None
    lon: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        """Stable identifier for upserts: the OSM type/id pair."""
        return f"osm:{self.details.get('osm_type')}/{self.details.get('osm_id')}"


@dataclass
class RunReport:
    """Outcome of an ingestion run — the basis for alerting (E1-S2 AC4)."""

    requested: list[str] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    records: int = 0
    skipped_unmapped: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        return (
            f"regions ok={len(self.succeeded)}/{len(self.requested)} "
            f"records={self.records} skipped={self.skipped_unmapped} "
            f"failed={sorted(self.failed) or 'none'}"
        )


class OSMProviderScraper:
    """Fetches care providers per federal state from the Overpass API."""

    def __init__(
        self,
        endpoint: str = "https://overpass-api.de/api/interpreter",
        timeout: int = 180,
        delay: float = 3.0,
        max_retries: int = 3,
        user_agent: str = "CareGraphBot/0.1 (+https://github.com/LWSNLab/CareGraph)",
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.delay = delay          # politeness pause between regions
        self.max_retries = max_retries
        self.headers = {"User-Agent": user_agent}

    # ---------------------------------------------------------------- query

    @staticmethod
    def build_query(bundesland: str, timeout: int = 180) -> str:
        """Overpass QL for all care-related facilities in one federal state."""
        return f"""
[out:json][timeout:{timeout}];
area["name"="{bundesland}"]["admin_level"="4"]->.a;
(
  nwr(area.a)["amenity"="social_facility"];
  nwr(area.a)["healthcare"="nurse"];
);
out center tags;
""".strip()

    def _post(self, query: str) -> dict:
        """POST a query, retrying the transient 'server is busy' failures.

        Overpass is a shared free service; it answers overload with a 200 that
        contains an HTML error body, so the JSON decode is the actual check.
        """
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                res = requests.post(
                    self.endpoint,
                    data=query.encode("utf-8"),
                    headers=self.headers,
                    timeout=self.timeout,
                )
                res.raise_for_status()
                return res.json()          # HTML error body -> ValueError
            except (requests.RequestException, ValueError) as err:
                last = err
                backoff = self.delay * 2**attempt
                log.warning(
                    "Overpass attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt, self.max_retries, type(err).__name__, backoff,
                )
                if attempt < self.max_retries:
                    time.sleep(backoff)
        raise OverpassError(f"Overpass failed after {self.max_retries} attempts: {last}")

    # -------------------------------------------------------------- mapping

    @staticmethod
    def map_element(element: dict, bundesland: str | None = None) -> ProviderRecord | None:
        """Map one Overpass element to a ProviderRecord, or None if out of scope.

        Pure function — the unit tests exercise the mapping without network.
        """
        tags = element.get("tags") or {}

        facility = tags.get("social_facility")
        provider_type = TYPE_MAPPING.get(facility)

        # healthcare=nurse without a social_facility value is an ambulatory service.
        is_nurse = provider_type is None and tags.get("healthcare") == "nurse"
        if is_nurse:
            provider_type = "pflegedienst_ambulant"

        if provider_type is None:
            return None

        # Keep elderly care only. `social_facility:for` is a multi-value tag
        # ("senior;disabled", "juvenile;abuse"), so it must be split — an exact
        # comparison would let mixed audiences slip through.
        audiences = {
            value.strip()
            for value in (tags.get("social_facility:for") or "").split(";")
            if value.strip()
        }

        if SENIOR_AUDIENCE not in audiences:
            if audiences:
                return None  # explicitly some other audience
            if facility in AUDIENCE_REQUIRED_FACILITIES:
                return None  # generic social work, no senior context

        name = (tags.get("name") or "").strip()
        if not name:
            return None  # unnamed facilities are not useful downstream

        centre = element.get("center") or {}
        lat = element.get("lat", centre.get("lat"))
        lon = element.get("lon", centre.get("lon"))

        street = tags.get("addr:street")
        housenumber = tags.get("addr:housenumber")
        strasse = " ".join(p for p in (street, housenumber) if p) or None

        details = {
            "osm_type": element.get("type"),
            "osm_id": element.get("id"),
            "social_facility": facility,
            "social_facility:for": tags.get("social_facility:for"),
            "source": "openstreetmap",
            "attribution": ATTRIBUTION,
        }
        for tag_key, detail_key in (
            ("phone", "phone"),
            ("contact:phone", "phone"),
            ("email", "email"),
            ("contact:email", "email"),
            ("opening_hours", "opening_hours"),
            ("wheelchair", "wheelchair"),
        ):
            if tag_key in tags and detail_key not in details:
                details[detail_key] = tags[tag_key]

        return ProviderRecord(
            type=provider_type,
            name=name,
            ik_nummer=None,  # OSM carries no Institutionskennzeichen
            parent_organization=tags.get("operator"),
            website=tags.get("website") or tags.get("contact:website"),
            strasse=strasse,
            plz=tags.get("addr:postcode"),
            ort=tags.get("addr:city"),
            bundesland=bundesland,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            details={k: v for k, v in details.items() if v is not None},
        )

    # -------------------------------------------------------------- fetching

    def fetch_bundesland(self, bundesland: str) -> tuple[list[ProviderRecord], int]:
        """Fetch one federal state. Returns (records, skipped_unmapped)."""
        payload = self._post(self.build_query(bundesland, self.timeout))
        elements = payload.get("elements", [])

        records, skipped = [], 0
        for element in elements:
            record = self.map_element(element, bundesland)
            if record is None:
                skipped += 1
            else:
                records.append(record)

        log.info("%s: %d providers (%d skipped)", bundesland, len(records), skipped)
        return records, skipped

    def fetch(self, bundeslaender: Iterable[str] | None = None) -> tuple[list[ProviderRecord], RunReport]:
        """Fetch several federal states, collecting failures instead of aborting.

        One unreachable region must not lose the whole run; the report carries
        what failed so a scheduler can alert on it.
        """
        regions = list(bundeslaender) if bundeslaender is not None else list(BUNDESLAENDER)
        report = RunReport(requested=regions)
        all_records: list[ProviderRecord] = []

        for index, region in enumerate(regions):
            if index:
                time.sleep(self.delay)  # be a good Overpass citizen
            try:
                records, skipped = self.fetch_bundesland(region)
            except OverpassError as err:
                log.error("region %s failed: %s", region, err)
                report.failed[region] = str(err)
                continue

            all_records.extend(records)
            report.succeeded.append(region)
            report.skipped_unmapped += skipped

        report.records = len(all_records)
        log.info("run finished: %s", report.summary())
        return all_records, report
