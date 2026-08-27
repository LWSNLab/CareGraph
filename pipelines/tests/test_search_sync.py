"""Tests for the Typesense sync worker (story E2-S2).

The unit cases need no Typesense; the integration cases skip without one:

    docker compose up -d typesense
    CAREGRAPH_TEST_TYPESENSE=http://localhost:8108 uv run --project pipelines pytest
"""

from __future__ import annotations

import os

import pytest

from pipelines.search.sync import (
    INDEXED_TYPES,
    SCHEMA_FIELDS,
    TypesenseClient,
    TypesenseError,
    _document,
    sync_index,
)
from pipelines.tests.conftest import SEED_PREFIX  # noqa: F401  (fixture module)

TYPESENSE_URL = os.environ.get("CAREGRAPH_TEST_TYPESENSE")
DSN = os.environ.get("CAREGRAPH_TEST_DSN")
integration = pytest.mark.skipif(
    not (TYPESENSE_URL and DSN), reason="CAREGRAPH_TEST_TYPESENSE/DSN not set")

# Never the production alias. These tests share a Typesense instance with
# development, and publishing to `providers` replaced a real 9,099-document
# index with five seeded rows — search then returned nothing, with a 200.
TEST_ALIAS = "providers_pytest"

ROW = {
    "source_id": "osm:node/1",
    "name": "Pflegeheim Münster",
    "type": "pflegeheim_stationaer",
    "parent_organization": "Caritas",
    "strasse": "Hauptstraße 1",
    "plz": "48143",
    "ort": "Münster",
    "bundesland": "Nordrhein-Westfalen",
    "lat": 51.96,
    "lon": 7.62,
}


# --------------------------------------------------------------- mapping

def test_geopoint_is_lat_lng_not_lng_lat():
    """Typesense takes [lat, lng]; PostGIS takes (lon, lat).

    Getting this backwards is silent — the document indexes fine and every
    German facility ends up in the Indian Ocean.
    """
    document = _document(ROW)
    lat, lng = document["location"]
    assert lat == pytest.approx(51.96)
    assert lng == pytest.approx(7.62)
    assert 47 < lat < 56 and 5 < lng < 16, "coordinates left Germany"


def test_the_source_id_becomes_the_document_id():
    # The API resolves a hit back to the full row in Postgres by this key.
    assert _document(ROW)["id"] == "osm:node/1"


def test_absent_optional_fields_are_omitted_not_blanked():
    sparse = {**ROW, "strasse": None, "plz": "", "parent_organization": None}
    document = _document(sparse)
    for field in ("strasse", "plz", "parent_organization"):
        assert field not in document, f"{field} was indexed as an empty value"


def test_import_rejects_a_truncated_typesense_response(monkeypatch):
    class Response:
        text = '{"success":true}\n'
        status_code = 200

    monkeypatch.setattr("pipelines.search.sync.requests.request", lambda *a, **kw: Response())
    client = TypesenseClient("http://typesense", "key")

    with pytest.raises(TypesenseError, match="returned 1 results for 2 documents"):
        client.import_documents("providers_test", [{"id": "1"}, {"id": "2"}])


@integration
def test_count_mismatch_never_publishes_the_new_collection(client, monkeypatch, seeded_providers):
    key = os.environ.get("CAREGRAPH_TEST_TYPESENSE_KEY", "devkey")
    monkeypatch.setattr(TypesenseClient, "document_count", lambda self, name: 0)

    with pytest.raises(TypesenseError, match="expected"):
        sync_index(DSN, TYPESENSE_URL, key, alias=TEST_ALIAS)


def test_a_row_without_coordinates_still_indexes():
    document = _document({**ROW, "lat": None, "lon": None})
    assert "location" not in document
    assert document["name"] == "Pflegeheim Münster"


def test_german_locale_is_set_on_text_fields():
    """Without it umlauts tokenise oddly and "Munster" misses "Münster"."""
    by_name = {f["name"]: f for f in SCHEMA_FIELDS}
    for field in ("name", "ort", "strasse", "parent_organization"):
        assert by_name[field].get("locale") == "de", f"{field} lacks the German locale"


def test_insurers_are_not_indexed():
    """They carry no coordinates and are reached by IK, so a hit for one would
    behave differently from every other result in the same list."""
    assert "krankenkasse" not in INDEXED_TYPES
    assert "krankenhaus" in INDEXED_TYPES


# ----------------------------------------------------------- integration

@pytest.fixture
def client():
    c = TypesenseClient(TYPESENSE_URL, os.environ.get("CAREGRAPH_TEST_TYPESENSE_KEY", "devkey"))
    if not c.health():
        pytest.skip("Typesense not reachable")
    return c


@integration
def test_sync_publishes_an_alias_and_prunes_old_collections(client, seeded_providers):
    key = os.environ.get("CAREGRAPH_TEST_TYPESENSE_KEY", "devkey")

    first = sync_index(DSN, TYPESENSE_URL, key, keep=1, alias=TEST_ALIAS)
    second = sync_index(DSN, TYPESENSE_URL, key, keep=1, alias=TEST_ALIAS)

    assert second.collection != first.collection, "rebuild reused a collection name"
    assert second.documents == first.documents
    assert second.documents >= len(seeded_providers)
    assert second.ok

    # The alias must point at the newest, or readers keep seeing stale data.
    aliases = client._call("GET", "/aliases").json()["aliases"]
    current = next(a for a in aliases if a["name"] == TEST_ALIAS)
    assert current["collection_name"] == second.collection

    # keep=1 retains exactly one superseded collection for a manual rollback.
    kept = [c for c in client.list_collections() if c.startswith(f"{TEST_ALIAS}_")]
    assert len(kept) == 2, kept


@integration
def test_a_failed_rebuild_leaves_no_orphan_collection(client, monkeypatch, seeded_providers):
    """A half-built collection is litter that accumulates on every retry."""
    before = set(client.list_collections())

    def explode(self, name, documents):
        raise TypesenseError("import failed")

    monkeypatch.setattr(TypesenseClient, "import_documents", explode)
    with pytest.raises(TypesenseError):
        sync_index(DSN, TYPESENSE_URL,
                   os.environ.get("CAREGRAPH_TEST_TYPESENSE_KEY", "devkey"),
                   alias=TEST_ALIAS)

    assert set(client.list_collections()) == before, "a partial collection was left behind"


@integration
def test_an_empty_result_never_replaces_a_working_index(client, monkeypatch, seeded_providers):
    """Publishing an empty index would take search down and report success."""
    key = os.environ.get("CAREGRAPH_TEST_TYPESENSE_KEY", "devkey")
    sync_index(DSN, TYPESENSE_URL, key, alias=TEST_ALIAS)
    aliases = client._call("GET", "/aliases").json()["aliases"]
    before = next(a for a in aliases if a["name"] == TEST_ALIAS)["collection_name"]

    monkeypatch.setattr("pipelines.search.sync.SELECT_SQL",
                        "SELECT source_id, name, type::text AS type, parent_organization,"
                        " strasse, plz, ort, bundesland, NULL::float AS lat, NULL::float AS lon"
                        " FROM care_infrastructure WHERE false")
    with pytest.raises(TypesenseError, match="refusing to publish an empty index"):
        sync_index(DSN, TYPESENSE_URL, key, alias=TEST_ALIAS)

    aliases = client._call("GET", "/aliases").json()["aliases"]
    after = next(a for a in aliases if a["name"] == TEST_ALIAS)["collection_name"]
    assert after == before, "the alias moved to an empty index"


@integration
def test_an_unreachable_typesense_is_reported_not_swallowed():
    with pytest.raises(TypesenseError, match="not reachable"):
        sync_index(DSN, "http://127.0.0.1:1", "devkey", alias=TEST_ALIAS)
