# Sprint log

Planned vs. actual scope per sprint, with the measured outcome and a short retro. Dates are when the work
was done. Sprints 2 and 4 were built partly in parallel by separate Claude Code agents on their own
branches, then merged into `dev`.

## Sprint 1: Prototype (2026-10-28 to 2026-10-31)

**Goal:** prove that "public signals → LLM → ranked insights" produces something a sales rep would want,
before building infrastructure.

| Planned | Actual |
|---|---|
| ~200 real signals for 3 competitors | 803 fetched, **101** relevant after filters (legitimate sources ran out) |
| Claude enrichment | **Changed:** provider-agnostic `LLMClient`; Gemini (free tier) runs it, Claude adapter built and tested with replayed responses |
| Print ranked insights | Done; ranking bug found and fixed with a measured before/after |

**Shipped:** `prototype/scout.py`, `src/signal_scout/enrichment/`, `docs/PROTOTYPE_TO_PROD.md`,
evidence in `docs/results/2026-10-31-prototype/`. Merged to `main` via PR #1.

**Retro:** Checking `robots.txt` first saved us from building on Google News RSS. A live 503 crashed the
first run, which drove the transient/permanent error split. Measuring ranking on saved output (no new LLM
calls) made tuning free.

## Sprint 2: Foundation (2026-10-30 to 2026-10-31)

**Goal:** storage, queue, ingestion and CI that later sprints build on.

| Planned | Actual |
|---|---|
| Compose: Postgres, ES, MinIO | Done (MinIO via `bitnamilegacy/minio`; official images are no longer published) |
| Alembic schema + evolving migration | Done; migration 2 adds `signals.language` and insight/webhook columns, tested down/up |
| Postgres job queue + scheduler | Done (`SKIP LOCKED`, backoff, dead letters, stale-lock release) |
| RSS ingestion → S3 + Postgres, `job_runs` | Done; content-addressed S3, conditional GETs, circuit breaker |
| CI skeleton | Done: ruff, mypy strict, unit + integration (testcontainers) on GitHub Actions |

**Retro:** Building this in a parallel agent worktree kept it from blocking evals. Several competitor
blogs have no usable feed; the seed file documents every rejected URL and why.

## Sprint 3: Intelligence (2026-10-31)

**Goal:** spend LLM quota only where it matters, and make insights trustworthy.

| Planned | Actual |
|---|---|
| Dedupe + relevance filter | Exact (URL, hash) + MinHash LSH near-dups + low-value title patterns: drops 16/35 irrelevant, 0/66 relevant on the eval set |
| Enrichment with CostGuard | Done; caps from `llm_calls`; tested (cap reached → 0 calls, signals deferred) |
| Webpage-change monitoring | Done: 8 pricing pages, cosmetic changes ignored, a change becomes a diff signal |
| Clustering + ranking | TF-IDF clustering across categories (10 multi-signal clusters vs 5 before), 7-day recency half-life |
| Eval set + `make eval` | 101 labeled real signals; prompts v1 → v2 → v3; v3 default (100% of important signals reach the feed, 62.1% exact importance) |

**Retro:** Feed-level metrics changed the decision: v2 looked best on aggregate accuracy but dropped two
important pricing signals. Labels are still pending human review.

## Sprint 4: Interfaces (2026-10-31)

**Goal:** make the intel usable by people and AI agents.

| Planned | Actual |
|---|---|
| ES indexing + search API | Indexer with versioned index + alias, full rebuild from Postgres (tested: identical results) |
| REST endpoints, problem+json, API keys | Built by a parallel agent on `interfaces`, merged into `dev` |
| Slack alerts + signed webhooks | Done; at-most-once per insight; retries with backoff (tested: 500, 500, 200 → delivered once) |
| MCP server + Claude Desktop demo | Server built (stdio + Streamable HTTP); Claude Desktop demo pending |

## Sprint 5: Production + evidence (in progress)

Prometheus + Grafana with provisioned dashboard and alert rules (done), Dockerfile (done), historical
backfill (running), load test, GHCR publishing, remaining docs.
