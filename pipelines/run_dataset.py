"""Export and import the distributable provider dataset (story E4-S5).

    python -m pipelines.run_dataset export --out dist/caregraph-providers.tar.gz
    python -m pipelines.run_dataset import --file dist/caregraph-providers.tar.gz

Exists so a self-hoster gets data: starting the stack and applying the migrations
leaves an empty database, and rebuilding it from source means minutes of Overpass
calls. Exits non-zero on failure so a release job can tell.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from pipelines.common import DSNError, checked_dsn, dsn_from_env, use_system_trust_store
from pipelines.dataset import export_dataset, import_dataset

log = logging.getLogger("pipelines.run_dataset")



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["export", "import"])
    ap.add_argument("--out", type=Path, help="archive to write (export)")
    ap.add_argument("--file", type=Path, help="archive to read (import)")
    ap.add_argument("--migrations", type=Path, default=Path("db/migrations"))
    ap.add_argument("--allow-schema-mismatch", action="store_true",
                    help="import an archive cut against a different migration")
    ap.add_argument(
        "--dsn",
        default=dsn_from_env(),
        help="Postgres DSN (defaults to $INGEST_DATABASE_URL)",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    use_system_trust_store()

    # Vet the DSN before anything connects: a missing variable or a plaintext
    # connection to a remote host should stop here, not halfway through a load.
    try:
        dsn = checked_dsn(args.dsn)
    except DSNError as exc:
        log.error("%s", exc)
        return 1

    try:
        if args.what == "export":
            out = args.out or Path(
                f"dist/caregraph-providers-{datetime.now(UTC):%Y-%m-%d}.tar.gz")
            result = export_dataset(dsn, out, args.migrations)
            print(f"✅ {result.summary()}")
            print(f"   {result.path}")
        else:
            if not args.file:
                ap.error("import needs --file")
            result = import_dataset(dsn, args.file, args.migrations,
                                    args.allow_schema_mismatch)
            print(f"✅ {result.summary()}")
            print(f"   {result.manifest['attribution']}")
            if not result.report.ok:
                log.error("skipped %d record(s)", len(result.report.skipped))
                return 1
    except Exception:
        log.exception("dataset %s failed", args.what)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
