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

migrate: ## Apply migrations to the running db
	docker compose exec -T db psql -U $${POSTGRES_USER:-caregraph} -d $${POSTGRES_DB:-caregraph} < db/migrations/0001_init.sql

tidy: ## Resolve Go dependencies (creates go.sum)
	go mod tidy

api: ## Run the Go API locally
	go run ./cmd/api

fmt: ## Format Go code
	go fmt ./...

pipelines: ## Sync Python pipeline dependencies
	cd pipelines && uv sync
