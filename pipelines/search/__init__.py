"""Feeding the Typesense index from PostgreSQL (story E2-S2).

Postgres stays the source of truth; Typesense is a derived, disposable index.
That direction is the whole design: anything the index holds can be rebuilt from
the database, so a lost or corrupt index is an inconvenience rather than data
loss.
"""

from pipelines.search.sync import SyncReport, sync_index

__all__ = ["SyncReport", "sync_index"]
