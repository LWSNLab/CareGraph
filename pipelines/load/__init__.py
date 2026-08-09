"""Load stage — write the enriched dataset to its destination.

`postgres_loader.py` is the real destination (story E1-S4): it upserts records
into CareGraph's unified `care_infrastructure` schema plus its satellites
(`krankenkasse_bundesland`, `zusatzbeitrag_historie`). The database is the
single source of truth; the API reads from it and never scrapes on request.

Idempotency is the point — a scheduled re-run updates rows instead of
duplicating them, keyed on the source-namespaced `source_id`
(`osm:node/123`, `ik:108616568`).

`exporter.py` is the earlier prototype exporter. It still emits CSV/JSON and an
SQL script for a *standalone* `krankenkassen` schema, which is useful for
sharing snapshots or seeding a Supabase prototype, but it is not the platform's
storage path.
"""
