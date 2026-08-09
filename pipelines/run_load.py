"""Load ingested data into PostgreSQL/PostGIS (story E1-S4).

This is the step that makes the database the source of truth. It is meant to be
run on a schedule (cron/CI), not per request:

    # providers collected by run_providers.py
    python -m pipelines.run_load providers --input pipelines/data/processed/providers.json

    # statutory insurers straight from the official PDF
    python -m pipelines.run_load insurers --pdf pipelines/data/raw/gkv_liste_2026.pdf \\
        --gueltig-ab 2026-07-26

Both are idempotent: re-running updates rows instead of duplicating them.
Exits non-zero when records had to be skipped, so a scheduler can alert.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from pipelines.common import parse_bundeslaender
from pipelines.load.postgres_loader import PostgresLoader

log = logging.getLogger("pipelines.run_load")

DEFAULT_DSN = "postgres://caregraph_ingest:devingest@localhost:5433/caregraph?sslmode=disable"


def load_providers(loader: PostgresLoader, path: Path):
    records = json.loads(path.read_text(encoding="utf-8"))
    print(f"📥 {len(records)} providers from {path}")
    return loader.load_providers(records)


def load_insurers(loader: PostgresLoader, pdf: Path, gueltig_ab: date, expand: bool):
    # Imported lazily so the provider path does not need pdfplumber.
    from pipelines.parsers.gkv_parser import GKVParser

    df = GKVParser(pdf, output_filename=pdf.name).parse_pdf()
    print(f"📥 {len(df)} insurers parsed from {pdf}")

    insurers = []
    for _, row in df.iterrows():
        insurers.append({
            "name": row["name"],
            "website": row["website"],
            "zusatzbeitrag": row["zusatzbeitrag"],
            "geoffnet_in": row["geoffnet_in"],
            "is_bundesweit": bool(row["is_bundesweit"]),
            "bundeslaender": parse_bundeslaender(
                row["geoffnet_in"], row["is_bundesweit"], expand
            ),
            # Address columns exist once the scraper has run; absent here.
            "strasse": row.get("strasse"),
            "plz": row.get("plz"),
            "ort": row.get("ort"),
            "scraping_status": row.get("scraping_status"),
        })
    return loader.load_insurers(insurers, gueltig_ab=gueltig_ab)


def main() -> int:
    ap = argparse.ArgumentParser(description="Load ingested data into PostGIS.")
    ap.add_argument("what", choices=["providers", "insurers"])
    ap.add_argument("--input", type=Path, default=Path("pipelines/data/processed/providers.json"))
    ap.add_argument("--pdf", type=Path, default=Path("pipelines/data/raw/gkv_liste_2026.pdf"))
    ap.add_argument("--gueltig-ab", type=date.fromisoformat, default=date.today(),
                    help="Publication date of the insurer list (YYYY-MM-DD)")
    ap.add_argument("--expand-bundesweit", action="store_true",
                    help="Link nationwide insurers to all 16 states")
    # INGEST_DATABASE_URL first: DATABASE_URL belongs to the read-only API role,
    # and loading with it would fail on the first write.
    ap.add_argument(
        "--dsn",
        default=os.environ.get("INGEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_DSN,
        help="Postgres DSN (defaults to $INGEST_DATABASE_URL)",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    loader = PostgresLoader(args.dsn)
    if args.what == "providers":
        report = load_providers(loader, args.input)
    else:
        report = load_insurers(loader, args.pdf, args.gueltig_ab, args.expand_bundesweit)

    print(f"✅ {report.summary()}")
    if not report.ok:
        log.error("skipped %d record(s): %s", len(report.skipped), report.skipped[:5])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
