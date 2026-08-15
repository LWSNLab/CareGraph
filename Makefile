.PHONY: help up down logs ps migrate tidy api fmt pipelines

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start infra (db, typesense, redis)
	docker compose up -d db typesense redis

down: ## Stop and remove containers
	docker compose down

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
	uv run --project pipelines python -m pipelines.run_load providers

load-insurers: ## Load the GKV insurer list into PostGIS
	uv run --project pipelines python -m pipelines.run_load insurers

test-db: ## Run the Python suite including database integration tests
	CAREGRAPH_TEST_DSN=$${DATABASE_URL:-postgres://caregraph:caregraph@localhost:5433/caregraph?sslmode=disable} \
		uv run --project pipelines pytest pipelines/tests -q

tidy: ## Resolve Go dependencies (creates go.sum)
	go mod tidy

api: ## Run the Go API locally
	go run ./cmd/api

fmt: ## Format Go code
	go fmt ./...

pipelines: ## Sync Python pipeline dependencies
	cd pipelines && uv sync

apikey-dev: ## Issue a local API key for development (prints it once)
	go run ./cmd/apikey issue --name "Local Dev" --tier community

apikeys: ## List API keys
	go run ./cmd/apikey list

dataset-export: ## Export the provider dataset as a redistributable archive
	uv run --project pipelines python -m pipelines.run_dataset export

dataset-import: ## Import a dataset archive (FILE=dist/....tar.gz)
	@test -n "$(FILE)" || (echo "usage: make dataset-import FILE=dist/caregraph-providers-YYYY-MM-DD.tar.gz" && exit 1)
	uv run --project pipelines python -m pipelines.run_dataset import --file "$(FILE)"

bootstrap: ## Fresh install: schema + dataset (FILE=...) or full ingestion
	@echo "1/2  applying migrations"
	@$(MAKE) --no-print-directory migrate
	@if [ -n "$(FILE)" ]; then \
		echo "2/2  importing $(FILE)"; \
		$(MAKE) --no-print-directory dataset-import FILE="$(FILE)"; \
	else \
		echo "2/2  no FILE given — run the ingestion instead:"; \
		echo "       make load-providers   (needs pipelines/data/processed/providers.json)"; \
		echo "       make load-insurers    (needs the GKV list PDF)"; \
		echo "     or pass a release archive:  make bootstrap FILE=dist/....tar.gz"; \
	fi
