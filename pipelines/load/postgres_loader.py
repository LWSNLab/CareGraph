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
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row

from pipelines.common import normalize_website

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

# Columns an update must not blank out. `ik_nummer` comes from a network
# enrichment step (E1-S6); when that step degrades, the record arrives without
# an IK, and `ik_nummer = EXCLUDED.ik_nummer` would erase what an earlier run
# had resolved. Until the insurer key stopped flapping this was invisible,
# because a run without an IK inserted a second row instead of updating.
_PRESERVE_IF_NULL = frozenset({"ik_nummer"})
_PROVIDER_VALUE_COLUMNS = (
    "source_id", *_UPSERT_COLUMNS,
)
_POSTGRES_MAX_PARAMETERS = 65535
_MAX_PROVIDER_BATCH_SIZE = _POSTGRES_MAX_PARAMETERS // (
    len(_PROVIDER_VALUE_COLUMNS) + 2  # latitude and longitude
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
    key_preserved: int = 0  # rows matched by name because this run had no IK
    duplicates: int = 0     # repeated source IDs in the input; last record wins

    @property
    def ok(self) -> bool:
        return not self.skipped

    def summary(self) -> str:
        parts = [
            f"inserted={self.inserted}",
            f"updated={self.updated}",
            f"skipped={len(self.skipped)}",
            f"state_links={self.state_links}",
            f"history_rows={self.history_rows}",
            f"rekeyed={self.rekeyed}",
        ]
        # Only shown when it happened: a non-zero value means IK enrichment came
        # back thinner than the database already knew about.
        if self.key_preserved:
            parts.append(f"key_preserved={self.key_preserved}")
        if self.duplicates:
            parts.append(f"duplicates={self.duplicates}")
        return " ".join(parts)


class PostgresLoader:
    """Writes providers and insurers into ``care_infrastructure`` and satellites."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    # ------------------------------------------------------------------ utils

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _upsert_sql(values_sql: str | None = None) -> str:
        """INSERT … ON CONFLICT (source_id) DO UPDATE — the idempotent write.

        ``location`` is built from lon/lat inside SQL so PostGIS owns the
        geometry construction; NULL coordinates yield a NULL location rather
        than a bogus point at (0, 0).
        """
        def assignment(col: str) -> str:
            if col in _PRESERVE_IF_NULL:
                return f"{col} = COALESCE(EXCLUDED.{col}, care_infrastructure.{col})"
            return f"{col} = EXCLUDED.{col}"

        assignments = ",\n            ".join(assignment(col) for col in _UPSERT_COLUMNS)
        values_sql = values_sql or """
        (%(source_id)s, %(ik_nummer)s, %(type)s, %(name)s,
         %(parent_organization)s, %(website)s, %(strasse)s, %(plz)s,
         %(ort)s, %(bundesland)s, %(details)s, %(scraping_status)s,
         CASE
             WHEN %(lon)s::double precision IS NULL
               OR %(lat)s::double precision IS NULL THEN NULL
             ELSE ST_SetSRID(
                 ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision),
                 4326
             )::geography
         END)
        """
        return f"""
        INSERT INTO care_infrastructure (
            source_id, {', '.join(_UPSERT_COLUMNS)}, location
        ) VALUES (
            {values_sql}
        )
        ON CONFLICT (source_id) DO UPDATE SET
            {assignments},
            location = EXCLUDED.location,
            updated_at = now()
        RETURNING id, (xmax = 0) AS inserted
        """

    @staticmethod
    def _provider_values_sql(suffix: str) -> str:
        """Build one parameterised provider tuple for a multi-row upsert."""
        parameters = ", ".join(
            f"%({column}{suffix})s" for column in _PROVIDER_VALUE_COLUMNS
        )
        location = (
            f"CASE WHEN %(lon{suffix})s::double precision IS NULL "
            f"OR %(lat{suffix})s::double precision IS NULL THEN NULL "
            f"ELSE ST_SetSRID(ST_MakePoint(%(lon{suffix})s::double precision, "
            f"%(lat{suffix})s::double precision), 4326)::geography END"
        )
        return f"({parameters}, {location})"

    # -------------------------------------------------------------- providers

    def load_providers(self, records: Iterable[Any], batch_size: int = 500) -> LoadReport:
        """Upsert care providers (from OSM or open data) into care_infrastructure."""
        report = LoadReport()
        rows_by_source_id: dict[str, dict[str, Any]] = {}

        for record in records:
            params = self._provider_params(record)
            if params is None:
                report.skipped.append(getattr(record, "name", "<unnamed>"))
                continue
            source_id = params["source_id"]
            if source_id in rows_by_source_id:
                report.duplicates += 1
            rows_by_source_id[source_id] = params

        rows = list(rows_by_source_id.values())
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if batch_size > _MAX_PROVIDER_BATCH_SIZE:
            raise ValueError(
                f"batch_size must be at most {_MAX_PROVIDER_BATCH_SIZE} "
                f"({_POSTGRES_MAX_PARAMETERS} PostgreSQL parameters maximum)"
            )

        with self._connect() as conn:
            with conn.cursor() as cur:
                for start in range(0, len(rows), batch_size):
                    batch = rows[start: start + batch_size]
                    values = []
                    query_params = {}
                    for index, params in enumerate(batch):
                        suffix = f"_{index}"
                        values.append(self._provider_values_sql(suffix))
                        query_params.update({f"{key}{suffix}": value for key, value in params.items()})
                    batch_sql = self._upsert_sql(",\n            ".join(values))
                    cur.execute(batch_sql, query_params)
                    for result in cur.fetchall():
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

        # An explicit source_id wins. The dict branch used to derive `osm:…`
        # unconditionally, which hardcoded one source into a loader that is
        # otherwise source-agnostic: a record from any other origin would have
        # been rekeyed as if it came from OpenStreetMap. Harmless until now — the
        # scraper's records carry no source_id — but it blocks re-importing an
        # exported dataset and would have mis-keyed the hospital directory.
        source_id = get("source_id")
        if not source_id and details.get("osm_id"):
            source_id = f"osm:{details.get('osm_type')}/{details.get('osm_id')}"

        if not name or not source_id or "None" in str(source_id):
            return None

        return {
            "source_id": source_id,
            "ik_nummer": get("ik_nummer"),
            "type": get("type"),
            "name": name,
            "parent_organization": get("parent_organization"),
            "website": normalize_website(get("website")),
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
                    source_id = self._resolve_insurer_key(cur, name, ik, report)

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
                        "website": normalize_website(insurer.get("website")),
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
    def _resolve_insurer_key(cur, name: str, ik: str | None, report: LoadReport) -> str:
        """Pick the `source_id` to upsert this insurer on.

        The IK is the identifier worth keying on — it survives renames — but it
        arrives from a network enrichment step that can fail. Deriving the key
        directly from it means the key changes whenever that step flaps, and a
        changed key makes the upsert miss and insert a duplicate instead.
        Observed on 2026-08-10: a run whose IK coverage fell from 91/92 to 75/92
        silently added 16 duplicate insurers and still exited 0.

        So the preferred key is only used when nothing better is already stored:
        an insurer already in the table keeps the key it has, unless this run can
        upgrade it to an IK.
        """
        preferred = f"ik:{ik}" if ik else f"gkv:{name}"

        cur.execute(
            "SELECT 1 FROM care_infrastructure WHERE source_id = %s", (preferred,)
        )
        if cur.fetchone():
            return preferred

        # Not under the preferred key — is this insurer stored under another one?
        # Prefer an IK-keyed row if several somehow share the name.
        cur.execute(
            """
            SELECT source_id FROM care_infrastructure
             WHERE type = 'krankenkasse' AND name = %s
             ORDER BY (source_id LIKE 'ik:%%') DESC, source_id
             LIMIT 1
            """,
            (name,),
        )
        existing = cur.fetchone()
        if existing is None:
            return preferred        # genuinely new insurer

        current = existing["source_id"]

        if ik:
            # Upgrade a name key to an IK, or follow a corrected IK — the
            # official list does revise these between publications.
            cur.execute(
                """
                UPDATE care_infrastructure SET source_id = %(new)s
                 WHERE type = 'krankenkasse' AND name = %(name)s AND source_id = %(old)s
                """,
                {"new": preferred, "name": name, "old": current},
            )
            report.rekeyed += cur.rowcount
            return preferred

        # No IK this run, but the row exists. Keep its key: downgrading to a name
        # key would insert a second row and orphan the history attached to the
        # first. The enrichment failing is not a reason to reshape the data.
        report.key_preserved += 1
        log.warning(
            "no IK resolved for %r this run; keeping existing key %s", name, current
        )
        return current

    def count_insurers_with_ik(self) -> int:
        """How many stored insurers already carry an IK — the regression baseline."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM care_infrastructure "
                    " WHERE type = 'krankenkasse' AND ik_nummer IS NOT NULL"
                )
                return cur.fetchone()["n"]

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
        # NaN as well as None: an insurer that levies no Zusatzbeitrag has a
        # non-numeric cell in the source ("wird nicht erhoben"), which pandas
        # turns into NaN. `NaN is None` is false, and PostgreSQL NUMERIC accepts
        # 'NaN' — so without this the SVLFG would carry a stored rate of NaN
        # instead of no rate at all.
        if rate is None or (isinstance(rate, float) and not math.isfinite(rate)):
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
