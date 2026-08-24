# Contributing to CareGraph

CareGraph unifies fragmented German health and care data into one spatial API.
Contributions are welcome — code, data corrections, documentation, or a bug
report that saves someone else an afternoon.

**Security problems do not belong in an issue.** See [SECURITY.md](./SECURITY.md).

## Getting a development environment

Docker and Docker Compose are the only hard requirements. Go 1.25 and `uv` are
needed to work on the Go API or the Python pipelines directly.

```bash
cp .env.example .env
make up
make db-roles-dev
```

The database is empty until you load it. From a release archive:

```bash
make dataset-import FILE=dist/caregraph-providers-YYYY-MM-DD.tar.gz
```

Then run the API and issue yourself a key:

```bash
make tidy && make api
make apikey-dev
```

`make help` lists everything else. If you would rather not install a Go
toolchain, `make stack` builds and runs the API in a container instead.

## Before you open a pull request

Run what CI runs. It is faster to find out here than in the pull request:

| | |
| :-- | :-- |
| `make fmt` | formatting — CI fails on a diff, it does not reformat for you |
| `go build ./... && go vet ./...` | |
| `go test ./...` | needs the stack up; migrations are applied twice, so every migration must be re-runnable |
| `uv run ruff check .` | in `pipelines/` |
| `uv run --project pipelines pytest pipelines/tests -q` | |

CI additionally walks the full git history with `gitleaks` and runs a dataset
export/import round trip.

### The contract is a build gate

`api/openapi.yaml` is not documentation that trails the code — it ships inside
the binary, is served at `GET /openapi.yaml`, and tests fail the build when
routes, error codes, provider types, struct tags or real responses drift from
it.

If you add or change an endpoint, change the contract in the same commit. A test
telling you the spec disagrees with the code is the gate working, not a flake.

## Commits and branches

Branches are named after the story they implement where there is one —
`feature/e4-s1-containerization` — and after what they do otherwise.

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(search): add umlaut-tolerant matching
fix(auth): reject keys whose id half is well-formed but unknown
docs(deploy): explain the trusted-proxy setting
```

Pull requests go against `develop`. `main` is the release branch: merging into
it publishes images and cuts a release, so it is not where work lands.

## Releases

Two things are written by hand — the version and the notes:

```bash
make set-version VERSION=0.2.0
$EDITOR CHANGELOG.md
```

Everything after that is automatic. The full reasoning is in
[Versioning](https://github.com/LWSNLab/CareGraph_Doc/blob/main/docs/architecture/versioning.md).

## Contributing data

Data corrections are as valuable as code, and often harder to come by.

Records carry their source in `quelle` and the date it was valid from. A
correction needs the same: **where the better value comes from**, so the fix
survives the next ingestion run rather than being overwritten by it. A
correction with no source cannot be re-derived, and will be.

Hand-maintained corrections live in `pipelines/data/ik_overrides.json` and
`manual_overrides.json`.

Two rules the ingestion follows, and so must any contribution to it:

- **Re-collect, do not mirror.** German law protects a compiled database
  (§§ 87a–87e UrhG) even where the individual facts are free. Extract and
  re-verify facts from primary sources; never copy a substantial part of someone
  else's directory.
- **Respect `robots.txt` and terms of service.** Where a directory is protected,
  the question is whom to ask, not how much can be taken without asking.

Background: [Data Sources & Licensing](https://github.com/LWSNLab/CareGraph_Doc/blob/main/docs/legal/data-licensing.md).

## Requesting an API key

There is no self-service signup. Open an issue using the **API key request**
template — the maintainers issue keys by hand.

This is deliberate for now: self-service means storing email addresses, which
means a privacy notice and abuse handling before there is any demand. A handful
of users the maintainers can talk to are worth more than a thousand anonymous
keys.

If you are running your own instance, you do not need to ask anyone:
`make apikey-dev` issues one against your own database.

## Licensing of contributions

Code is **AGPLv3**; the documentation repository is **CC BY-SA 4.0**. By opening
a pull request you agree that your contribution is licensed the same way.

There is no CLA. Contributors keep their copyright, and the project cannot
relicense their work — which also means CareGraph cannot later be taken
proprietary.

## Code of Conduct

Participation is governed by the [Code of Conduct](./CODE_OF_CONDUCT.md).
