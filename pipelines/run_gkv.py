"""GKV ingestion orchestrator (roadmap Milestone 1.1).

Parse the official GKV insurer PDF → optionally enrich with impressum addresses
→ export CSV/JSON/SQL.

Run from the repository root:

    python -m pipelines.run_gkv data/raw/gkv_liste_2026.pdf
    python -m pipelines.run_gkv <URL> --year 2026 --no-scrape

Exits non-zero on failure, so a scheduler can tell a bad run from a good one.
"""

from __future__ import annotations

import argparse
import logging
import sys

from pipelines.common import use_system_trust_store
from pipelines.load.exporter import DataExporter
from pipelines.parsers.gkv_parser import GKVParser
from pipelines.scrapers.address_scraper import AddressScraper

log = logging.getLogger("pipelines.run_gkv")


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse, enrich and export the GKV insurer list.")
    ap.add_argument("source", help="Path or URL to the GKV list PDF")
    ap.add_argument("--year", type=int, default=2026, help="Version year for output filenames")
    ap.add_argument("--no-scrape", action="store_true", help="Skip address scraping (fast)")
    ap.add_argument("--out", default="data/processed", help="Output directory")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    # The PDF may be fetched by URL and the impressum scraper hits ~93 hosts.
    use_system_trust_store()

    try:
        df = GKVParser(args.source, output_filename=f"gkv_liste_{args.year}.pdf").parse_pdf()
        print(f"✅ Geparst: {len(df)} Krankenkassen")

        if args.no_scrape:
            for col in ("strasse", "plz", "ort", "scraping_status"):
                df[col] = ""
        else:
            df = AddressScraper().enrich_dataframe(df)

        exporter = DataExporter(output_dir=args.out)
        exporter.export_csv(df, f"krankenkassen_{args.year}.csv")
        exporter.export_json(df, f"krankenkassen_{args.year}.json")
        exporter.export_sql(df, f"krankenkassen_{args.year}.sql")
    except Exception:
        # log.exception, not log.error: without the traceback an unexpected
        # failure in a scheduled run leaves a one-line message and no way back
        # to the line that raised.
        log.exception("GKV ingestion failed")
        return 1

    print("🎉 Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
