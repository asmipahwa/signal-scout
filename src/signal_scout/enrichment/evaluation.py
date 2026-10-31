"""Score LLM enrichments against hand-labeled signals.

Why this exists: "the model seems good" isn't evidence. The eval runs the same labeled signals
through any provider/model and reports the numbers that matter for the product: does it reject
irrelevant items, pick the right category, rate importance sensibly, and name the right
competitor, and what does that cost and how long does it take.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from pydantic import BaseModel

from .models import Category, Enrichment, LlmUsage


class GoldLabel(BaseModel):
    is_about_tracked_competitor: bool
    competitors: list[str]
    category: Category
    importance: int


@dataclass
class EvalCase:
    id: str
    gold: GoldLabel
    predicted: Enrichment | None  # None when enrichment failed
    usage: LlmUsage | None


@dataclass
class EvalReport:
    n: int
    failures: int
    relevance_accuracy: float
    relevance_precision: float
    relevance_recall: float
    category_accuracy: float  # over signals that are relevant in the gold labels
    importance_exact: float
    importance_within_1: float
    competitor_precision: float
    competitor_recall: float
    cost_usd: float
    latency_p50_s: float
    latency_p95_s: float
    input_tokens: int
    output_tokens: int


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))]


def score(cases: list[EvalCase]) -> EvalReport:
    """A failed enrichment counts as wrong on every metric it would have contributed to."""
    ok = [c for c in cases if c.predicted is not None]
    gold_relevant = [c for c in cases if c.gold.is_about_tracked_competitor]

    def pred_relevant(c: EvalCase) -> bool:
        return c.predicted is not None and c.predicted.is_about_tracked_competitor and bool(c.predicted.competitors)

    tp = sum(1 for c in cases if pred_relevant(c) and c.gold.is_about_tracked_competitor)
    predicted_pos = sum(1 for c in cases if pred_relevant(c))
    correct_relevance = sum(
        1 for c in cases if c.predicted is not None and pred_relevant(c) == c.gold.is_about_tracked_competitor
    )

    cat_ok = sum(1 for c in gold_relevant if c.predicted is not None and c.predicted.category == c.gold.category)
    imp_exact = sum(1 for c in gold_relevant if c.predicted is not None and c.predicted.importance == c.gold.importance)
    imp_close = sum(
        1 for c in gold_relevant if c.predicted is not None and abs(c.predicted.importance - c.gold.importance) <= 1
    )

    comp_tp = comp_pred = comp_gold = 0
    for c in cases:
        gold = set(c.gold.competitors) if c.gold.is_about_tracked_competitor else set()
        pred = set(c.predicted.competitors) if c.predicted is not None and pred_relevant(c) else set()
        comp_tp += len(gold & pred)
        comp_pred += len(pred)
        comp_gold += len(gold)

    usages = [c.usage for c in cases if c.usage is not None]
    latencies = [u.latency_s for u in usages]
    return EvalReport(
        n=len(cases),
        failures=len(cases) - len(ok),
        relevance_accuracy=_ratio(correct_relevance, len(cases)),
        relevance_precision=_ratio(tp, predicted_pos),
        relevance_recall=_ratio(tp, len(gold_relevant)),
        category_accuracy=_ratio(cat_ok, len(gold_relevant)),
        importance_exact=_ratio(imp_exact, len(gold_relevant)),
        importance_within_1=_ratio(imp_close, len(gold_relevant)),
        competitor_precision=_ratio(comp_tp, comp_pred),
        competitor_recall=_ratio(comp_tp, comp_gold),
        cost_usd=sum(u.cost_usd for u in usages),
        latency_p50_s=statistics.median(latencies) if latencies else 0.0,
        latency_p95_s=_percentile(latencies, 0.95),
        input_tokens=sum(u.input_tokens + u.cache_read_tokens + u.cache_write_tokens for u in usages),
        output_tokens=sum(u.output_tokens + u.thinking_tokens for u in usages),
    )
