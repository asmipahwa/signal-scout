# Observability

How to tell whether the pipeline is healthy, and how to debug it when it isn't. Three layers, each
answering a different question:

| Layer | Answers | Where |
|---|---|---|
| `job_runs` table | Did job X run, when, how long, what went in and out, what failed and why? | Postgres; `make runs-report`; `GET /api/v1/runs` |
| Structured logs | What happened inside one run? | JSON lines (structlog); every line of a run carries its `run_id` and `job_type` |
| Prometheus metrics | Is it healthy *right now*, and how fast is it? | API `:8000/metrics`, workers `:9100/metrics`; Grafana dashboard |

## job_runs: the audit log

Every job (fetch, page watch, enrich, build insights, index, alerts, webhook delivery, backfill, reindex,
seed, scheduler tick, and every MCP tool call) writes a `job_runs` row when it starts and updates it when
it finishes: status, `items_in`, `items_out`, `errors`, `last_error`, tokens, cost, and a JSON `details`
blob (for example, enrichment outcomes by reason, or the HTTP status of a feed fetch). Failures are
recorded and re-raised, so the queue still retries the job. `run_id` links the row to its log lines.

`make runs-report` prints the last 24 hours by job type plus the full funnel by source type.
`make source-report` prints yield per source. Both read only from Postgres.

## Logs

structlog writes one JSON object per line. Context is bound per run (`run_id`, `job_type`, `source_id`,
`signal_id`), so `grep <run_id>` reconstructs a run. Secrets are redacted before anything is written:
keys that look like secrets, and passwords inside URLs (`src/signal_scout/log.py`, unit-tested).

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `signals_ingested_total` | counter | `source_type` |
| `signals_filtered_total` | counter | `reason` (`no_competitor`, `low_value`, `near_duplicate`) |
| `signals_enriched_total` | counter | `category` |
| `enrichment_failures_total` | counter | |
| `llm_tokens_total` | counter | `type` (input, output, cache_read, cache_write, thinking) |
| `llm_cost_usd_total`, `llm_cost_usd_today`, `llm_daily_cap_usd` | counter / gauges | |
| `job_duration_seconds` | histogram | `job_type` |
| `job_failures_total` | counter | `job_type` |
| `webhook_deliveries_total` | counter | `status` (delivered, retry, failed) |
| `source_circuit_open_total` | counter | `source` |
| `http_request_seconds` | histogram | `route`, `method`, `status` |
| `search_request_seconds` | histogram | |
| `mcp_tool_seconds` | histogram | `tool`, `outcome` |

Pipeline metrics are derived from each job's `RunStats` in one place (`pipeline/metrics.py`), so they
always agree with `job_runs`. Prometheus discovers every worker replica through DNS.

## Dashboard and alerts

`make observe` (or `make app`) starts Prometheus and Grafana; the dashboard is provisioned from
[`ops/grafana/dashboards/signal-scout.json`](../ops/grafana/dashboards/signal-scout.json). Pipeline panels
query Postgres directly (cumulative signals by source type, signals per day, funnel by source type, LLM
calls and spend, job runs, webhook deliveries); latency panels query Prometheus (search p50/p95/p99, HTTP
p95 by route).

Alert rules ([`ops/prometheus/alerts.yml`](../ops/prometheus/alerts.yml)), all loaded and evaluated by
Prometheus:

| Alert | Condition |
|---|---|
| `NoSignalsIngested` | no new signals for 2 hours |
| `EnrichmentFailureRateHigh` | > 10% of enrichments failing over an hour, for 15 min |
| `SearchLatencyP95High` | search p95 > 200 ms for 10 min |
| `LlmDailySpendNearCap` | paid spend today > 80% of the daily cap |

Routing alerts to Slack or a pager needs an Alertmanager, deliberately left out of this single-machine setup.

## Debugging walkthrough: a source that quietly polluted the funnel (real, 2026-10-31)

**Symptom.** While watching the historical Hacker News backfill, its per-source progress lines (the
`backfill_source_done` log events, also in `job_runs.details`) showed one source fetching far more than
its peers: `HN: Linear` fetched 3,914 stories, as many as `HN: Notion`, for a product with much less
public discussion.

**Narrowing it down.** The `job_runs` rows confirmed the volume (3,914 in, 3,349 stored). The signals
table showed what kind of signals they were.
Grouping its signals by status in Postgres:

```sql
SELECT sig.status, count(*) FROM signals sig JOIN sources s ON s.id = sig.source_id
WHERE s.name = 'HN: Linear' GROUP BY 1;
-- new 68 | filtered_out 3173 | duplicate 156
```

**Root cause.** The seed queried HN for the bare word `Linear`. Even with exact matching, that returns
stories about linear algebra, linear regression and "linear" anything: 3,914 stories, of which 68 mention
the product by its aliases (`linear.app`, `Linear app`). The alias filter did its job (nothing irrelevant
reached the LLM), but the ingestion counts were inflated by off-topic data.

**Fix.** The source now queries `linear.app` with exact matching, the 3,329 off-topic signals were
deleted, and the source was re-backfilled (75 new real signals). Recorded in
[`docs/results/2026-10-31-backfill/NOTES.md`](results/2026-10-31-backfill/NOTES.md).

**Lesson.** Volume metrics need a per-source quality check. `make source-report` (signals vs. relevant
per source) is that check in one command; it shows this kind of problem immediately.

## Second incident: an overloaded model crashed a run (real, 2026-10-31)

During the first prototype enrichment, `gemini-3.8-flash` answered `503 UNAVAILABLE: This model is
currently experiencing high demand`. The adapter only knew about 429s, so the exception escaped and
killed the run. The fix split provider failures into transient (429, 5xx, network: back off and retry),
quota (per-day 429, out of credit: stop the run, resume after reset), and permanent (4xx: fail one
signal). Tests replay the exact 503 body the API returned. In the pipeline, a transient failure now
leaves the signal `deferred` for the next run, and `job_runs.details.stopped` says why a run stopped early.
