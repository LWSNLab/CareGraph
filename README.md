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

# 2) Go API
make tidy        # resolve deps → go.sum   (needs Go 1.23+)
make api         # runs on :8080

# 3) Python pipelines
make pipelines   # cd pipelines && uv sync
```

Smoke test:

```bash
curl localhost:8080/healthz
```

The full container stack including the API:

```bash
make tidy
docker compose --profile app up --build
```

---

## Status

Early scaffold (Phase 1). The Go domain methods and pipelines are stubs with
`TODO`s pointing at the reference queries/specs in the documentation repo. See
its roadmap for milestones.
