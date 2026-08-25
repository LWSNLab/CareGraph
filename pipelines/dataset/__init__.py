"""Packaging the loaded dataset for distribution (story E4-S5).

A self-hoster who starts the stack gets a schema and no rows: both ingestion
inputs are gitignored, and rebuilding them means minutes of Overpass calls. This
package produces a redistributable artefact from the database and reads it back.

Exported from the **database**, not from the scraper's output: the database is
the source of truth (E1-S4), so anything added after ingestion — normalised
website URLs today, backfilled addresses later — travels with the artefact
instead of silently diverging from what the API serves.
"""

from pipelines.dataset.export import ExportResult, export_dataset
from pipelines.dataset.load import ImportResult, import_dataset

__all__ = ["ExportResult", "ImportResult", "export_dataset", "import_dataset"]
