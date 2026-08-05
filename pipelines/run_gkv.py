"""GKV ingestion orchestrator (roadmap Milestone 1.1).

Parse the official GKV insurer PDF → optionally enrich with impressum addresses
→ export CSV/JSON/SQL.

Run from the repository root:

    python -m pipelines.run_gkv data/raw/gkv_liste_2026.pdf
    python -m pipelines.run_gkv <URL> --year 2026 --no-scrape
"""

import argparse

from pipelines.parsers.gkv_parser import GKVParser
from pipelines.scrapers.address_scraper import AddressScraper
from pipelines.load.exporter import DataExporter


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse, enrich and export the GKV insurer list.")
    ap.add_argument("source", help="Path or URL to the GKV list PDF")
    ap.add_argument("--year", type=int, default=2026, help="Version year for output filenames")
    ap.add_argument("--no-scrape", action="store_true", help="Skip address scraping (fast)")
    ap.add_argument("--out", default="data/processed", help="Output directory")
    args = ap.parse_args()

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
    print("🎉 Fertig.")


if __name__ == "__main__":
    main()
