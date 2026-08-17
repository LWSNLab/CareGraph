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
# .env is required, not optional: the binaries carry no default DSN, because a
# compiled-in credential is one that eventually gets deployed.
cp .env.example .env

# 1) Start infrastructure (auto-applies db/migrations on first run)
make up
make db-roles-dev   # the migration creates the least-privilege roles; this
                    # gives them the throwaway passwords .env expects

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

# Which dependencies this instance can reach
curl localhost:8080/readyz

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

### Everything in containers

No Go and no `uv` needed — the API and the ingestion pipelines both have images:

```bash
make tidy      # once, to produce go.sum
make stack     # db, redis, typesense and the API, built and started

# Ingestion runs as a batch job in its own image
make ingest-dataset ARGS="export --out /app/dist/providers.tar.gz"
make ingest-load    ARGS="providers --input pipelines/data/raw/providers.json"
make ingest-search  ARGS="sync"
```

`make stack` publishes the API on `:8080`. All published ports are overridable in
`.env` — `CAREGRAPH_PORT`, `POSTGRES_PORT`, `TYPESENSE_PORT`, `REDIS_PORT` — which
matters because `6379` is already taken on any machine running a Redis. If you
move `POSTGRES_PORT`, change the port inside `DATABASE_URL` and
`ADMIN_DATABASE_URL` to match; they are separate settings because a deployment
may point at a database that compose does not manage.

The API container reports its own health — the binary probes its `/readyz`, since
the image is distroless and has no shell for curl — so `docker compose ps` tells
you whether it can actually serve, not just whether the process started.

**A remote database must use TLS.** Both the API and the pipelines refuse
`sslmode=disable` — and an *unset* `sslmode`, which libpq treats as `prefer` and
silently downgrades — against anything that is not loopback. Use
`sslmode=require`. The compose services set `CAREGRAPH_ALLOW_INSECURE_DB=1`
because they talk to `db` over a private network on one host; a deployment
spanning machines must not.

---

## Status

Pre-1.0, and honest about which parts are real:

| | |
| :-- | :-- |
| `GET /v1/infrastructure/near` | ✅ radius search over PostGIS, p95 ~10 ms |
| `GET /v1/infrastructure/{ik_nummer}` | ✅ resolves the 92 insurers that have an IK — **once they are loaded**; the release archive is providers only, so this answers `404` until `make load-insurers` has run |
| `GET /v1/infrastructure/search` | ✅ typo- and umlaut-tolerant via Typesense; `501` when no engine is configured |
| `GET /openapi.yaml` | ✅ the contract, embedded in the binary and served unauthenticated |
| API keys & rate limiting | ✅ Argon2id, Redis token bucket, per-tier quotas |
| Ingestion | ✅ OSM providers, GKV insurer list, IK enrichment, Bundes-Klinik-Atlas hospitals |
| `GET /healthz` | ✅ liveness — deliberately does not probe dependencies |
| `GET /readyz` | ✅ readiness — Postgres, Redis and Typesense, with severity per dependency |
| Deduplication, address backfill | ⏳ planned |

See the [documentation repo](https://github.com/LWSNLab/CareGraph_Doc) for the
roadmap and the story-level detail.
