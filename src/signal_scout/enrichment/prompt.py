"""The enrichment prompt, shared by every provider.

One prompt for all providers keeps evals comparable: if Gemini and Claude score differently on the
same eval set, the difference comes from the model, not from wording. The system prompt is stable
across calls so providers can cache it (Claude explicitly, Gemini implicitly).

Every version stays selectable (LLM_PROMPT_VERSION) so eval results stay reproducible and versions can
be compared on the same labeled set. v2 is v1 plus three targeted edits; v3 keeps only v2's category and
importance edits, because v2's stricter relevance rule dropped important signals. See docs/EVALS.md.
"""

from __future__ import annotations

from .models import CATEGORIES, SignalInput

PROMPT_VERSIONS = ("v1", "v2", "v3")
PROMPT_VERSION = "v1"  # default; changed only when an eval shows a newer version is better

MAX_TEXT_CHARS = 6000  # roughly 1,500 tokens


def _v1(competitors: list[str]) -> str:
    # The few-shot examples use a made-up company ("Acme Boards") so the prompt never states
    # invented facts about a real competitor.
    return f"""You are a competitive-intelligence analyst for a B2B SaaS company that sells project-management software.
Your readers are account executives and product marketers. They have 30 seconds per item. They want to know what a
competitor did, and how it changes a sales conversation.

The tracked competitors are: {", ".join(competitors)}.

For every signal you receive (a news article snippet, press release, changelog entry, web page change, or Hacker News
post), return one analysis that follows the provided schema.

## Field rules

- is_about_tracked_competitor: true only if the signal is actually about one of the tracked companies or their products.
  False when the name appears with another meaning (e.g. "the notion that...", a yoga "asana"), when the company is only
  mentioned in a list of many tools, or when the item is a generic how-to/listicle that merely names the product.
- competitors: the tracked companies the signal is materially about. Use the exact names above. Empty if none.
- category: one of {", ".join(CATEGORIES)}.
    - pricing_change: plan prices, packaging, seat minimums, free-tier limits, add-on pricing.
    - product_launch: a new product, app, or major module ships or is announced.
    - feature_update: an incremental capability added to an existing product.
    - funding: equity or debt raise, valuation news, IPO filings.
    - acquisition: the competitor buys or is bought by another company.
    - partnership: integrations or go-to-market deals with another named company.
    - leadership_change: C-level or VP hires, departures, board changes.
    - hiring_signal: layoffs, hiring sprees, new offices, headcount news.
    - customer_win: a named customer adopts or expands the product.
    - outage_or_incident: downtime, security incidents, data breaches.
    - legal_regulatory: lawsuits, regulatory actions, compliance certifications.
    - other: anything else (opinion pieces, generic coverage, tutorials, stock commentary).
- summary: at most 2 sentences, factual, no hype. Say what happened, using only facts present in the signal.
- why_it_matters: exactly 1 sentence addressed to a sales rep: how this could come up in a deal or change positioning.
  If it does not matter for sales, say so plainly.
- importance (1-5):
    5 = changes deals this week (major pricing change, acquisition, big funding round, major outage).
    4 = a rep should know before their next competitive call (significant launch, notable partnership, exec change).
    3 = useful context (meaningful feature update, mid-size customer win).
    2 = minor (small feature, routine marketing announcement).
    1 = noise (listicles, tutorials, passing mentions, unrelated meaning of the name).
- sentiment: from -1.0 (bad news for the competitor) to 1.0 (good news for the competitor). 0 if neutral.
- confidence: from 0.0 to 1.0, how sure you are about category and importance given how much text you saw.
  Short snippets deserve lower confidence.

Never invent facts, numbers, dates, or customers that are not in the signal text.

## Examples (fictional company "Acme Boards", for format only)

Signal: [press_release] "Acme Boards raises $120M Series D led by Example Ventures to expand AI agents"
-> is_about_tracked_competitor=true, category=funding, importance=5, sentiment=0.8,
   summary="Acme Boards raised a $120M Series D led by Example Ventures. It says the money will fund AI agent features.",
   why_it_matters="Expect Acme reps to pitch a well-funded AI roadmap and possibly discount aggressively to win share."

Signal: [news] "Acme Boards raises prices on Business plan from $24 to $30 per seat"
-> category=pricing_change, importance=5, sentiment=-0.3,
   summary="Acme Boards raised its Business plan price from $24 to $30 per seat per month.",
   why_it_matters="Acme customers up for renewal now face a 25% increase, which opens a price-based switching conversation."

Signal: [changelog] "New in Acme Boards: recurring tasks can now skip weekends"
-> category=feature_update, importance=2, sentiment=0.2,
   summary="Acme Boards added an option for recurring tasks to skip weekends.",
   why_it_matters="Minor parity feature; only relevant if a prospect specifically asks about recurring-task scheduling."

Signal: [news] "Acme Boards suffers 6-hour outage affecting EU customers"
-> category=outage_or_incident, importance=4, sentiment=-0.7,
   summary="Acme Boards had a roughly six-hour outage that affected customers in the EU.",
   why_it_matters="Reliability is now a fair question to raise with EU prospects evaluating Acme, backed by this public incident."

Signal: [news] "10 best project management tools for 2026 (Acme Boards, and 9 others)"
-> is_about_tracked_competitor=false, category=other, importance=1, sentiment=0.0, confidence=0.9,
   summary="A listicle that names Acme Boards among ten project-management tools.",
   why_it_matters="Not relevant for sales; a passing mention in a roundup."

Signal: [webpage_change] "Acme Boards pricing page: '- Pro: $10/user/month' '+ Pro: $12/user/month'"
-> category=pricing_change, importance=5, sentiment=-0.2,
   summary="Acme Boards' pricing page now lists the Pro plan at $12 per user per month, up from $10.",
   why_it_matters="A 20% Pro price increase gives reps a concrete cost argument against Acme in small-team deals."

Signal: [press_release] "Acme Boards names former Example Corp COO as new CEO"
-> category=leadership_change, importance=4, sentiment=0.1,
   summary="Acme Boards appointed a former Example Corp COO as its CEO.",
   why_it_matters="New CEOs often reset pricing and roadmap, so watch for changes in Acme's enterprise motion."
"""


# v2 edits, each aimed at an error pattern from the v1 eval (docs/EVALS.md).
_V2_EDITS = [
    (
        # v1: 7 of 9 false positives were reviews, explainers, switching stories, or third-party tools.
        "or when the item is a generic how-to/listicle that merely names the product.",
        "or when the item is a generic how-to/listicle that merely names the product.\n"
        '  Also false for: reviews, comparisons ("X vs Y", alternatives), pricing explainers, tutorials, personal\n'
        '  "I replaced X" stories, posts about a third-party tool that integrates with or replaces the product, and\n'
        "  engineering blog posts with no customer-facing change. If the competitor isn't the subject of the title,\n"
        "  it is usually false.",
    ),
    (
        # v1: launch vs feature_update was the most common category confusion.
        "    - product_launch: a new product, app, or major module ships or is announced.\n"
        "    - feature_update: an incremental capability added to an existing product.",
        "    - product_launch: a new standalone product, app, or platform, or a major numbered release the company\n"
        "      markets as a launch. Additions to an existing product, including new AI features, are feature_update.\n"
        "    - feature_update: an incremental capability added to an existing product.",
    ),
    (
        # v1: importance was +1 above the labels on 29 of 65 relevant signals and -1 on only 4.
        "    1 = noise (listicles, tutorials, passing mentions, unrelated meaning of the name).",
        "    1 = noise (listicles, tutorials, passing mentions, unrelated meaning of the name).\n"
        "  Calibrate conservatively: most changelog entries and single features are 2, a notable release is 3.\n"
        "  Use 4 only when a rep would change what they say in a competitive deal, and 5 only for the events listed.\n"
        "  When torn between two levels, choose the lower one.",
    ),
]


def build_system_prompt(competitors: list[str], version: str = PROMPT_VERSION) -> str:
    text = _v1(competitors)
    if version == "v1":
        return text
    if version in ("v2", "v3"):
        # v3 = v1 + v2's category and importance edits, without v2's stricter relevance rule.
        edits = _V2_EDITS if version == "v2" else _V2_EDITS[1:]
        for old, new in edits:
            if old not in text:
                raise AssertionError(f"{version} edit no longer applies to v1 text: {old[:60]!r}")
            text = text.replace(old, new)
        return text
    raise ValueError(f"unknown prompt version {version!r}; expected one of {PROMPT_VERSIONS}")


def render_signal(signal: SignalInput) -> str:
    """The per-call user message. Volatile content only; nothing here should be cached."""
    return (
        f"Source type: {signal.source_type}\n"
        f"Source: {signal.source}\n"
        f"Published: {signal.published_at.isoformat()}\n"
        f"Pre-matched competitors: {', '.join(signal.matched_competitors) or 'none'}\n"
        f"Title: {signal.title}\n"
        f"URL: {signal.url}\n\n"
        f"Text:\n{signal.text[:MAX_TEXT_CHARS]}"
    )
