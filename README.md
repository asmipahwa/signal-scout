# Signal Scout: AI Competitor Insights Pipeline

Watches competitors' news, press releases, and website changes, uses an LLM (Gemini or Claude) to turn them into ranked insights, and serves them over search, Slack alerts, and an MCP server for AI agents.

> **Status: in active development.** The prototype and the LLM enrichment layer work today; storage, search, and agent interfaces are being built in sprints (see [Roadmap](#roadmap)). Every number in this README comes from a real run, with the raw output linked. Nothing is estimated or simulated.

**LLM provider:** Claude adapter built and tested; production run on Gemini.

## Who it's for

- **A sales rep** walking into a competitive deal: "What did ClickUp ship this month, and how do I handle it?"
- **A product marketer** tracking launches, pricing changes, and messaging shifts across a competitive set.

The output is not a news feed. Each insight says *what changed* and *why it matters for a seller*, ranked so the important items come first.

## What works today

| Piece | What it does | Where |
|---|---|---|
| Signal ingestion (prototype) | Pulls real items from Bing News RSS, Hacker News (Algolia API), PR Newswire, tech publisher feeds, and Notion's release-notes feed. Respects `robots.txt` (Google News RSS is excluded because its robots.txt disallows it). | [`prototype/scout.py`](prototype/scout.py) |
| Pre-LLM filtering | Drops stale items, items that don't mention a tracked competitor (case-sensitive, so "the notion that..." isn't Notion), duplicate URLs, and near-duplicate titles *before* spending any tokens. | [`prototype/scout.py`](prototype/scout.py) |
| Provider-agnostic LLM enrichment | One `LLMClient` interface, two real adapters (Gemini via `google-genai`, Claude via `anthropic`), one shared prompt and JSON schema. Output is schema-constrained, then validated with Pydantic; bad output is retried once, then the signal is marked failed instead of crashing. | [`src/signal_scout/enrichment/`](src/signal_scout/enrichment) |
| Free-tier-safe rate limiting | Client-side per-minute and per-day limits; the daily count persists across restarts so multi-day backfills resume safely. Rate limits and overloaded-model 503s back off and retry; daily-quota exhaustion stops cleanly. | [`rate_limit.py`](src/signal_scout/enrichment/rate_limit.py), [`client.py`](src/signal_scout/enrichment/client.py) |
| Clustering + ranking (prototype) | Groups signals about the same event, then ranks by importance × recency (48 h half-life) × source weight × corroboration. | [`prototype/scout.py`](prototype/scout.py) |
| Tests | Both adapters tested against replayed API responses (plus a contract test against a real recorded Gemini response); 98% coverage on the enrichment package. | [`tests/unit/enrichment/`](tests/unit/enrichment) |

## Prototype results

First real run, 2026-10-31: 3 competitors (Asana, ClickUp, Notion), 180-day window, `gemini-3.5-flash-lite` on the free tier. Raw output in [`docs/results/2026-10-31-prototype/`](docs/results/2026-10-31-prototype/); full write-up in [`docs/PROTOTYPE_TO_PROD.md`](docs/PROTOTYPE_TO_PROD.md).

| Metric | Value |
|---|---|
| Signals fetched → sent to LLM → relevant | 803 → 101 → 75 |
| Enrichment failures | 0 of 101 |
| LLM cost | $0.00 (free tier); $0.82 per 1,000 signals at paid Flash-Lite rates |
| Latency per call | p50 1.11 s, p95 10.62 s |

Top insights after the ranking fix (7-day recency half-life):

| # | Competitor | Category | Importance | Headline (shortened) |
|---|---|---|---|---|
| 1 | Notion | product_launch | 4 | Agents, rebuilt to do more for less (2 sources) |
| 2 | Notion | outage_or_incident | 4 | Notion outage of over three hours left users unable to sign in |
| 3 | Notion | pricing_change | 5 | Notion AI Business plan shifts to a credit system |
| 4 | Notion | pricing_change | 4 | Notion AI moves to "General" and "Designated" pricing |
| 5 | Asana | outage_or_incident | 4 | Asana down for thousands of users |

What the prototype got wrong (and the fixes) is documented too: the first ranking put an importance-2 blog post at #1, and clustering split one Notion story into five insights.

## Swapping providers

Enrichment goes through one `LLMClient` interface with two real adapters. Both return the same validated `Enrichment`, so switching is one environment variable:

```sh
LLM_PROVIDER=claude   # or gemini (default); optionally LLM_MODEL=<model id>
```

## Quickstart (prototype)

Requires [uv](https://docs.astral.sh/uv/) and a free Gemini API key from [Google AI Studio](https://aistudio.google.com).

```sh
uv sync
# Store the key in the macOS Keychain (never in the repo):
security add-generic-password -U -a "$USER" -s signal-scout-gemini -w
uv run python prototype/scout.py --no-llm                       # fetch + filter only
scripts/with-secrets.sh uv run python prototype/scout.py        # fetch, enrich, rank
uv run pytest                                                   # tests (no network)
```

## Running the pipeline locally (Sprint 2: foundation)

Needs Docker and [uv](https://docs.astral.sh/uv/).

```sh
make up        # creates .env with random local passwords, starts Postgres 16, Elasticsearch 8, MinIO
make migrate   # alembic upgrade head
make seed      # 8 competitors, 30 sources (incl. 8 pricing pages) from seeds/competitors.yaml (idempotent)
make scheduler # enqueues fetch_source jobs as sources come due
make worker    # claims jobs from the Postgres queue and ingests feeds
make test      # unit tests;  make it  # integration tests (testcontainers);  make lint
make down      # stop the stack (data volumes are kept)
```

How ingestion works: the scheduler enqueues a `fetch_source` job per due source into a Postgres queue
(`SELECT ... FOR UPDATE SKIP LOCKED`). A worker fetches the feed politely (identifying User-Agent,
ETag/If-Modified-Since, per-domain token bucket, retries with backoff and jitter, a circuit breaker
that disables a source after repeated failures), stores the raw item and extracted text gzipped and
content-addressed in S3 (`raw/<yyyy>/<mm>/<dd>/<sha256>.html.gz`, `text/<sha256>.txt.gz`), and inserts
deduplicated signal rows (metadata and a short excerpt only). Every job writes a `job_runs` row, and its
JSON log lines carry the same `run_id`.

Local dev uses `bitnamilegacy/minio` because MinIO no longer publishes images to Docker Hub or Quay;
it is a frozen build, fine for development only.

## Roadmap

| Sprint | Scope | Status |
|---|---|---|
| 1. Prototype | Real signals → LLM → ranked insights; validate the idea | Done |
| 2. Foundation | Docker Compose (Postgres 16, Elasticsearch 8, MinIO/S3), Alembic schema, Postgres job queue + scheduler, RSS ingestion → S3 + Postgres, `job_runs` logging, CI | Done |
| 3. Intelligence | MinHash near-dup detection, CostGuard, webpage-change monitoring with diffs, clustering + ranking service, 100-signal labeled eval (`make eval PROVIDER=gemini\|claude`) | Planned |
| 4. Interfaces | Elasticsearch search API, FastAPI REST, Slack alerts, signed inbound/outbound webhooks, MCP server for AI agents | Planned |
| 5. Production + evidence | Prometheus + Grafana, historical backfill, Locust load test, GHCR image, deploy guide | Planned |

## Design decisions (so far)

- **Filter before the LLM.** Most fetched items never mention a tracked competitor; filtering them out first is the biggest cost and quota saver.
- **One schema, many providers.** The JSON schema is deliberately simple (no min/max) because providers support different subsets of JSON Schema; Pydantic enforces ranges and the tracked-competitor list after the call.
- **Transient vs. permanent failures.** 429s and 5xx back off and retry; a daily-quota error stops the run so it can resume after reset; a 4xx fails only that one signal. Discovered the hard way when a live 503 from an overloaded model crashed the first run.
- **Secrets stay out of the repo.** Keys live in the macOS Keychain and are loaded only into the process that needs them ([`scripts/with-secrets.sh`](scripts/with-secrets.sh)); GitHub secret scanning and push protection are on.

## How I built this with AI tools

I'm building this with Claude Code as a pair programmer: it drafts code and tests, I review, run, and direct. Honest notes, kept as they happen:

- A test fixture's source URL was written from memory and labeled as real; a later check against the cached fetch caught it, and it now uses the actual item.
- The first adapters only handled rate limits, so a live 503 crashed the run; fixed with transient/permanent error classes and tests built from the real error body.
- The original plan used Google News RSS; checking `robots.txt` before writing the fetcher showed it's disallowed, so it was dropped.

## License

MIT
