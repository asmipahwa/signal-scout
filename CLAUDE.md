# CLAUDE.md

Guidance for AI coding assistants (Claude Code and similar) working in this repository.

## What this is

Signal Scout: an AI competitor-insights pipeline. Public signals (news RSS, press releases, changelogs,
Hacker News, competitor pricing pages) are ingested, filtered cheaply, enriched by an LLM (Gemini by
default, Claude via the same interface), clustered into ranked insights, indexed in Elasticsearch, and
served over a REST API, an MCP server, Slack alerts and signed webhooks.

## Layout

- `src/signal_scout/ingest/`: fetching (polite HTTP, feeds, HN, page snapshots), S3 storage, dedupe, alias matching.
- `src/signal_scout/enrichment/`: `LLMClient` protocol, Gemini and Claude adapters, prompts (versioned), eval scoring.
- `src/signal_scout/processing/`: pure functions: relevance filter, MinHash near-dups, clustering, ranking, page diffs.
- `src/signal_scout/pipeline/`: queue jobs: enrich, build insights, index, alerts; CostGuard.
- `src/signal_scout/api/`, `services/`, `mcp_server.py`: REST API and MCP server over one service layer.
- `src/signal_scout/db/` + `migrations/`: SQLAlchemy models and Alembic migrations. Postgres is the source of truth.
- `tests/unit/` (no network, no Docker) and `tests/integration/` (testcontainers Postgres, MinIO, Elasticsearch).

## Commands

```sh
make up && make migrate && make seed     # local stack
make worker / make scheduler / make api  # processes
make test        # unit tests;  make it  # integration tests (Docker)
make lint        # ruff + mypy --strict
make runs-report # funnel and job runs from Postgres
```

## Rules

- **Never fabricate data or metrics.** Every number in docs must come from a real run with output saved
  under `docs/results/`. Label hand-written test fixtures as such.
- **Never call an LLM API in tests.** Use `FakeLLMClient` or the replayed fixtures in
  `tests/unit/enrichment/fixtures/`. Real calls happen only in `scripts/run_eval.py` and the live worker.
- **Never read, print or commit secrets.** API keys live in the macOS Keychain and are loaded by
  `scripts/with-secrets.sh`; `.env` holds only local-dev infrastructure passwords and is gitignored.
- Respect `robots.txt` and rate limits; identify the crawler with the project User-Agent.
- Schema changes go through a new Alembic migration that keeps `tests/integration/test_migrations.py` green.
- Prompt changes get a new `PROMPT_VERSION` and an eval run (`make eval`) before becoming the default.
- Style: boring, conventional, well-named code; a short docstring on every module and public class
  explaining why it exists; ruff (line length 120) and mypy strict must pass.
