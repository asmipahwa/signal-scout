# Performance

Measured numbers only, each with its raw output. Anything not yet measured says so.

## Machine

Apple M4 MacBook Air, 10 CPU cores, 16 GB RAM, macOS; Docker Desktop with about 8 GB available to
containers. Postgres 16.15, Elasticsearch 8.19.5 (single node, 512 MB heap), MinIO, all in Docker.
Python 3.12.

## Ingestion throughput (historical backfill)

| Measure | Value |
|---|---|
| Hacker News stories fetched | 14,551 |
| New signals stored through the full ingestion path | 12,595 |
| Wall-clock time (12 runs) | 933.7 s |
| **Throughput** | **48,561 new signals/hour** |

Full ingestion path per signal: HTTP fetch and JSON parse, gzip + S3 put of raw and extracted text
(content-addressed, skipped if present), canonical URL + content-hash dedupe, alias matching, Postgres
insert, `job_runs`. The bottleneck is our own politeness: a per-domain token bucket of 0.5 requests/s
(burst 2) to hn.algolia.com, with up to 1,000 stories per request. Raw output:
[`backfill-job-runs.txt`](results/2026-10-31-backfill/backfill-job-runs.txt).

## LLM enrichment

| Measure | Value | Source |
|---|---|---|
| Latency per call, gemini-3.5-flash-lite (prompt v3) | p50 1.10 s, p95 4.61 s | [eval run](../eval/results/) |
| Latency per call, prompt v1 / v2 | p50 1.17 s / 1.15 s, p95 1.89 s / 2.24 s | same |
| Input tokens per call | ~1,640 (mostly the shared system prompt) | prototype run |
| Failures | 0 of 404 calls (prototype + 3 eval runs) | same |
| Throughput limit | free tier: 15 requests/min, 500/day; client paces at 12/min, 450/day | AI Studio |

Enrichment is quota-bound, not compute-bound: at 450 requests/day the 5,878-signal backlog takes about
13 days. The queue is prioritized (fresh signals first, then official sources before news before forum
posts) so live signals never wait behind the backfill.

## Search latency

**Not yet measured at the target scale.** The search API, mapping and query builder are built and tested
(`tests/integration/test_api.py`, `test_pipeline.py`), the Locust load test is in
[`load-test/locustfile.py`](../load-test/locustfile.py), and a 15-second smoke run against an almost
empty index succeeded with 0 failures. That run doesn't represent load at scale, so it isn't reported as
a result. The real run (50 users, 5 minutes, warmed caches) waits until the insight index holds the
enriched backlog; results will go in `docs/results/<date>-search/`.

The index is built for it: one shard, strict mapping, filters in `filter` context (cacheable),
`_source` limited to the fields the API returns, and a 5 s refresh interval.

## Optimizations (before → after, measured)

| Change | Before | After | Evidence |
|---|---|---|---|
| Pre-LLM low-value title filter | 35 of 35 irrelevant eval signals sent to the LLM | 19 of 35 (16 dropped), 0 relevant dropped | [relevance-and-clustering.txt](results/2026-10-31-processing/relevance-and-clustering.txt) |
| Ranking half-life 48 h → 7 days | #1 insight was an importance-2 blog post | #1–5 are real launches, outages, pricing changes | [ranking-variants.txt](results/2026-10-31-prototype/ranking-variants.txt) |
| Clustering: TF-IDF across categories | 5 multi-signal clusters (59 insights) | 10 (50 insights), 0 false merges found | [relevance-and-clustering.txt](results/2026-10-31-processing/relevance-and-clustering.txt) |
| Search: OR semantics instead of "all words" for short queries | "layoffs workforce" → 0 hits | → the ClickUp layoff cluster | [es-8.19.5-smoke.txt](results/2026-10-31-search-smoke/es-8.19.5-smoke.txt) |
| Gemini model + pacing | 3.8 Flash: 503s, 20 requests/day | Flash-Lite: 0 failures, 500/day | AI Studio limits; eval runs |
