"""Load ingested records into CareGraph's PostGIS schema (story E1-S4).

This replaces the file-based exporter as the pipeline's destination: the
database is the single source of truth, the API reads from it, and a scheduled
ingestion keeps it current. Nothing here is called per API request.

Idempotency is the central property — a nightly or monthly re-run must update
rows, never duplicate them. The join key is ``care_infrastructure.source_id``,
a source-namespaced external identifier (``osm:node/123``, ``ik:108616568``).

Contribution rates are *appended* to ``zusatzbeitrag_historie`` rather than
overwritten, so the yearly GKV publication builds a time series instead of
destroying the previous value.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

# Columns updated on conflict. `source_id` is the key and `created_at` must
# survive an update, so neither appears here.
_UPSERT_COLUMNS = (
    "ik_nummer",
    "type",
    "name",
    "parent_organization",
    "website",
    "strasse",
    "plz",
    "ort",
    "bundesland",
    "details",
    "scraping_status",
)


@dataclass
class LoadReport:
    """Outcome of a load run — inserted/updated counts and anything skipped."""

    inserted: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)
    state_links: int = 0
    history_rows: int = 0
    rekeyed: int = 0        # rows migrated from a name key to an IK key

    @property
    def ok(self) -> bool:
        return not self.skipped

    def summary(self) -> str:
        return (
            f"inserted={self.inserted} updated={self.updated} "
            f"skipped={len(self.skipped)} state_links={self.state_links} "
            f"history_rows={self.history_rows} rekeyed={self.rekeyed}"
        )


class PostgresLoader:
    """Writes providers and insurers into ``care_infrastructure`` and satellites."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    # ------------------------------------------------------------------ utils

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _upsert_sql() -> str:
        """INSERT … ON CONFLICT (source_id) DO UPDATE — the idempotent write.

        ``location`` is built from lon/lat inside SQL so PostGIS owns the
        geometry construction; NULL coordinates yield a NULL location rather
        than a bogus point at (0, 0).
        """
        assignments = ",\n            ".join(
            f"{col} = EXCLUDED.{col}" for col in _UPSERT_COLUMNS
        )
        return f"""
        INSERT INTO care_infrastructure (
            source_id, {', '.join(_UPSERT_COLUMNS)}, location
        ) VALUES (
            %(source_id)s, %(ik_nummer)s, %(type)s, %(name)s,
            %(parent_organization)s, %(website)s, %(strasse)s, %(plz)s,
            %(ort)s, %(bundesland)s, %(details)s, %(scraping_status)s,
            -- Explicit casts: for records without coordinates (insurers today)
            -- every parameter is NULL and Postgres cannot infer a type otherwise.
            CASE
                WHEN %(lon)s::double precision IS NULL
                  OR %(lat)s::double precision IS NULL THEN NULL
                ELSE ST_SetSRID(
                    ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision),
                    4326
                )::geography
            END
        )
        ON CONFLICT (source_id) DO UPDATE SET
            {assignments},
            location = EXCLUDED.location,
            updated_at = now()
        RETURNING id, (xmax = 0) AS inserted
        """

    # -------------------------------------------------------------- providers

    def load_providers(self, records: Iterable[Any], batch_size: int = 500) -> LoadReport:
        """Upsert care providers (from OSM or open data) into care_infrastructure."""
        report = LoadReport()
        rows = []

        for record in records:
            params = self._provider_params(record)
            if params is None:
                report.skipped.append(getattr(record, "name", "<unnamed>"))
                continue
            rows.append(params)

        sql = self._upsert_sql()
        with self._connect() as conn:
            with conn.cursor() as cur:
                for start in range(0, len(rows), batch_size):
                    for params in rows[start: start + batch_size]:
                        cur.execute(sql, params)
                        result = cur.fetchone()
                        if result["inserted"]:
                            report.inserted += 1
                        else:
                            report.updated += 1
            conn.commit()

        log.info("providers loaded: %s", report.summary())
        return report

    @staticmethod
    def _provider_params(record: Any) -> dict[str, Any] | None:
        """Map a ProviderRecord (or dict) to query parameters."""
        get = record.get if isinstance(record, dict) else lambda k, d=None: getattr(record, k, d)

        name = (get("name") or "").strip()
        details = get("details") or {}
        source_id = (
            get("source_id")
            if not isinstance(record, dict)
            else f"osm:{details.get('osm_type')}/{details.get('osm_id')}"
        )
        if not name or not source_id or "None" in str(source_id):
            return None

        return {
            "source_id": source_id,
            "ik_nummer": get("ik_nummer"),
            "type": get("type"),
            "name": name,
            "parent_organization": get("parent_organization"),
            "website": get("website"),
            "strasse": get("strasse"),
            "plz": get("plz"),
            "ort": get("ort"),
            "bundesland": get("bundesland"),
            "details": json.dumps(details, ensure_ascii=False),
            "scraping_status": details.get("source", "unknown"),
            "lat": get("lat"),
            "lon": get("lon"),
        }

    # --------------------------------------------------------------- insurers

    def load_insurers(
        self,
        insurers: Sequence[dict[str, Any]],
        gueltig_ab: date,
        quelle: str = "GKV-Spitzenverband",
    ) -> LoadReport:
        """Upsert statutory insurers, their state coverage and contribution rate.

        `insurers` items carry the GKV parser's fields plus optional address
        data: name, website, zusatzbeitrag, bundeslaender (list), ik_nummer,
        strasse, plz, ort.
        """
        report = LoadReport()
        sql = self._upsert_sql()

        with self._connect() as conn:
            with conn.cursor() as cur:
                for insurer in insurers:
                    name = (insurer.get("name") or "").strip()
                    if not name:
                        report.skipped.append("<unnamed insurer>")
                        continue

                    ik = insurer.get("ik_nummer")
                    # IK is the stable key; the name is the fallback for the
                    # few insurers no Kostenträgerdatei lists (E1-S6).
                    source_id = f"ik:{ik}" if ik else f"gkv:{name}"

                    # An insurer already in the table may carry an older key:
                    # the name key from before IKs were resolved, or a previous
                    # IK (the official list corrects these between versions).
                    # Rewrite in place — otherwise the upsert below inserts a
                    # second row and orphans the first.
                    if ik:
                        cur.execute(
                            """
                            UPDATE care_infrastructure
                               SET source_id = %(new)s
                             WHERE type = 'krankenkasse'
                               AND name = %(name)s
                               AND source_id <> %(new)s
                               AND NOT EXISTS (
                                   SELECT 1 FROM care_infrastructure existing
                                    WHERE existing.source_id = %(new)s
                               )
                            """,
                            {"new": source_id, "name": name},
                        )
                        if cur.rowcount:
                            report.rekeyed += cur.rowcount

                    details = {
                        "source": "gkv-spitzenverband",
                        "geoffnet_in": insurer.get("geoffnet_in"),
                        "is_bundesweit": bool(insurer.get("is_bundesweit")),
                    }

                    cur.execute(sql, {
                        "source_id": source_id,
                        "ik_nummer": ik,
                        "type": "krankenkasse",
                        "name": name,
                        "parent_organization": None,
                        "website": insurer.get("website"),
                        "strasse": insurer.get("strasse") or None,
                        "plz": insurer.get("plz") or None,
                        "ort": insurer.get("ort") or None,
                        "bundesland": None,     # insurers are regional, see junction
                        "details": json.dumps(details, ensure_ascii=False),
                        "scraping_status": insurer.get("scraping_status") or "parsed",
                        "lat": insurer.get("lat"),
                        "lon": insurer.get("lon"),
                    })
                    row = cur.fetchone()
                    insurer_id = row["id"]
                    if row["inserted"]:
                        report.inserted += 1
                    else:
                        report.updated += 1

                    report.state_links += self._sync_states(
                        cur, insurer_id, insurer.get("bundeslaender") or []
                    )
                    report.history_rows += self._append_rate(
                        cur, insurer_id, insurer.get("zusatzbeitrag"), gueltig_ab, quelle
                    )
            conn.commit()

        log.info("insurers loaded: %s", report.summary())
        return report

    @staticmethod
    def _sync_states(cur, insurer_id: str, states: Sequence[str]) -> int:
        """Replace an insurer's state links so removals are applied too."""
        cur.execute(
            "DELETE FROM krankenkasse_bundesland WHERE krankenkasse_id = %s",
            (insurer_id,),
        )
        if not states:
            return 0
        cur.execute(
            """
            INSERT INTO krankenkasse_bundesland (krankenkasse_id, bundesland_id)
            SELECT %s, b.id FROM bundeslaender b WHERE b.name = ANY(%s)
            ON CONFLICT DO NOTHING
            """,
            (insurer_id, list(states)),
        )
        return cur.rowcount

    @staticmethod
    def _append_rate(
        cur, insurer_id: str, rate: Any, gueltig_ab: date, quelle: str
    ) -> int:
        """Append the contribution rate for this publication date.

        Append-only: re-running the same publication is a no-op, and last
        year's value is never overwritten.
        """
        if rate is None:
            return 0
        cur.execute(
            """
            INSERT INTO zusatzbeitrag_historie
                (krankenkasse_id, gueltig_ab, zusatzbeitrag, quelle)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (krankenkasse_id, gueltig_ab) DO NOTHING
            """,
            (insurer_id, gueltig_ab, rate, quelle),
        )
        return cur.rowcount
