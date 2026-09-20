from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from pipelines.common import (
    PACKAGE_ROOT,
    DSNError,
    checked_dsn,
    dsn_from_env,
    use_system_trust_store,
)
from pipelines.geocoding.backfill import backfill_addresses
from pipelines.geocoding.cache import DEFAULT_PATH, GeocodeCache
from pipelines.geocoding.nominatim import (
    MIN_INTERVAL_S,
    PUBLIC_ENDPOINT,
    NominatimClient,
)

log = logging.getLogger("pipelines.run_geocode")

def user_agent() -> str:
    """Identify the application, as the usage policy requires.

    The version is read rather than written out: a User-Agent claiming 1.0 from a
    0.2.0 build is a small lie, and the one person it misleads is the operator
    trying to work out whose traffic this is. The repository rather than an
    address, because it is durable and says who to contact.
    """
    try:
        version = (PACKAGE_ROOT.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "dev"
    return f"CareGraph/{version} (+https://github.com/LWSNLab/CareGraph)"

# Small enough to stay a polite guest on the public instance. Lifting it is a
# decision, which is why it takes a flag rather than a larger number.
DEFAULT_LIMIT = 200


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("what", choices=["backfill"])
    ap.add_argument("--dsn", default=dsn_from_env())
    ap.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"rows to process in this run (default {DEFAULT_LIMIT})",
    )
    ap.add_argument(
        "--all", action="store_true",
        help="process every candidate — intended for a self-hosted --base-url",
    )
    ap.add_argument("--base-url", default=os.environ.get("NOMINATIM_URL", PUBLIC_ENDPOINT))
    ap.add_argument(
        "--min-interval", type=float, default=MIN_INTERVAL_S,
        help="seconds between requests; below 1.0 only against your own instance",
    )
    ap.add_argument("--cache", default=str(DEFAULT_PATH))
    ap.add_argument(
        "--dry-run", action="store_true",
        help="look up and report, write no rows (the cache is still filled)",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    use_system_trust_store()

    if args.all and args.base_url == PUBLIC_ENDPOINT:
        log.error(
            "--all against the public Nominatim is the bulk use its policy asks "
            "people not to make. Run it against a self-hosted instance "
            "(--base-url), or work through the backlog with --limit."
        )
        return 2

    if args.min_interval < MIN_INTERVAL_S and args.base_url == PUBLIC_ENDPOINT:
        log.error(
            "--min-interval below %.1fs exceeds the public instance's rate limit",
            MIN_INTERVAL_S,
        )
        return 2

    try:
        dsn = checked_dsn(args.dsn)
    except DSNError as exc:
        log.error("%s", exc)
        return 1

    cache = GeocodeCache(Path(args.cache))
    log.info("geocode cache holds %d coordinate(s)", len(cache))

    client = NominatimClient(
        user_agent=user_agent(),
        base_url=args.base_url,
        min_interval_s=args.min_interval,
    )

    try:
        report = backfill_addresses(
            dsn,
            client,
            cache,
            limit=None if args.all else args.limit,
            dry_run=args.dry_run,
        )
    except Exception:
        log.exception("address backfill failed")
        return 1

    prefix = "would fill" if args.dry_run else "filled"
    print(f"✅ {prefix}: {report.summary()}")
    if not report.ok:
        log.error("%d row(s) could not be looked up", len(report.failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
