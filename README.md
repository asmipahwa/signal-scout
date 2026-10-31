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
make seed      # 8 competitors, 22 sources from seeds/competitors.yaml (idempotent)
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

## API

`make api` serves a FastAPI app on `API_HOST:API_PORT` (default `127.0.0.1:8000`); OpenAPI docs are at
`/docs`. `make env` generates a random local `API_KEY` and `INBOUND_WEBHOOK_SECRET` in `.env`.

| Endpoint | What it returns |
|---|---|
| `GET /api/v1/insights` | Ranked feed (score desc), filters `competitor`, `category`, `min_importance`, `days`; opaque `cursor` pagination |
| `GET /api/v1/insights/{id}` (`/sources`) | One insight with its linked signals (title, URL, source type, date) |
| `GET /api/v1/search` | Elasticsearch search: `q`, `competitor`, `category`, `from`, `to`, `min_importance`, `page`, `size`; highlights, `total`, `took_ms` |
| `GET /api/v1/competitors`, `/competitors/{id}/brief` | Tracked competitors; a brief with top insights, counts by category, pricing and page changes |
| `GET /api/v1/changes`, `/compare` | Recent pricing changes / launches / page diffs; side-by-side counts |
| `GET /api/v1/signals/{id}` | Signal metadata plus a presigned S3 URL for the raw object (expires after 15 minutes) |
| `POST/GET/DELETE /api/v1/webhooks`, `GET /api/v1/webhooks/{id}/deliveries` | Outbound webhooks; the signing secret is returned only once, at creation |
| `POST /api/v1/ingest/webhook` | Inbound signal, HMAC-signed (`X-SignalScout-Signature`); bad or stale signatures get 401 |
| `GET /api/v1/runs` | The `job_runs` audit log |
| `GET /healthz`, `/readyz`, `/metrics` | Liveness; readiness of Postgres, ES and S3 (503 with per-dependency status); Prometheus |

Reads are public; `POST`/`DELETE /api/v1/webhooks` need `X-API-Key`. Every error is RFC 9457
`application/problem+json`:

```sh
curl -s 'localhost:8000/api/v1/search?q=pricing&competitor=Notion&size=5'
curl -s 'localhost:8000/api/v1/insights?limit=10'          # then pass next_cursor back as ?cursor=
curl -s -X POST localhost:8000/api/v1/webhooks -H "X-API-Key: $API_KEY" \
  -H 'content-type: application/json' -d '{"name":"sales","url":"https://example.com/hook","filters":{"min_importance":4}}'
curl -s -X POST localhost:8000/api/v1/webhooks -d '{}' -H 'content-type: application/json'
# {"type":"about:blank","title":"Unauthorized","status":401,"detail":"missing or invalid X-API-Key header",...}
```

Signing an inbound signal (same scheme as outbound deliveries: HMAC-SHA256 over `<timestamp>.<raw body>`):

```sh
BODY='{"title":"Acme adds an AI tier","url":"https://example.com/acme-ai","text":"...","competitors":["Asana"]}'
SIG=$(uv run python -c "import sys; from signal_scout.alerts.signing import sign; print(sign(sys.argv[1], sys.argv[2].encode()))" "$INBOUND_WEBHOOK_SECRET" "$BODY")
curl -s -X POST localhost:8000/api/v1/ingest/webhook -H 'content-type: application/json' \
  -H "X-SignalScout-Signature: $SIG" -d "$BODY"
# {"signal_id":7,"created":true,"s3_key":"raw/...","status":"new"}
```

The curl calls above were run by hand against a throwaway local stack seeded with the integration-test
fixture data (invented insights, not real news).

## MCP

`src/signal_scout/mcp_server.py` is an MCP server (official `mcp` Python SDK v2, where FastMCP is now
`MCPServer`) so AI agents can query the same data. All tools are read-only and call the same services
as the REST API. Each call is logged with its arguments and latency and recorded in `job_runs` as
`mcp_tool:<name>`.

| Kind | Name |
|---|---|
| Tools | `search_insights(query, competitor?, category?, days?, limit?)`, `get_competitor_brief(competitor, days=30)`, `list_recent_changes(competitor?, change_type?, days=7)`, `compare_competitors(competitors, days=30)`, `get_signal_sources(insight_id)` |
| Resources | `competitors://list`, `insights://top` (head of the ranked feed) |
| Prompt | `weekly_competitive_digest(competitors?)` |

```sh
make mcp            # Streamable HTTP on MCP_HOST:MCP_PORT (default 127.0.0.1:8001), path /mcp
make mcp-inspect    # MCP Inspector (npx) against the stdio server
uv run python -m signal_scout.mcp_server --transport stdio
```

Checked by hand: the SDK client over both stdio and Streamable HTTP, and MCP Inspector 2.13.0 in CLI
mode (`--cli ... --method tools/list` / `tools/call`), against the same throwaway fixture stack.

### Use it from Claude Desktop

Add this to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`), with the
absolute path to your checkout. `--directory` makes the server read that checkout's `.env`, so the
stack from `make up` must be running:

```json
{
  "mcpServers": {
    "signal-scout": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/signal-scout", "python", "-m", "signal_scout.mcp_server"]
    }
  }
}
```

stdio is the default transport. Logs go to stderr, because stdout carries the protocol. If Claude Desktop
cannot find `uv`, use its absolute path (`which uv`) as `command`.

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
