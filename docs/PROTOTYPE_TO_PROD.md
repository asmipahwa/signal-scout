# From prototype to production

Sprint 1 asked one question before building any infrastructure: **if we pull real public signals about competitors and have an LLM classify and summarize them, do the top-ranked results read like something a sales rep would want?**

The prototype is one throwaway script ([`prototype/scout.py`](../prototype/scout.py)): fetch → filter → LLM enrichment → cluster → rank → print. No database, no queue. This document records what it showed, with numbers from a real run, and what that means for the production build.

## The run

- **Date:** 2026-10-31 02:25 UTC. Competitors: Asana, ClickUp, Notion. Lookback: 180 days.
- **Model:** `gemini-3.5-flash-lite` on the Gemini free tier, through the production `LLMClient` adapter (the prototype imports it).
- **Raw evidence:** [`docs/results/2026-10-31-prototype/`](results/2026-10-31-prototype/): the full report, every signal with its enrichment and token usage (`run.json`), and the ranking comparison.

### Funnel

| Stage | Count | Notes |
|---|---:|---|
| Fetched | 803 | Bing News RSS (10 query variants × 3 competitors), HN Algolia, PR Newswire, TechCrunch, VentureBeat, The Verge, SaaStr, Notion release notes |
| Older than 180 days | −261 | Mostly Notion's release-notes archive and older Bing results |
| No tracked competitor in title/snippet | −432 | Mostly HN fuzzy-search hits and general tech feeds |
| Duplicate URL | −9 | |
| **Sent to the LLM** | **101** | 62 news, 20 changelog, 19 HN |
| LLM: not actually about a tracked competitor | −26 | See "What worked" |
| **Relevant, enriched** | **75** | 0 enrichment failures |
| Insights after clustering | 69 → **59** | 59 after dropping importance-1 items (see ranking fix) |

The original goal was ~200 signals; legitimate sources gave 101 relevant ones for three competitors over six months. That number drives the production source plan below.

### LLM cost and speed

| Metric | Value |
|---|---|
| Calls | 101 (no retries for bad output) |
| Tokens | 165,195 input, 12,837 output, 277 thinking |
| Mean input per call | 1,636 tokens (almost all of it the shared system prompt) |
| Actual cost | **$0.00** (free tier) |
| Paid-tier equivalent | $0.082 for the run, **$0.82 per 1,000 enriched signals** (Flash-Lite list prices, 2026-10-30) |
| Latency per call | p50 1.11 s, p95 10.62 s |

The p95 is high. It was measured during a period when Google was returning 503 "high demand" errors for `gemini-3.8-flash`; my unconfirmed guess is that it includes the SDK's internal retries. To check in Sprint 3.

## What worked

- **The core idea holds.** The top of the corrected ranking is the kind of thing a rep would want: a Notion outage, Notion's AI pricing moving to a credit system, an Asana outage, Notion's agent relaunch, a ClickUp channel partnership. Each comes with a one-line "why it matters" that is usable in a deal.
- **The LLM is a good second relevance filter.** The cheap alias filter let through 26 signals that weren't really about a competitor, and the model caught them: "Asana vs Basecamp" style comparison listicles, pricing explainers, Show HN posts that merely mention Notion, and **"Asana Partners"**, a real-estate firm renovating a mall in Austin.
- **Structured output was reliable.** 101 of 101 responses parsed and validated on the first try.
- **The provider abstraction paid off on day one.** `gemini-3.8-flash` was returning 503s, so the run switched to `gemini-3.5-flash-lite` with one environment variable and no code changes.

## What didn't work, and the fix

| Problem | Evidence | Fix |
|---|---|---|
| **Ranking favored freshness over importance.** The #1 item was an importance-2 blog post about an Asana–Salesforce integration, above an importance-5 pricing change, only because it was newer. | [`ranking-variants.txt`](results/2026-10-31-prototype/ranking-variants.txt), variant A | Half-life 48 h → **7 days**, and importance-1 items are excluded from the feed (variants B and C). Applied to the prototype; production makes both config values. |
| **Clustering missed the same story told differently.** Five articles about Notion's AI credits and 24-hour agents ended up as separate insights, split across three categories. | Variant D: clustering across categories merged only one more pair | Shared-word (Jaccard) matching on titles is too weak. Production: cluster on competitor + time window + summary similarity (TF-IDF cosine or MinHash), *not* exact category. |
| **The cheap filter is loose.** 26% of what reached the LLM was irrelevant. | 26 of 101 marked not relevant | Free tier makes this cheap today, but every call counts against the daily quota. Add title heuristics for comparison/listicle/explainer patterns before the LLM. |
| **Snippets are thin.** Bing gives roughly a 200-character snippet; GDELT gives only a title. | Lower `confidence` on snippet-only items | Fetch and extract article text (trafilatura) where robots.txt allows, store it in S3, send up to ~1,500 tokens. |
| **Some sources aren't usable.** | Google News RSS: disallowed by robots.txt. GDELT: HTTP 429 on almost every request. investors.asana.com: HTTP 403. Asana/ClickUp: no blog RSS found. | Production source list uses only verified, allowed feeds; GDELT only for slow historical backfill at its documented 1 request / 5 s. |
| **No prompt caching on this tier.** | `cache_read_tokens` = 0 on every call | Flash-Lite has no free-tier caching. On a paid tier or with Claude, the stable system prompt would be cached; prompt layout already supports it. |
| **An overloaded model crashed the first run.** | A live 503 escaped the adapter | Fixed in the adapters: 5xx and 429 back off and retry; daily quota stops cleanly; 4xx fails only that signal. Tested with the real error body. |

## Decisions for production

1. **Volume comes from breadth, not depth.** About 100 relevant signals per 3 competitors per 6 months means the production build needs all 8 competitors, more verified feeds, webpage-change monitoring, and multi-year historical backfill (HN history, GDELT at its rate limit, feed archives). Most ingested signals will be filtered out before the LLM, and the funnel will be reported openly.
2. **The daily free-tier quota is the bottleneck, not money.** Every LLM call must earn its place: dedupe and filter first, and run the backfill as a resumable job that stops at the daily limit and continues the next day.
3. **The model choice is settled by the eval, not by this run.** Sprint 3's 100-signal labeled eval compares Flash-Lite, Flash, and Claude Haiku on the same prompt.
4. **Keep the parts that were promoted early.** The enrichment package (`src/signal_scout/enrichment/`) is already production code with tests; Sprint 2 builds storage, queue, and ingestion around it.
