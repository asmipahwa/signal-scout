"""Re-rank a saved prototype run under different ranking settings, with no LLM calls.

Ranking and clustering only use the stored enrichments, so tuning them is free. This produced
docs/results/2026-10-31-prototype/ranking-variants.txt.

Usage (from the repo root):
    uv run python prototype/rerank.py docs/results/2026-10-31-prototype/run.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scout  # noqa: E402

VARIANTS = [
    ("A baseline (48h, all, same-category)", 48, 1, False),
    ("B half-life 7d", 168, 1, False),
    ("C half-life 7d + drop importance 1", 168, 2, False),
    ("D = C + cluster across categories", 168, 2, True),
]


def rank_variant(run: dict, half_life: int, min_importance: int, cross_category: bool) -> list[scout.Insight]:
    scout.HALF_LIFE_HOURS = half_life
    signals = [scout.Signal(**s) for s in run["signals"] if s["status"] == "enriched"]
    signals = [s for s in signals if s.enrichment["importance"] >= min_importance]
    if cross_category:
        # Keep the real category for display, but let clustering ignore it.
        for s in signals:
            s.enrichment = {**s.enrichment, "_cat": s.enrichment["category"], "category": "any"}
    insights = scout.cluster(signals)
    scout.rank(insights, datetime.fromisoformat(run["generated_at"]))
    return sorted(insights, key=lambda i: i.rank_score, reverse=True)


def main() -> None:
    run = json.loads(Path(sys.argv[1]).read_text())
    for label, half_life, min_importance, cross_category in VARIANTS:
        insights = rank_variant(run, half_life, min_importance, cross_category)
        multi = sum(len(i.signals) > 1 for i in insights)
        print(f"\n## {label}: {len(insights)} insights; multi-signal clusters: {multi}")
        for k, ins in enumerate(insights[:8], 1):
            e = ins.lead.enrichment
            importance = max(s.enrichment["importance"] for s in ins.signals)
            category = e.get("_cat", e["category"])
            print(
                f"{k}. {ins.rank_score:.3f} [{ins.competitor} | {category} | imp {importance} | "
                f"{len(ins.signals)} src] {ins.lead.title[:80]}"
            )


if __name__ == "__main__":
    main()
