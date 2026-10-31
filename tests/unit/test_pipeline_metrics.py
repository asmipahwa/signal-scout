"""Pipeline metrics are derived from each job's RunStats."""

from __future__ import annotations

import uuid
from decimal import Decimal

from signal_scout.jobruns import RunStats
from signal_scout.metrics import REGISTRY
from signal_scout.pipeline.metrics import record_job


def value(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_enrich_run_feeds_signal_llm_and_failure_counters() -> None:
    before = {
        "enriched": value("signals_enriched_total", {"category": "pricing_change"}),
        "filtered": value("signals_filtered_total", {"reason": "low_value"}),
        "failed": value("enrichment_failures_total"),
        "tokens": value("llm_tokens_total", {"type": "input"}),
        "cost": value("llm_cost_usd_total"),
        "jobs": value("job_duration_seconds_count", {"job_type": "enrich_signals"}),
    }
    run = RunStats(job_type="enrich_signals", run_id=uuid.uuid4(), cost_usd=Decimal("0.0021"))
    run.details.update(
        outcomes={"low_value": 3, "enrichment_failed": 1, "enriched": 2},
        categories={"pricing_change": 2},
        tokens={"input": 3200, "output": 400},
        spend_today_usd=0.12,
        daily_cap_usd=0.5,
    )
    record_job(run, duration_s=4.2, failed=False)
    assert value("signals_enriched_total", {"category": "pricing_change"}) - before["enriched"] == 2
    assert value("signals_filtered_total", {"reason": "low_value"}) - before["filtered"] == 3
    assert value("enrichment_failures_total") - before["failed"] == 1
    assert value("llm_tokens_total", {"type": "input"}) - before["tokens"] == 3200
    assert abs(value("llm_cost_usd_total") - before["cost"] - 0.0021) < 1e-9
    assert value("llm_cost_usd_today") == 0.12 and value("llm_daily_cap_usd") == 0.5
    assert value("job_duration_seconds_count", {"job_type": "enrich_signals"}) - before["jobs"] == 1


def test_ingest_and_failure_counters() -> None:
    before_ingested = value("signals_ingested_total", {"source_type": "news_rss"})
    before_failures = value("job_failures_total", {"job_type": "fetch_source"})
    run = RunStats(job_type="fetch_source", run_id=uuid.uuid4())
    run.details.update(source_type="news_rss", new_signals=7)
    record_job(run, duration_s=1.0, failed=False)
    record_job(RunStats(job_type="fetch_source", run_id=uuid.uuid4()), duration_s=0.5, failed=True)
    assert value("signals_ingested_total", {"source_type": "news_rss"}) - before_ingested == 7
    assert value("job_failures_total", {"job_type": "fetch_source"}) - before_failures == 1
