## What this changes

<!-- And why. The what is in the diff; the why is not. -->

Closes #

## Checklist

- [ ] `make fmt` — CI fails on a formatting diff rather than fixing it
- [ ] `go build ./... && go vet ./... && go test ./...`
- [ ] `uv run ruff check .` and the pipeline tests, if `pipelines/` changed
- [ ] `api/openapi.yaml` updated in this pull request, if the API surface changed
- [ ] Documentation updated, if behaviour or operation changed
- [ ] No secrets, and no raw source data — CI walks the full history for both

## Migrations

<!-- Delete if none. -->

Migrations are applied twice in CI, so every migration must be re-runnable.

- [ ] Re-runnable
- [ ] An existing deployment survives it — `deploy/update.sh` applies migrations
      against a populated database, not an empty one

## Anything reviewers should look at first

<!-- A decision you are unsure about, a trade-off, something you could not test. -->
