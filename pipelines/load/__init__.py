"""Load stage — write the enriched dataset to its destination.

`exporter.py` is the ported GKV prototype exporter. It emits CSV/JSON and an
idempotent SQL upsert script for a STANDALONE ``krankenkassen`` schema (the
Supabase prototype), incl. a normalized ``bundeslaender`` + ``krankenkasse_bundesland``.

⚠️ This is NOT yet CareGraph's unified schema. CareGraph stores everything in
``care_infrastructure`` (+ ``krankenkasse_bundesland``, ``zusatzbeitrag_historie``)
— see db/migrations/0001_init.sql. Mapping insurers into that schema (type =
'krankenkasse', address → location via geocoding, zusatzbeitrag → history) and a
direct Postgres load is the follow-up integration step.
"""
