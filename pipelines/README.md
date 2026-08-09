# CareGraph Ingestion Pipelines (Python)

Extract → normalize → geocode → **load into PostGIS**. The database is the
source of truth; these pipelines run on a schedule, never per API request.

## Layout

| Package | Role | Story |
| :--- | :--- | :--- |
| `parsers/` | PDF & tabular parsers (GKV insurer list) | E1-S1 |
| `scrapers/` | Care-provider collection (OSM/Overpass) and insurer address enrichment | E1-S1, E1-S2 |
| `geocoding/` | OSM/Nominatim address resolution + cache | E1-S3 |
| `load/` | `postgres_loader.py` writes to PostGIS; `exporter.py` is the older file exporter | E1-S4 |
| `common/` | Helpers shared across stages (federal-state normalisation) | — |
| `tests/` | Unit tests everywhere; loader integration tests need a database | — |

Entry points are the `run_*.py` modules at the top level.

## Setup

```bash
cd pipelines
uv sync --all-groups       # venv + runtime and dev dependencies
uv run playwright install  # browser binaries, only needed for future scrapers
```

## Running

From the **repository root**, so `pipelines` imports as a package:

```bash
make up                 # start PostGIS (host port 5433 by default)
make migrate            # apply every migration in order

python -m pipelines.run_providers --all          # collect providers from OSM
python -m pipelines.run_load providers           # load them into PostGIS
python -m pipelines.run_load insurers --gueltig-ab 2026-07-26
```

Both loads are **idempotent** — re-running updates rows instead of duplicating
them, keyed on `care_infrastructure.source_id`.

## Tests

```bash
uv run --project pipelines pytest pipelines/tests -q   # unit tests only
make test-db                                           # + database integration tests
```

Tests that need the official GKV PDF or a database skip themselves when those
are absent, so a plain checkout stays green.

## Notes

- Respect each source's `robots.txt` and terms — see the docs repo,
  `docs/legal/data-licensing.md`. The insurer portals are deliberately not scraped.
- OSM data is ODbL-licensed; the attribution travels with each record in `details`.
- Anything used by more than one stage belongs in `common/`, not copied.
