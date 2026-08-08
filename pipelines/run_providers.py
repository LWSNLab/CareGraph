"""Care-provider ingestion runner (story E1-S2).

Fetches care providers from OpenStreetMap via Overpass, per federal state, and
writes them to a JSON file for the loader (E1-S4) to pick up.

Exits non-zero when any region failed, so a scheduler/CI job can alert on it.

Run from the repository root:

    python -m pipelines.run_providers --bundesland Bremen
    python -m pipelines.run_providers --all --out pipelines/data/processed/providers.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from pipelines.scrapers.osm_provider_scraper import BUNDESLAENDER, OSMProviderScraper

log = logging.getLogger("pipelines.run_providers")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest care providers from OpenStreetMap.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--bundesland", action="append", help="Federal state (repeatable)")
    group.add_argument("--all", action="store_true", help="All 16 federal states")
    ap.add_argument("--out", default="pipelines/data/processed/providers.json", help="Output JSON file")
    ap.add_argument("--delay", type=float, default=3.0, help="Pause between regions (be polite to Overpass)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    regions = list(BUNDESLAENDER) if args.all else args.bundesland
    scraper = OSMProviderScraper(delay=args.delay)
    records, report = scraper.fetch(regions)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    print()
    print(f"💾 {len(records)} providers -> {out_path}")
    for provider_type, count in Counter(r.type for r in records).most_common():
        print(f"   {count:5}  {provider_type}")

    geocoded = sum(1 for r in records if r.lat is not None)
    addressed = sum(1 for r in records if r.strasse and r.plz and r.ort)
    print(f"   coordinates: {geocoded}/{len(records)} · full address: {addressed}/{len(records)}")

    if not report.ok:
        # Non-zero exit makes the failure alertable from cron/CI (E1-S2 AC4).
        log.error("regions failed: %s", ", ".join(sorted(report.failed)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
