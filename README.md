# 🗺️ CareGraph

> The Open Health & Care Infrastructure Graph for Germany.

High-performance, open-source spatial API that unifies fragmented German health
and care data (GKV insurers, outpatient care services, nursing homes, advice
centers) into one PostGIS-backed graph.

**Documentation:** [LWSNLab/CareGraph_Doc](https://github.com/LWSNLab/CareGraph_Doc) ·
**License:** [AGPLv3](./LICENSE)

---

## Architecture (Modular Monolith)

```text
cmd/api/                 Go: API gateway entry point
internal/
  infrastructure/        Config + PostgreSQL/PostGIS connection
  provider/              Care-infrastructure domain (types, repo, handlers)
  search/                Typesense integration (fuzzy search)
  auth/                  API keys & rate limiting
pipelines/               Python: ingestion (scrapers, parsers, geocoding)
db/migrations/           SQL schema (PostGIS)
docker-compose.yml       Dev stack: Postgres+PostGIS, Typesense, Redis
```

**Stack:** Go (Gin) · PostgreSQL 16 + PostGIS · Typesense (C++) · Redis · Python 3.12 ingestion.

---

## Quick Start

```bash
cp .env.example .env

# 1) Start infrastructure (auto-applies db/migrations on first run)
make up

# 2) Load data — the database is empty until you do
make dataset-import FILE=dist/caregraph-providers-YYYY-MM-DD.tar.gz

# 3) Go API
make tidy        # resolve deps → go.sum
make api         # runs on :8080

# 4) Issue yourself an API key (printed once)
make apikey-dev
```

Smoke test — `/healthz` is public, everything under `/v1` needs the key:

```bash
curl localhost:8080/healthz

# The contract this build implements — no key needed
curl localhost:8080/openapi.yaml

curl -H "X-API-Key: cg_…" \
  'localhost:8080/v1/infrastructure/near?lat=52.52&lng=13.405&radius_km=5'
```

### The dataset

Providers are distributed as a release archive rather than committed to the
repository. Download it from the releases page, or rebuild from source with
`make load-providers` / `make load-insurers` — that path needs network access to
Overpass and takes minutes.

| | |
| :-- | :-- |
| **Contains** | ~7,500 care providers: outpatient services, nursing homes, Pflegestützpunkte |
| **Every record has** | a name and coordinates |
| **Roughly a third lack** | a full street address — their OpenStreetMap objects carry no `addr:*` tags |
| **No record has** | an Institutionskennzeichen; no public source publishes provider IKs |
| **Not included** | the statutory insurers — `make load-insurers` fetches those from the official GKV publication |
| **Licence** | ODbL v1.0, attribution required: *© OpenStreetMap contributors* |

It is a **snapshot** and ages from the moment it was cut; the archive's
`MANIFEST.json` records the date and the schema migration it matches.

To produce one from your own database: `make dataset-export`.

The full container stack including the API:

```bash
make tidy
docker compose --profile app up --build
```

---

## Status

Pre-1.0, and honest about which parts are real:

| | |
| :-- | :-- |
| `GET /v1/infrastructure/near` | ✅ radius search over PostGIS, p95 ~10 ms |
| `GET /v1/infrastructure/{ik_nummer}` | ✅ resolves the 92 insurers that have an IK |
| `GET /v1/infrastructure/search` | ✅ typo- and umlaut-tolerant via Typesense; `501` when no engine is configured |
| `GET /openapi.yaml` | ✅ the contract, embedded in the binary and served unauthenticated |
| API keys & rate limiting | ✅ Argon2id, Redis token bucket, per-tier quotas |
| Ingestion | ✅ OSM providers, GKV insurer list, IK enrichment, Bundes-Klinik-Atlas hospitals |
| `GET /healthz` | ⚠️ liveness only — a `200` does not mean the database is reachable |
| Deduplication, address backfill | ⏳ planned |

See the [documentation repo](https://github.com/LWSNLab/CareGraph_Doc) for the
roadmap and the story-level detail.
