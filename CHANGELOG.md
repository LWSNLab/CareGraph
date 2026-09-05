# Changelog

Written by hand. The entry for a version becomes the text of its GitHub release,
so it is addressed to whoever runs or consumes this — not a list of commits.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/). Releasing is `make set-version`, an
entry here, and a merge into `main`.

## [Unreleased]

## [0.1.1] — 2026-08-25

Deployment fixes. v0.1.0 could be installed and would answer `/readyz` with 200
while being usable by nobody — every `/v1` route needs an API key, and the tool
that issues one was not in the image.

### Fixed

- **The API image carries `apikey`.** Issuing and listing keys is the one routine
  operator task on a server, and it previously required a Go toolchain beside the
  container — which is what the image exists to avoid. Reached with
  `--entrypoint /apikey`; the image build now fails if it is missing.
- **The deployment runbook keeps its own promise.** It opens with "nothing here
  needs a Go toolchain or `uv` on the server" and then told you to run targets
  that need exactly those. Loading data and issuing keys now go through the
  containers.
- **`make ingest-*` can use the published image.** `CAREGRAPH_PROD=1` adds the
  production overlay and pulls it; without it a server built its own copy of an
  image CI had already built and tested.

### Changed

- CI no longer runs twice on a release. A push to `main` starts the release,
  which calls CI itself, so the second run only cancelled the first and left a
  grey "cancelled" beside the green checks.

## [0.1.0] — 2026-08-19

First release. Pre-1.0 and honest about it: the API surface is complete and
tested, the ingestion covers three sources, and one story is blocked on an
answer from outside the project.

### Added

- **Spatial radius search** — `GET /v1/infrastructure/near`, PostGIS
  `ST_DWithin` over 9,192 records, nearest first with distances. Query p95 under
  7 ms.
- **Typo- and umlaut-tolerant search** — `GET /v1/infrastructure/search` via
  Typesense. `Charite` finds Charité, `Munster` finds Münster. The engine ranks,
  PostgreSQL returns the records, so a hit has the same shape as one from
  `/near`.
- **Lookup by Institutionskennzeichen** — `GET /v1/infrastructure/{ik_nummer}`,
  resolving 92 of the 93 statutory insurers. Care providers and hospitals carry
  no public IK, so they are reached through the other two endpoints.
- **API keys and rate limiting** — Argon2id, a two-part key whose id half is
  public, and a Redis token bucket with per-tier quotas. Failed authentications
  are limited separately, so a stream of wrong secrets cannot exhaust a
  legitimate client's budget.
- **One error shape for every failure**, including those the framework generates:
  unknown route, wrong method, panic. Branch on `code`, never on the message.
- **The contract is a build gate.** `api/openapi.yaml` ships inside the binary and
  is served at `GET /openapi.yaml`. Tests fail the build when routes, error
  codes, provider types, struct tags or real responses drift from it.
- **Liveness and readiness are separate.** `/healthz` reports the process;
  `/readyz` probes Postgres, Redis and Typesense and weighs each by how the API
  actually degrades without it.
- **A redistributable dataset** — ODbL, ~7,500 care providers with attribution and
  the schema it was cut against recorded in its manifest. `make bootstrap` turns
  an empty database into a running instance.
- **Deployment** — Caddy terminates TLS with automatic certificates, a merge into
  `main` publishes images, and the server pulls them. Nothing connects inward.

### Known limitations

- **Provider IK numbers are missing, and will stay missing.** Every
  Leistungserbringer has one, but no public source publishes them. The bodies
  that hold the pairing were asked and declined: the number is not among the
  data whose publication is legally provided for, and it is maintained for
  billing rather than for identification by third parties. Providers are reached
  by name and location instead.
- **The published archive holds care providers only.** Insurers need the GKV list
  PDF; hospitals are excluded until the Standortverzeichnis answers a
  redistribution question.
- **Roughly a third of provider records lack a full street address** — their
  OpenStreetMap objects carry no `addr:*` tags.
- **Deduplication and address backfill are not built.**

[Unreleased]: https://github.com/LWSNLab/CareGraph/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/LWSNLab/CareGraph/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/LWSNLab/CareGraph/releases/tag/v0.1.0
