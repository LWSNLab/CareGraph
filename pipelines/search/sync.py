"""Rebuild the Typesense index from PostgreSQL.

**A full rebuild behind an alias, not an incremental sync.** The obvious design
is to track `updated_at` and push changes, and at a few million documents that
would be right. At 9,192 it is not: a whole rebuild takes about a second, while
incremental sync introduces the one failure this index must not have — silent
drift, where a deleted row lives on in search results and nobody notices because
nothing errored.

The rebuild writes into a fresh collection and only then moves the alias, so:

* readers never see a half-built index — the alias flips atomically;
* a failed rebuild leaves the previous index serving, rather than an empty one;
* re-running is inherently idempotent, because the result depends on the
  database and not on what the index happened to contain.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import requests
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

ALIAS = "providers"
BATCH_SIZE = 1000

# Only rows a search can return. Insurers have no coordinates and are found
# through their own routes, so indexing them would put results in the same list
# that behave differently from every other hit.
INDEXED_TYPES = (
    "pflegedienst_ambulant",
    "pflegeheim_stationaer",
    "pflegestuetzpunkt",
    "krankenhaus",
)

SELECT_SQL = f"""
SELECT source_id, name, type::text AS type, parent_organization,
       strasse, plz, ort, bundesland,
       ST_Y(location::geometry) AS lat,
       ST_X(location::geometry) AS lon
  FROM care_infrastructure
 WHERE type::text IN ({", ".join(f"'{t}'" for t in INDEXED_TYPES)})
   AND location IS NOT NULL
 ORDER BY source_id
"""

# `locale: de` matters: without it Typesense tokenises umlauts as separate
# characters, so "Krankenhaus Münster" does not match a search for "Munster".
SCHEMA_FIELDS = [
    {"name": "name", "type": "string", "locale": "de"},
    {"name": "ort", "type": "string", "locale": "de", "optional": True, "facet": True},
    {"name": "strasse", "type": "string", "locale": "de", "optional": True},
    {"name": "plz", "type": "string", "optional": True, "facet": True},
    {"name": "bundesland", "type": "string", "optional": True, "facet": True},
    {"name": "parent_organization", "type": "string", "locale": "de", "optional": True},
    {"name": "type", "type": "string", "facet": True},
    {"name": "location", "type": "geopoint", "optional": True},
]


@dataclass
class SyncReport:
    """Outcome of one rebuild."""

    collection: str
    documents: int
    duration_s: float
    dropped: list[str]

    @property
    def ok(self) -> bool:
        return not self.dropped

    def summary(self) -> str:
        return (f"{self.documents} documents into {self.collection} "
                f"in {self.duration_s:.1f}s, dropped={len(self.dropped)}")


class TypesenseError(RuntimeError):
    """Typesense refused a request."""


class TypesenseClient:
    """Thin HTTP client. Raw requests rather than the SDK: this needs four calls
    and the project keeps its dependency list short."""

    def __init__(self, url: str, api_key: str, timeout: int = 60):
        self.url = url.rstrip("/")
        self.headers = {"X-TYPESENSE-API-KEY": api_key}
        self.timeout = timeout

    def _call(self, method: str, path: str, **kwargs):
        # Merged, not passed alongside: a caller adding Content-Type would
        # otherwise collide with the api-key header on the same argument.
        headers = {**self.headers, **kwargs.pop("headers", {})}
        response = requests.request(
            method, f"{self.url}{path}", headers=headers, timeout=self.timeout, **kwargs
        )
        if response.status_code >= 400:
            raise TypesenseError(
                f"{method} {path} → {response.status_code}: {response.text[:200]}")
        return response

    def health(self) -> bool:
        try:
            return self._call("GET", "/health").json().get("ok", False)
        except (TypesenseError, requests.RequestException):
            return False

    def create_collection(self, name: str) -> None:
        self._call("POST", "/collections",
                   json={"name": name, "fields": SCHEMA_FIELDS, "enable_nested_fields": False})

    def import_documents(self, name: str, documents: list[dict]) -> list[str]:
        """Import a batch, returning the ids Typesense rejected."""
        payload = "\n".join(json.dumps(d, ensure_ascii=False) for d in documents)
        response = self._call(
            "POST", f"/collections/{name}/documents/import?action=upsert",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        failures = []
        for line, document in zip(response.text.splitlines(), documents, strict=False):
            try:
                if not json.loads(line).get("success", False):
                    failures.append(document.get("id", "<no id>"))
            except json.JSONDecodeError:
                failures.append(document.get("id", "<no id>"))
        return failures

    def upsert_alias(self, alias: str, collection: str) -> None:
        self._call("PUT", f"/aliases/{alias}", json={"collection_name": collection})

    def list_collections(self) -> list[str]:
        return [c["name"] for c in self._call("GET", "/collections").json()]

    def drop_collection(self, name: str) -> None:
        self._call("DELETE", f"/collections/{name}")

    def document_count(self, name: str) -> int:
        return self._call("GET", f"/collections/{name}").json().get("num_documents", 0)


def sync_index(
    dsn: str, url: str, api_key: str, keep: int = 1, alias: str = ALIAS
) -> SyncReport:
    """Rebuild the index and point `alias` at it.

    `keep` older collections are retained so an operator can roll the alias back
    by hand if a rebuild turns out to have indexed something wrong.

    `alias` is a parameter so tests can publish somewhere else. They share a
    Typesense instance with development, and an earlier version wrote to the
    production alias — a test run left the developer's index holding five seeded
    rows, and search silently returned nothing until someone looked at `out_of`.
    """
    client = TypesenseClient(url, api_key)
    if not client.health():
        raise TypesenseError(f"Typesense at {url} is not reachable")

    # Random suffix, not just a timestamp: two runs inside the same second
    # collided with a 409 and left the first one's collection behind.
    collection = f"{alias}_{datetime.now(UTC):%Y%m%d%H%M%S}_{secrets.token_hex(3)}"
    started = time.perf_counter()
    client.create_collection(collection)

    try:
        documents, dropped, batch = 0, [], []
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            with conn.cursor(name="search_sync") as cur:
                cur.execute(SELECT_SQL)
                for row in cur:
                    batch.append(_document(row))
                    if len(batch) >= BATCH_SIZE:
                        dropped += client.import_documents(collection, batch)
                        documents += len(batch)
                        batch = []
        if batch:
            dropped += client.import_documents(collection, batch)
            documents += len(batch)
    except Exception:
        # A half-built collection is never published, so it is pure litter — and
        # it accumulates on every retry until someone notices the disk.
        _drop_quietly(client, collection)
        raise

    if documents == 0:
        # Swapping to an empty index would take search down and look like a
        # successful run. Leave the alias where it is and fail loudly.
        _drop_quietly(client, collection)
        raise TypesenseError(
            "no indexable rows found — refusing to publish an empty index. "
            "Has the ingestion run?"
        )

    client.upsert_alias(alias, collection)
    _prune(client, collection, keep, alias)

    report = SyncReport(collection, documents, time.perf_counter() - started, dropped)
    if dropped:
        log.error("Typesense rejected %d documents: %s", len(dropped), dropped[:5])
    log.info("search index rebuilt: %s", report.summary())
    return report


def _drop_quietly(client: TypesenseClient, collection: str) -> None:
    """Remove a collection that was never published; failure here is not fatal."""
    try:
        client.drop_collection(collection)
    except TypesenseError as err:
        log.warning("could not clean up %s: %s", collection, err)


def _document(row: dict) -> dict:
    """One database row as a Typesense document."""
    document = {
        # The source_id is the id: stable, unique, and the same key the API uses
        # to fetch the full record from Postgres afterwards.
        "id": row["source_id"],
        "name": row["name"],
        "type": row["type"],
    }
    for field in ("parent_organization", "strasse", "plz", "ort", "bundesland"):
        if row.get(field):
            document[field] = row[field]
    if row.get("lat") is not None and row.get("lon") is not None:
        # Typesense geopoints are [lat, lng] — the opposite order from PostGIS,
        # which takes (lon, lat). Getting this backwards puts every German
        # facility in the Indian Ocean.
        document["location"] = [row["lat"], row["lon"]]
    return document


def _prune(client: TypesenseClient, current: str, keep: int, alias: str = ALIAS) -> None:
    """Drop superseded collections, keeping the newest `keep` for rollback."""
    older = sorted(
        (c for c in client.list_collections()
         if c.startswith(f"{alias}_") and c != current),
        reverse=True,
    )
    for name in older[keep:]:
        try:
            client.drop_collection(name)
            log.debug("dropped superseded collection %s", name)
        except TypesenseError as err:
            # Not fatal: the alias already points at the new index, so leftover
            # collections cost disk, not correctness.
            log.warning("could not drop %s: %s", name, err)
