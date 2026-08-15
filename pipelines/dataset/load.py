"""Read a distributed dataset archive back into the database."""

from __future__ import annotations

import csv
import io
import json
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path

from pipelines.dataset.export import COLUMNS, latest_migration
from pipelines.load.postgres_loader import LoadReport, PostgresLoader

log = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Outcome of an import, including what the archive said about itself."""

    manifest: dict
    report: LoadReport

    def summary(self) -> str:
        return (f"{self.manifest.get('name', 'dataset')} "
                f"cut {self.manifest.get('generated_at', '?')}: {self.report.summary()}")


def import_dataset(
    dsn: str,
    archive_path: Path,
    migrations_dir: Path = Path("db/migrations"),
    allow_schema_mismatch: bool = False,
) -> ImportResult:
    """Load an exported archive through the ordinary provider loader.

    Deliberately routed through `PostgresLoader.load_providers` rather than a
    direct `COPY`: the loader is idempotent, normalises website URLs and applies
    the same key resolution as the ingestion. An import that bypassed it would be
    a second way into the database with its own rules — and a second thing to
    keep in step.
    """
    manifest, records = read_archive(archive_path)

    cut_at = manifest.get("schema_migration", "unknown")
    current = latest_migration(migrations_dir)
    if cut_at != current:
        message = (
            f"archive was cut against schema {cut_at}, this checkout is at {current}. "
            "Restoring across a schema change can fail in confusing ways."
        )
        if not allow_schema_mismatch:
            raise ValueError(message + " Pass --allow-schema-mismatch to try anyway.")
        log.warning("%s Continuing because it was explicitly allowed.", message)

    loader = PostgresLoader(dsn)
    report = loader.load_providers(records)
    result = ImportResult(manifest, report)
    log.info("dataset imported: %s", result.summary())
    return result


def read_archive(archive_path: Path) -> tuple[dict, list[dict]]:
    """Return the manifest and the provider records held in an archive."""
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = json.loads(_read(archive, "MANIFEST.json"))
        rows = list(csv.DictReader(io.StringIO(_read(archive, "providers.csv"))))

    missing = set(COLUMNS) - set(rows[0]) if rows else set(COLUMNS)
    if missing:
        raise ValueError(f"archive is missing columns: {', '.join(sorted(missing))}")

    records = [_record(row) for row in rows]
    if len(records) != manifest.get("row_count"):
        raise ValueError(
            f"archive claims {manifest.get('row_count')} rows but holds {len(records)} — "
            "it is truncated or was tampered with"
        )
    return manifest, records


def _read(archive: tarfile.TarFile, name: str) -> str:
    # extractfile raises KeyError for an absent member and returns None for a
    # directory or symlink. Both mean the same thing to a caller who pointed at
    # the wrong file, and both should say so rather than surfacing a KeyError.
    try:
        member = archive.extractfile(name)
    except KeyError:
        member = None
    if member is None:
        raise ValueError(f"archive has no {name} — is this a CareGraph dataset?")
    return member.read().decode("utf-8")


def _record(row: dict[str, str]) -> dict:
    """Turn one CSV row back into the loader's record shape.

    CSV has no types: every absent value arrives as an empty string, which would
    be written as an empty address rather than a missing one.
    """
    record = {key: (row.get(key) or None) for key in COLUMNS}
    record["details"] = json.loads(row["details"]) if row.get("details") else {}
    for coordinate in ("lat", "lon"):
        record[coordinate] = float(row[coordinate]) if row.get(coordinate) else None
    return record
