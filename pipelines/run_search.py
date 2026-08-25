"""Rebuild the Typesense search index from PostgreSQL (story E2-S2).

    python -m pipelines.run_search sync

Run it after any ingestion. The index is derived state: everything in it can be
rebuilt from the database, which is why a full rebuild is preferred over an
incremental sync at this size — see pipelines/search/sync.py.

Exits non-zero on failure so a scheduled run can alert.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from pipelines.common import DSNError, checked_dsn, dsn_from_env, use_system_trust_store
from pipelines.search import sync_index

log = logging.getLogger("pipelines.run_search")



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["sync"])
    ap.add_argument("--url", default=os.environ.get("TYPESENSE_URL", "http://localhost:8108"))
    ap.add_argument("--api-key", default=os.environ.get("TYPESENSE_API_KEY", "devkey"))
    ap.add_argument("--keep", type=int, default=1,
                    help="superseded collections to retain for a manual rollback")
    ap.add_argument(
        "--dsn",
        default=dsn_from_env(),
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
        report = sync_index(dsn, args.url, args.api_key, args.keep)
    except Exception:
        log.exception("search index rebuild failed")
        return 1

    print(f"✅ {report.summary()}")
    if not report.ok:
        log.error("%d document(s) were rejected", len(report.dropped))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
