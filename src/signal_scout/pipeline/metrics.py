"""Prometheus metrics for the pipeline, recorded once per job from its RunStats.

Jobs already report what they did (counts, tokens, cost, per-outcome details) to `job_runs`; turning the
same numbers into counters in one place keeps the jobs free of metrics code and guarantees Prometheus
and `job_runs` agree. The worker exposes these on METRICS_PORT (default 9100) for Prometheus to scrape.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from ..jobruns import RunStats
from ..metrics import REGISTRY

JOB_DURATION_SECONDS = Histogram(
    "job_duration_seconds",
    "Wall time of one job execution.",
    labelnames=("job_type",),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900),
    registry=REGISTRY,
)
JOB_FAILURES_TOTAL = Counter("job_failures_total", "Jobs that raised.", labelnames=("job_type",), registry=REGISTRY)
SIGNALS_INGESTED_TOTAL = Counter(
    "signals_ingested_total", "New signals stored.", labelnames=("source_type",), registry=REGISTRY
)
SIGNALS_FILTERED_TOTAL = Counter(
    "signals_filtered_total", "Signals dropped before the LLM.", labelnames=("reason",), registry=REGISTRY
)
SIGNALS_ENRICHED_TOTAL = Counter(
    "signals_enriched_total", "Signals the LLM judged relevant.", labelnames=("category",), registry=REGISTRY
)
ENRICHMENT_FAILURES_TOTAL = Counter("enrichment_failures_total", "Signals whose enrichment failed.", registry=REGISTRY)
LLM_TOKENS_TOTAL = Counter("llm_tokens_total", "LLM tokens used.", labelnames=("type",), registry=REGISTRY)
LLM_COST_USD_TOTAL = Counter("llm_cost_usd_total", "Paid LLM spend in USD.", registry=REGISTRY)
LLM_COST_USD_TODAY = Gauge("llm_cost_usd_today", "Paid LLM spend so far today (UTC).", registry=REGISTRY)
LLM_DAILY_CAP_USD = Gauge("llm_daily_cap_usd", "CostGuard daily cap.", registry=REGISTRY)
WEBHOOK_DELIVERIES_TOTAL = Counter(
    "webhook_deliveries_total", "Outbound webhook attempts.", labelnames=("status",), registry=REGISTRY
)
SOURCE_CIRCUIT_OPEN = Counter(
    "source_circuit_open_total", "Sources disabled by the circuit breaker.", labelnames=("source",), registry=REGISTRY
)

FILTER_OUTCOMES = ("no_competitor", "low_value", "near_duplicate")


def record_job(run: RunStats, duration_s: float, failed: bool) -> None:
    JOB_DURATION_SECONDS.labels(run.job_type).observe(duration_s)
    if failed:
        JOB_FAILURES_TOTAL.labels(run.job_type).inc()
    d = run.details
    if run.job_type in ("fetch_source", "watch_page", "backfill_hn") and (stype := d.get("source_type")):
        SIGNALS_INGESTED_TOTAL.labels(stype).inc(int(d.get("new_signals", 0)))
    if run.job_type == "enrich_signals":
        outcomes = d.get("outcomes", {})
        for reason in FILTER_OUTCOMES:
            if outcomes.get(reason):
                SIGNALS_FILTERED_TOTAL.labels(reason).inc(outcomes[reason])
        for category, n in d.get("categories", {}).items():
            SIGNALS_ENRICHED_TOTAL.labels(category).inc(n)
        if outcomes.get("enrichment_failed"):
            ENRICHMENT_FAILURES_TOTAL.inc(outcomes["enrichment_failed"])
        for kind, n in d.get("tokens", {}).items():
            LLM_TOKENS_TOTAL.labels(kind).inc(n)
        LLM_COST_USD_TOTAL.inc(float(run.cost_usd))
        if "spend_today_usd" in d:
            LLM_COST_USD_TODAY.set(d["spend_today_usd"])
            LLM_DAILY_CAP_USD.set(d["daily_cap_usd"])
    if run.job_type == "deliver_webhook" and (status := d.get("delivery_status")):
        WEBHOOK_DELIVERIES_TOTAL.labels(status).inc()
    if d.get("circuit_opened"):
        SOURCE_CIRCUIT_OPEN.labels(str(d.get("source", "unknown"))).inc()
