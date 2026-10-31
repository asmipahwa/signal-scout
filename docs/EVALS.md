# LLM enrichment evals

How good is the enrichment, and did a change actually help? Every number here comes from
`scripts/run_eval.py` over the labeled set in [`eval/labeled.jsonl`](../eval/labeled.jsonl); raw
per-signal results are in [`eval/results/`](../eval/results/).

## The eval set

- **101 real signals** from the 2026-10-31 prototype run (news, Hacker News, Notion changelog) about
  Asana, ClickUp, and Notion: 66 relevant, 35 not relevant.
- Gold labels: is it really about a tracked competitor, which competitor, category, importance (1–5).
- **Caveats that affect every number below:**
  - Labels were drafted by Claude Code and are **not yet reviewed by a human** (`reviewed_by_human: false`).
  - The labeler had already seen `gemini-3.5-flash-lite`'s prototype output for about 40 of the signals,
    so labels may lean toward agreeing with it.
  - There is no held-out split. Prompt v2 was written after looking at v1's errors on this same set, so
    v2's improvement is an optimistic estimate.
  - 20 Notion changelog items have no body text (Notion's RSS ships titles only).

## Metrics

| Metric | What it answers |
|---|---|
| Relevance accuracy / precision / recall | Does it reject signals that aren't really about a competitor, without dropping real ones? |
| Category accuracy | On relevant signals, is the event type right? |
| Importance exact / within ±1 | Is the 1–5 rating usable for ranking? |
| Competitor precision / recall | Does it name the right company? |
| Cost, latency, tokens | What does it cost to run? |

## Results

| Run | Relevance acc. | Precision | Recall | Category | Importance exact | Importance ±1 | Failures | Cost | Latency p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| gemini-3.5-flash-lite, prompt v1 | 90.1% | 87.8% | 98.5% | 80.3% | 42.4% | 93.9% | 0 / 101 | $0 (free tier) | 1.17 s / 1.89 s |
| gemini-3.5-flash-lite, prompt v2 | 95.0% | 96.9% | 95.5% | 81.8% | 56.1% | 97.0% | 0 / 101 | $0 (free tier) | 1.15 s / 2.24 s |
| **gemini-3.5-flash-lite, prompt v3 (default)** | 93.1% | 90.4% | **100%** | 81.8% | **62.1%** | 97.0% | 0 / 101 | $0 (free tier) | 1.10 s / 4.61 s |

### What reaches the feed

Aggregate accuracy hides what matters for the product: importance-1 items never reach the feed, so a
false positive rated 1 costs nothing, while a missed pricing change costs a deal. Scored on what the
pipeline actually shows (relevant and importance ≥ 2):

| Prompt | Important signals (gold importance ≥ 3, n=38) that reach the feed | Feed items that aren't relevant |
|---|---:|---:|
| v1 | 100% | 2 of 65 (3.1%) |
| v2 | 94.7% | 1 of 61 (1.6%) |
| v3 | **100%** | 3 of 67 (4.5%) |

**Decision: v3 is the default.** It never drops an important signal and has the best importance
accuracy (62.1% exact vs 42.4% for v1), at the cost of one more irrelevant item in a 67-item feed.
The v3 p95 latency of 4.61 s is likely one or two retried calls (single run; not investigated).

Competitor precision/recall equal relevance precision/recall in this run: whenever the model judged a
signal relevant, it named the correct single competitor.

### What v1 got wrong

- **False positives (9):** 7 are reviews, explainers, switching stories, or third-party tools that the
  model marked "about" the competitor but rated importance 1 with category `other`. Those never reach
  the feed (importance-1 items are excluded), so the practical harm is small. The other 2 (an
  Asana–Salesforce how-to and Asana as one of many apps in a Cloudflare integration list) were
  borderline calls in the labels too.
- **False negative (1):** "Merge cells in simple tables" (a Notion changelog title with no body).
- **Importance runs high:** +1 above the label on 29 of 65 relevant signals, −1 on only 4.
- **Category confusion:** mostly `product_launch` ↔ `feature_update` (5 of 12 errors).

### Prompt v2

Three targeted edits to v1 ([`prompt.py`](../src/signal_scout/enrichment/prompt.py), `_V2_EDITS`):

1. Explicit "not about the competitor" cases: reviews, comparisons, pricing explainers, tutorials,
   switching stories, third-party integrations, engineering blogs.
2. A sharper boundary: `product_launch` is a new standalone product or a marketed major release;
   additions to an existing product, including AI features, are `feature_update`.
3. Conservative importance calibration: most changelog entries are 2, notable releases 3, 4 only if a rep
   would change what they say in a deal, and "when torn, choose the lower".

v2 results: precision jumped (87.8% → 96.9%) but recall fell (98.5% → 95.5%), and two of the three
missed signals were importance-4 pricing items: an Asana pricing analysis whose Bing snippet was only a
Yahoo Finance disclaimer, and a consultant's post on Notion's new AI pricing that v2 treated as a
tutorial. Its precision gain mostly removed importance-1 noise that never reached the feed anyway.

### Prompt v3

v1 plus only v2's category and importance edits, dropping the stricter relevance rule. This is a
principled ablation (remove the edit that hurt at feed level), but it was still chosen on the same
labeled set, so treat the comparison as indicative until a held-out set exists.

## Next

- Human review of the labels (`reviewed_by_human`), then rerun all three versions.
- A held-out set of fresh signals from the live pipeline to confirm v3 without tuning bias.
- Claude (`claude-haiku-5-5`) on the same set when an API key is available: `LLM_PROVIDER=claude scripts/with-secrets.sh uv run python scripts/run_eval.py`.
