"""Eval scoring math on small hand-made cases."""

from __future__ import annotations

import pytest

from signal_scout.enrichment.evaluation import EvalCase, GoldLabel, score
from signal_scout.enrichment.models import Category, Enrichment, LlmUsage


def gold(relevant: bool, category: Category = "other", importance: int = 1, comp: str = "Asana") -> GoldLabel:
    return GoldLabel(
        is_about_tracked_competitor=relevant,
        competitors=[comp] if relevant else [],
        category=category,
        importance=importance,
    )


def pred(relevant: bool, category: Category = "other", importance: int = 1, comp: str = "Asana") -> Enrichment:
    return Enrichment(
        is_about_tracked_competitor=relevant,
        competitors=[comp] if relevant else [],
        category=category,
        summary="A summary long enough to validate.",
        why_it_matters="A reason long enough to validate.",
        importance=importance,
        sentiment=0.0,
        confidence=0.5,
    )


def usage(latency: float, cost: float = 0.0) -> LlmUsage:
    return LlmUsage(provider="t", model="t", input_tokens=10, output_tokens=5, latency_s=latency, cost_usd=cost)


def test_perfect_predictions_score_one() -> None:
    cases = [
        EvalCase("a", gold(True, "pricing_change", 5), pred(True, "pricing_change", 5), usage(1.0)),
        EvalCase("b", gold(False), pred(False), usage(2.0)),
    ]
    r = score(cases)
    assert r.relevance_accuracy == r.relevance_precision == r.relevance_recall == 1.0
    assert r.category_accuracy == r.importance_exact == r.competitor_precision == r.competitor_recall == 1.0
    assert r.failures == 0


def test_mistakes_and_failures_are_counted() -> None:
    cases = [
        EvalCase("hit", gold(True, "funding", 5), pred(True, "funding", 4), usage(1.0, 0.01)),  # importance off by 1
        EvalCase("wrong_cat", gold(True, "funding", 5), pred(True, "acquisition", 2), usage(1.0, 0.01)),
        EvalCase("false_pos", gold(False), pred(True, "other", 1), usage(3.0)),
        EvalCase("failed", gold(True, "funding", 5), None, None),
    ]
    r = score(cases)
    assert r.failures == 1
    assert r.relevance_accuracy == pytest.approx(2 / 4)  # hit + wrong_cat correct; false_pos wrong; failure wrong
    assert r.relevance_precision == pytest.approx(2 / 3)
    assert r.relevance_recall == pytest.approx(2 / 3)
    assert r.category_accuracy == pytest.approx(1 / 3)
    assert r.importance_exact == 0.0
    assert r.importance_within_1 == pytest.approx(1 / 3)
    assert r.competitor_precision == pytest.approx(2 / 3)
    assert r.competitor_recall == pytest.approx(2 / 3)
    assert r.cost_usd == pytest.approx(0.02)
    assert r.latency_p50_s == 1.0


def test_wrong_competitor_lowers_precision_and_recall() -> None:
    r = score([EvalCase("x", gold(True, comp="Asana"), pred(True, comp="Notion"), usage(1.0))])
    assert r.relevance_recall == 1.0
    assert r.competitor_precision == r.competitor_recall == 0.0
