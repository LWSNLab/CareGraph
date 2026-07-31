# CareGraph Ingestion Pipelines (Python)

Extract → normalize → geocode → load, feeding the PostGIS core.

## Layout

| Package | Role | Roadmap |
| :--- | :--- | :--- |
| `parsers/` | PDF & tabular parsers (GKV insurer list) | Milestone 1.1 |
| `scrapers/` | Web scrapers for care-provider directories | Milestone 1.2 |
| `geocoding/` | OSM/Nominatim address resolution + cache | Milestone 1.3 |

## Setup

```bash
cd pipelines
uv sync                    # create venv + install deps
uv run playwright install  # browser binaries for scrapers
```

## Notes

- Respect each source's `robots.txt` / terms — see the docs repo `docs/legal/data-licensing.md`.
- The GKV parser prototype (coordinate-based `pdfplumber` extraction, address
  enrichment, Bundesland normalization) is ready to be ported into `parsers/`.
