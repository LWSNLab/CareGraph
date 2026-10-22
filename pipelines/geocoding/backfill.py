from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipelines.geocoding.cache import GeocodeCache
from pipelines.geocoding.nominatim import ATTRIBUTION, Address, NominatimClient

log = logging.getLogger(__name__)

# Rows with a point but an incomplete address. Insurers never match: they have an
# address and no location, which is deliberate — a statutory insurer is a region,
# not a place (see the data schema).
SELECT_CANDIDATES = """
SELECT source_id,
       ST_Y(location::geometry) AS lat,
       ST_X(location::geometry) AS lon,
       strasse, plz, ort, bundesland
  FROM care_infrastructure
 WHERE location IS NOT NULL
   AND (strasse IS NULL OR plz IS NULL OR ort IS NULL)
 ORDER BY source_id
 LIMIT %(limit)s
"""

# COALESCE, so a value written between the SELECT and here survives. The process
# that reads and the process that writes are the same one today; writing it this
# way means that staying true is not a precondition.
UPDATE_ROW = """
UPDATE care_infrastructure
   SET strasse    = COALESCE(strasse, %(strasse)s),
       plz        = COALESCE(plz, %(plz)s),
       ort        = COALESCE(ort, %(ort)s),
       bundesland = COALESCE(bundesland, %(bundesland)s),
       details    = details || %(meta)s,
       updated_at = now()
 WHERE source_id = %(source_id)s
"""

ADDRESS_FIELDS = ("strasse", "plz", "ort", "bundesland")


@dataclass
class BackfillReport:
    """What one run did. The basis for deciding whether to run it again."""

    examined: int = 0
    filled: int = 0
    from_cache: int = 0
    no_answer: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        parts = [
            f"examined={self.examined}",
            f"filled={self.filled}",
            f"cached={self.from_cache}",
            f"no_answer={self.no_answer}",
        ]
        if self.failed:
            parts.append(f"failed={len(self.failed)}")
        return ", ".join(parts)


def _missing(row: dict) -> list[str]:
    """Which address fields this row lacks."""
    return [f for f in ADDRESS_FIELDS if not row.get(f)]


def _lookup(
    client: NominatimClient, cache: GeocodeCache, lat: float, lon: float,
) -> tuple[Address | None, bool]:
    """The address for a coordinate, and whether the cache answered.

    A stored ``None`` means Nominatim was asked and had nothing. Keeping that is
    what stops a backfill re-asking the same unanswerable points every night.
    """
    if (lat, lon) in cache:
        return cache.get(lat, lon), True
    address = client.reverse(lat, lon)
    cache.put(lat, lon, address)
    return address, False


def backfill_addresses(
    dsn: str,
    client: NominatimClient,
    cache: GeocodeCache,
    # None means every candidate: PostgreSQL reads `LIMIT NULL` as `LIMIT ALL`,
    # so the query needs no second form for the unlimited case.
    limit: int | None,
    dry_run: bool = False,
) -> BackfillReport:
    """Fill missing address fields for rows that have coordinates."""
    report = BackfillReport()

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_CANDIDATES, {"limit": limit})
            candidates = cur.fetchall()

        log.info("%d row(s) with coordinates and an incomplete address", len(candidates))

        for row in candidates:
            report.examined += 1
            lat, lon = row["lat"], row["lon"]

            try:
                address, cached = _lookup(client, cache, lat, lon)
            except Exception as err:  # noqa: BLE001 — one bad row must not end the run
                log.warning("%s: %s", row["source_id"], err)
                report.failed.append(row["source_id"])
                continue

            if cached:
                report.from_cache += 1
            if address is None:
                report.no_answer += 1
                continue

            wanted = _missing(row)
            fills = {f: getattr(address, f) for f in wanted if getattr(address, f)}
            if not fills:
                report.no_answer += 1
                continue

            if dry_run:
                report.filled += 1
                log.debug("would fill %s: %s", row["source_id"], fills)
                continue

            meta = {
                "derived_address": {
                    "source": "nominatim",
                    "place_id": address.place_id,
                    "fields": sorted(fills),
                    "derived_on": date.today().isoformat(),
                    "attribution": ATTRIBUTION,
                }
            }
            params = {f: fills.get(f) for f in ADDRESS_FIELDS}
            params |= {"source_id": row["source_id"], "meta": Jsonb(meta)}

            with conn.cursor() as cur:
                cur.execute(UPDATE_ROW, params)
            report.filled += 1

        if not dry_run:
            conn.commit()

    # Saved even after a failure: the answers already paid for should not have to
    # be asked again because a later row went wrong.
    cache.save()

    log.info("address backfill: %s", report.summary())
    return report
