"""Export the provider dataset as a redistributable archive."""

from __future__ import annotations

import csv
import io
import json
import logging
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

# Providers only. The insurer rows are re-derived from a GKV publication whose
# redistribution terms are not settled, and mixing sources in one archive would
# make the file inherit the strictest of them. `make load-insurers` covers that
# half from the official source. See E4-S5.
EXPORT_WHERE = "type <> 'krankenkasse'"

# Column order is the loader's record shape, so an import is a straight mapping.
COLUMNS = (
    "source_id", "ik_nummer", "type", "name", "parent_organization",
    "website", "strasse", "plz", "ort", "bundesland", "details", "lat", "lon",
)

EXPORT_SQL = f"""
SELECT source_id, ik_nummer, type::text AS type, name, parent_organization,
       website, strasse, plz, ort, bundesland,
       details::text AS details,
       ST_Y(location::geometry) AS lat,
       ST_X(location::geometry) AS lon
  FROM care_infrastructure
 WHERE {EXPORT_WHERE}
 ORDER BY source_id
"""

LICENCE_NOTICE = """\
CareGraph provider dataset
==========================

This dataset is derived from OpenStreetMap and is therefore made available under
the Open Database License (ODbL) v1.0.

    https://opendatacommons.org/licenses/odbl/1-0/

Required attribution, to be reproduced by anyone using or redistributing this
data or any work produced from it:

    © OpenStreetMap contributors (ODbL)

Under the ODbL you are free to share and adapt this database, provided you
attribute as above, keep this notice intact, and license any Derivative Database
under the same terms.

Individual records carry their own `attribution` field inside `details`; those
values are authoritative for the record they belong to.
"""

README = """\
# CareGraph provider dataset

Care providers in Germany — outpatient services, nursing homes and
Pflegestützpunkte — as loaded into CareGraph.

## What this is

    {row_count} records, cut on {generated_at}
    Schema as of migration: {schema_migration}

Every record carries a name and coordinates. Roughly a third have no full street
address: their OpenStreetMap objects have no `addr:*` tags.

## What this is not

- **No Institutionskennzeichen.** No public source publishes provider IKs; the
  field exists but is empty for every row here.
- **Not the insurers.** Statutory insurers are loaded separately from the
  official GKV publication — see `make load-insurers`.
- **Not deduplicated.** The same facility may appear twice if OpenStreetMap
  holds two objects for it.
- **A snapshot.** It ages from the moment it was cut. Re-run the ingestion for
  current data.

## Loading it

    make migrate
    make dataset-import FILE=<this archive>

The import is idempotent: running it twice updates rows rather than duplicating
them.

## Licence

ODbL v1.0. See LICENSE.txt in this archive — attribution is required.
"""


@dataclass
class ExportResult:
    """What an export produced."""

    path: Path
    row_count: int
    generated_at: str
    schema_migration: str

    def summary(self) -> str:
        size = self.path.stat().st_size / 1024 / 1024
        return (f"{self.row_count} rows → {self.path.name} "
                f"({size:.1f} MB, schema {self.schema_migration})")


def latest_migration(migrations_dir: Path) -> str:
    """The newest migration file name, recorded so a restore can be checked.

    An archive restored against an older schema fails in confusing ways; naming
    the migration it was cut at turns that into a readable mismatch.
    """
    files = sorted(p.name for p in migrations_dir.glob("*.sql"))
    return files[-1] if files else "unknown"


def export_dataset(
    dsn: str,
    out_path: Path,
    migrations_dir: Path = Path("db/migrations"),
) -> ExportResult:
    """Write a .tar.gz holding the provider CSV, a manifest, licence and README."""
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d")
    schema_migration = latest_migration(migrations_dir)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)

    rows = 0
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # Server-side cursor: the table is small today, but streaming keeps this
        # honest once the hospital directory triples the row count.
        with conn.cursor(name="dataset_export") as cur:
            cur.execute(EXPORT_SQL)
            for record in cur:
                writer.writerow([record[column] for column in COLUMNS])
                rows += 1

    if rows == 0:
        raise ValueError(
            "no provider rows found — refusing to write an empty dataset. "
            "Has the ingestion run?"
        )

    manifest = {
        "name": "caregraph-providers",
        "generated_at": generated_at,
        "row_count": rows,
        "schema_migration": schema_migration,
        "licence": "ODbL-1.0",
        "licence_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "columns": list(COLUMNS),
        "contains": "care providers only; statutory insurers are excluded",
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as archive:
        _add(archive, "providers.csv", buffer.getvalue())
        _add(archive, "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
        _add(archive, "LICENSE.txt", LICENCE_NOTICE)
        _add(archive, "README.md", README.format(**manifest))

    result = ExportResult(out_path, rows, generated_at, schema_migration)
    log.info("dataset exported: %s", result.summary())
    return result


def _add(archive: tarfile.TarFile, name: str, content: str) -> None:
    """Add a file with a fixed mtime so identical data yields identical bytes."""
    data = content.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))
