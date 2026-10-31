.DEFAULT_GOAL := help
.PHONY: help env up down migrate seed worker scheduler test it lint format

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

env: ## Create .env with random local-dev passwords (never overwrites existing values)
	@scripts/init-env.sh

up: env ## Start postgres, elasticsearch, minio and create the bucket
	docker compose up -d --wait postgres elasticsearch minio
	docker compose run --rm minio-init

down: ## Stop the stack (keeps volumes; `docker compose down -v` deletes data)
	docker compose down

migrate: ## Apply database migrations
	uv run alembic upgrade head

seed: ## Load competitors and sources from seeds/competitors.yaml (idempotent)
	uv run python -m signal_scout.seed seeds/competitors.yaml

worker: ## Run a queue worker
	uv run python -m signal_scout.worker

scheduler: ## Run the scheduler (enqueues fetch_source jobs)
	uv run python -m signal_scout.scheduler

test: ## Unit tests with coverage
	uv run pytest -m "not integration" --cov --cov-report=term

it: ## Integration tests (needs Docker; uses throwaway testcontainers, not the compose stack)
	uv run pytest -m integration -v

lint: ## ruff check, ruff format --check, mypy --strict on src
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Auto-format
	uv run ruff check --fix .
	uv run ruff format .
