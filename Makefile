.PHONY: help up down stack images logs ps migrate tidy api fmt pipelines

help: ## Show available targets
	@grep -E '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start infra (db, typesense, redis)
	docker compose up -d db typesense redis

down: ## Stop and remove containers
	docker compose down

stack: ## Start the whole stack in containers, API included (CAREGRAPH_PORT=... to move it)
	docker compose --profile app up -d --build

images: ## Build both images without starting anything
	docker compose --profile app --profile ingest build

# Containerised ingestion. The same runners as the host targets above, in the
# image a deployment uses — which is the point: a self-hoster with neither Go
# nor uv installed can still load data and cut a dataset.
ingest-%: ## Run a pipeline in the ingest container, e.g. make ingest-dataset ARGS="export"
	@test -n "$(ARGS)" || (echo 'usage: make ingest-dataset ARGS="export --out /app/dist/x.tar.gz"' && exit 1)
	docker compose --profile ingest run --rm ingest -m pipelines.run_$* $(ARGS)

logs: ## Tail container logs
	docker compose logs -f

ps: ## Show running services
	docker compose ps

migrate: ## Apply ALL migrations to the running db, in order
	@for m in db/migrations/*.sql; do \
		echo "applying $$m"; \
		docker compose exec -T db psql -U $${POSTGRES_USER:-caregraph} -d $${POSTGRES_DB:-caregraph} -v ON_ERROR_STOP=1 -q < $$m || exit 1; \
	done

db-roles-dev: ## Set throwaway passwords for the least-privilege roles (LOCAL ONLY)
	@docker compose exec -T db psql -U $${POSTGRES_USER:-caregraph} -d $${POSTGRES_DB:-caregraph} -q -c \
		"ALTER ROLE caregraph_ingest WITH PASSWORD 'devingest'; ALTER ROLE caregraph_api WITH PASSWORD 'devapi';"
	@echo "dev passwords set — never use these outside a local container"

load-providers: ## Load scraped providers into PostGIS
	$(call with_env, uv run --project pipelines python -m pipelines.run_load providers)

load-insurers: ## Load the GKV insurer list into PostGIS
	$(call with_env, uv run --project pipelines python -m pipelines.run_load insurers)

test-db: ## Run the Python suite including database integration tests
	$(call with_env, CAREGRAPH_TEST_DSN=$$INGEST_DATABASE_URL uv run --project pipelines pytest pipelines/tests -q)

tidy: ## Resolve Go dependencies (creates go.sum)
	go mod tidy

# The Go binaries no longer carry a compiled-in DSN, so these targets take the
# configuration from .env.
#
# Only variables that are not already set are taken from the file. The obvious
# `set -a; . ./.env` would do the opposite and let the file overwrite what the
# caller passed, so `CAREGRAPH_HTTP_ADDR=:9000 make api` would silently ignore
# the port and start on the one in .env. The real environment wins; the file
# supplies defaults.
define with_env
@test -f .env || { \
	echo "no .env — run: cp .env.example .env && make up && make db-roles-dev"; \
	exit 1; \
}
@while IFS='=' read -r key value; do \
	case "$$key" in ''|\#*) continue ;; esac; \
	printenv "$$key" >/dev/null || export "$$key=$$value"; \
done < .env; \
$(1)
endef

api: ## Run the Go API locally (configuration from .env)
	$(call with_env, go run ./cmd/api)

fmt: ## Format Go code
	go fmt ./...

pipelines: ## Sync Python pipeline dependencies
	cd pipelines && uv sync

apikey-dev: ## Issue a local API key for development (prints it once)
	$(call with_env, go run ./cmd/apikey issue --name "Local Dev" --tier community)

apikeys: ## List API keys
	$(call with_env, go run ./cmd/apikey list)

dataset-export: ## Export the provider dataset as a redistributable archive
	$(call with_env, uv run --project pipelines python -m pipelines.run_dataset export)

dataset-import: ## Import a dataset archive (FILE=dist/....tar.gz)
	@test -n "$(FILE)" || (echo "usage: make dataset-import FILE=dist/caregraph-providers-YYYY-MM-DD.tar.gz" && exit 1)
	$(call with_env, uv run --project pipelines python -m pipelines.run_dataset import --file "$(FILE)")

search-sync: ## Rebuild the Typesense search index from Postgres
	$(call with_env, uv run --project pipelines python -m pipelines.run_search sync)

bootstrap: ## Fresh install: schema + dataset (FILE=...) or full ingestion
	@echo "1/3  applying migrations"
	@$(MAKE) --no-print-directory migrate
	@if [ -n "$(FILE)" ]; then \
		echo "2/3  importing $(FILE)"; \
		$(MAKE) --no-print-directory dataset-import FILE="$(FILE)"; \
		echo "3/3  building the search index"; \
		$(MAKE) --no-print-directory search-sync; \
	else \
		echo "2/3  no FILE given — run the ingestion instead:"; \
		echo "       make load-providers   (needs pipelines/data/processed/providers.json)"; \
		echo "       make load-insurers    (needs the GKV list PDF)"; \
		echo "     or pass a release archive:  make bootstrap FILE=dist/....tar.gz"; \
		echo "     then:  make search-sync"; \
	fi

load-hospitals: ## Load the Bundes-Klinik-Atlas export (FILE=... , download it yourself)
	@test -n "$(FILE)" || (echo "usage: make load-hospitals FILE=pipelines/data/raw/..._TVERZ_Export.xml" && echo "get it from https://bundes-klinik-atlas.de/open-data/" && exit 1)
	$(call with_env, uv run --project pipelines python -m pipelines.run_load hospitals --input "$(FILE)")
