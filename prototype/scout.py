# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "anthropic>=2.3,<3",
#   "feedparser>=6.0.14",
#   "httpx>=0.28.1",
#   "pydantic>=2.14",
#   "python-dotenv>=1.2",
# ]
# ///
"""Sprint 1 prototype: does "public signals -> Claude -> ranked insights" produce anything useful?

This is deliberately throwaway code. It answers one question before we build infrastructure:
if we pull a few hundred real news / press-release / changelog / Hacker News items for three
competitors and ask Claude Haiku to classify and summarize them, do the top-ranked results read
like something a sales rep would actually want?

Everything runs in one file, in memory, with no database. Sprint 2 rebuilds the parts that
worked as a real service (see docs/PROTOTYPE_TO_PROD.md).

Usage:
    uv run prototype/scout.py --no-llm        # fetch + filter only, free
    uv run prototype/scout.py --reuse         # enrich the last fetched batch again (no refetch)
    uv run prototype/scout.py                 # fetch, enrich, rank, print
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import anthropic
import feedparser
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
OUTPUT_DIR = ROOT / "output"

USER_AGENT = "SignalScout/0.1 (portfolio research crawler; +https://github.com/asmipahwa/signal-scout)"
# 180 days: Bing often returns older items, and 30 days left only ~40 relevant signals for 3 competitors.
LOOKBACK_DAYS = 180

# ---------------------------------------------------------------------------
# Competitors and sources
# ---------------------------------------------------------------------------

# Aliases are matched case-sensitively on word boundaries. "Notion" and "Asana" are also
# ordinary English words ("the notion that...", "yoga asana"), so case matters here.
COMPETITORS: dict[str, list[str]] = {
    "Asana": ["Asana"],
    "ClickUp": ["ClickUp", "Click Up"],
    "Notion": ["Notion", "Notion Labs", "Notion AI", "Notion Mail", "Notion Calendar"],
}

# Bing News returns ~14 items per query, so we vary the query to cover more of the news.
# bing.com/robots.txt does not disallow /news/search; format=rss is an official output format.
BING_QUERY_SUFFIXES = [
    "", "pricing", "launch OR launches OR announces", "funding OR acquisition OR acquires", "AI",
    "integration OR partnership", "CEO OR executive", "customers", "outage", "earnings OR revenue",
]

# General feeds: items only count if they mention a tracked competitor.
GENERAL_FEEDS: list[tuple[str, str]] = [
    ("press_release", "https://www.prnewswire.com/rss/computer-electronics-latest-news/computer-electronics-latest-news-list.rss"),
    ("press_release", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("news", "https://techcrunch.com/feed/"),
    ("news", "https://feeds.feedburner.com/venturebeat/SZYF"),
    ("news", "https://www.theverge.com/rss/index.xml"),
    ("news", "https://www.saastr.com/feed/"),
]

# Official competitor feeds (blogs / changelogs). Only Notion publishes one we could find;
# Asana and ClickUp have no discoverable RSS for their blog or release notes.
COMPETITOR_FEEDS: list[tuple[str, str, str]] = [
    ("changelog", "Notion", "https://www.notion.com/releases/rss.xml"),
]

SourceType = Literal["news", "press_release", "changelog", "hn"]

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    """One raw data point (an article, a press release, a changelog entry, an HN post)."""

    source_type: SourceType
    source: str
    title: str
    url: str
    published_at: str  # ISO-8601 UTC
    text: str
    matched_competitors: list[str] = field(default_factory=list)
    status: str = "new"  # new -> filtered_out | duplicate | enriched | not_relevant | enrichment_failed
    enrichment: dict | None = None
    llm_usage: dict | None = None


TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ocid$)")


def canonical_url(url: str) -> str:
    """Strip tracking params, fragments and trailing slashes so syndicated links compare equal."""
    p = urlparse(url.strip())
    query = [(k, v) for k, vs in parse_qs(p.query).items() for v in vs if not TRACKING_PARAMS.match(k)]
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", urlencode(query), ""))


def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def to_iso(dt: datetime | None) -> str:
    return (dt or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def entry_time(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            return datetime(*entry[key][:6], tzinfo=UTC)
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                return parsedate_to_datetime(entry[key])
            except (TypeError, ValueError):
                pass
    return None


# ---------------------------------------------------------------------------
# Fetching (polite, sequential, one client)
# ---------------------------------------------------------------------------


class Fetcher:
    """Sequential HTTP fetcher with a minimum delay per host. Good enough for a prototype."""

    def __init__(self) -> None:
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
        self.last_hit: dict[str, float] = {}
        self.min_interval = {"api.gdeltproject.org": 6.0}
        self.errors: list[str] = []

    def get(self, url: str) -> httpx.Response | None:
        host = urlparse(url).netloc
        wait = self.min_interval.get(host, 1.0) - (time.monotonic() - self.last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.monotonic()
        try:
            resp = self.client.get(url)
        except httpx.HTTPError as exc:
            self.errors.append(f"{url}: {exc!r}")
            return None
        if resp.status_code != 200:
            self.errors.append(f"{url}: HTTP {resp.status_code}")
            return None
        return resp


def fetch_bing_news(f: Fetcher, competitor: str) -> list[Signal]:
    out = []
    for suffix in BING_QUERY_SUFFIXES:
        q = f'"{competitor}" {suffix}'.strip()
        resp = f.get("https://www.bing.com/news/search?" + urlencode({"q": q, "format": "rss", "count": 100}))
        if not resp:
            continue
        for e in feedparser.parse(resp.content).entries:
            # Bing wraps the publisher link in a click-tracking URL; the real one is the `url` param.
            real = parse_qs(urlparse(e.get("link", "")).query).get("url", [e.get("link", "")])[0]
            out.append(Signal("news", f"bing:{e.get('news_source', 'unknown')}", strip_html(e.get("title", "")),
                              real, to_iso(entry_time(e)), strip_html(e.get("summary", ""))))
    return out


def fetch_hn(f: Fetcher, competitor: str) -> list[Signal]:
    since = int((datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).timestamp())
    params = {"query": competitor, "tags": "story", "hitsPerPage": 100, "numericFilters": f"created_at_i>{since}"}
    resp = f.get("https://hn.algolia.com/api/v1/search_by_date?" + urlencode(params))
    if not resp:
        return []
    out = []
    for h in resp.json().get("hits", []):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        text = strip_html(h.get("story_text") or "") or f"Hacker News story with {h.get('points', 0)} points and {h.get('num_comments', 0)} comments."
        out.append(Signal("hn", "hn.algolia.com", h.get("title") or "", url, h["created_at"].replace("Z", "+00:00"), text))
    return out


def fetch_gdelt(f: Fetcher, competitor: str) -> list[Signal]:
    params = {"query": f'"{competitor}" sourcelang:english', "mode": "artlist", "maxrecords": 75, "format": "json", "timespan": f"{LOOKBACK_DAYS}d"}
    resp = f.get("https://api.gdeltproject.org/api/v2/doc/doc?" + urlencode(params))
    if not resp:
        return []
    try:
        articles = resp.json().get("articles", [])
    except json.JSONDecodeError:  # GDELT answers rate limits with a plain-text body
        f.errors.append(f"gdelt {competitor}: non-JSON response: {resp.text[:80]!r}")
        return []
    out = []
    for a in articles:
        seen = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        # GDELT only gives a title; the snippet is the title too, so these are thin signals.
        out.append(Signal("news", f"gdelt:{a.get('domain', '')}", a.get("title", ""), a["url"], to_iso(seen), a.get("title", "")))
    return out


def fetch_feed(f: Fetcher, source_type: SourceType, url: str, competitor: str | None = None) -> list[Signal]:
    resp = f.get(url)
    if not resp:
        return []
    out = []
    for e in feedparser.parse(resp.content).entries:
        s = Signal(source_type, urlparse(url).netloc, strip_html(e.get("title", "")), e.get("link", ""),
                   to_iso(entry_time(e)), strip_html(e.get("summary", "") or e.get("description", "")))
        if competitor:
            s.matched_competitors = [competitor]  # an official feed is about its own company by definition
        out.append(s)
    return out


def fetch_all() -> tuple[list[Signal], list[str]]:
    f = Fetcher()
    signals: list[Signal] = []
    for comp in COMPETITORS:
        for fn in (fetch_bing_news, fetch_hn, fetch_gdelt):
            got = fn(f, comp)
            print(f"  {fn.__name__:<16} {comp:<8} {len(got):>4}", file=sys.stderr)
            signals += got
    for source_type, url in GENERAL_FEEDS:
        got = fetch_feed(f, source_type, url)
        print(f"  feed {urlparse(url).netloc:<28} {len(got):>4}", file=sys.stderr)
        signals += got
    for source_type, comp, url in COMPETITOR_FEEDS:
        got = fetch_feed(f, source_type, url, comp)
        print(f"  feed {urlparse(url).netloc:<28} {len(got):>4}", file=sys.stderr)
        signals += got
    return signals, f.errors


# ---------------------------------------------------------------------------
# Filtering before we spend tokens
# ---------------------------------------------------------------------------

ALIAS_PATTERNS = {
    comp: re.compile(r"\b(" + "|".join(re.escape(a) for a in aliases) + r")\b") for comp, aliases in COMPETITORS.items()
}


def match_competitors(text: str) -> list[str]:
    return [comp for comp, pat in ALIAS_PATTERNS.items() if pat.search(text)]


def title_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def filter_signals(signals: list[Signal]) -> dict[str, int]:
    """Mark old, irrelevant and duplicate signals in place; return funnel counts."""
    cutoff = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)
    funnel = {"fetched": len(signals), "too_old": 0, "no_competitor": 0, "dup_url": 0, "near_dup_title": 0}
    seen_urls: set[str] = set()
    kept_titles: list[set[str]] = []
    # Prefer official / press-release copies when the same story shows up from several sources.
    priority = {"changelog": 0, "press_release": 1, "news": 2, "hn": 3}
    for s in sorted(signals, key=lambda s: priority[s.source_type]):
        if datetime.fromisoformat(s.published_at) < cutoff:
            s.status, funnel["too_old"] = "filtered_out", funnel["too_old"] + 1
            continue
        if not s.matched_competitors:
            s.matched_competitors = match_competitors(f"{s.title} {s.text}")
        if not s.matched_competitors:
            s.status, funnel["no_competitor"] = "filtered_out", funnel["no_competitor"] + 1
            continue
        cu = canonical_url(s.url)
        if cu in seen_urls:
            s.status, funnel["dup_url"] = "duplicate", funnel["dup_url"] + 1
            continue
        seen_urls.add(cu)
        toks = title_tokens(s.title)
        if any(jaccard(toks, t) >= 0.8 for t in kept_titles):
            s.status, funnel["near_dup_title"] = "duplicate", funnel["near_dup_title"] + 1
            continue
        kept_titles.append(toks)
    funnel["to_enrich"] = sum(1 for s in signals if s.status == "new")
    return funnel


# ---------------------------------------------------------------------------
# Enrichment with Claude
# ---------------------------------------------------------------------------

MODEL = os.environ.get("SCOUT_MODEL", "claude-haiku-5-5")
PROMPT_VERSION = "proto-v1"

# USD per million tokens. Source: platform.claude.com pricing, checked 2026-10-28.
# Haiku 5.5 prompts <=100K tokens: input $0.10, output $0.50; cache write (5 min) 1.25x, cache read 0.1x.
PRICING = {"claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01}}

CATEGORIES = [
    "pricing_change", "product_launch", "feature_update", "funding", "acquisition", "partnership",
    "leadership_change", "hiring_signal", "customer_win", "outage_or_incident", "legal_regulatory", "other",
]

# The system prompt is long and identical on every call, so it is cached (the minimum cacheable
# prefix on Haiku 5.5 is 512 tokens; tools + this prompt are well above that).
# The few-shot examples use a made-up company ("Acme Boards") so we never put invented facts
# about a real competitor into the prompt.
SYSTEM_PROMPT = f"""You are a competitive-intelligence analyst for a B2B SaaS company that sells project-management software.
Your readers are account executives and product marketers. They have 30 seconds per item. They want to know what a
competitor did, and how it changes a sales conversation.

The tracked competitors are: {", ".join(COMPETITORS)}.

For every signal you receive (a news article snippet, press release, changelog entry, or Hacker News post), call the
`record_enrichment` tool exactly once.

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
    - other: anything else (opinion pieces, generic coverage, tutorials).
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

Signal: [hn] "Show HN: I replaced Acme Boards with a text file"
-> category=other, importance=2, sentiment=-0.2,
   summary="A Hacker News post describing a developer replacing Acme Boards with a plain text workflow.",
   why_it_matters="Anecdotal developer sentiment; mostly useful as color on complexity complaints."

Signal: [press_release] "Acme Boards names former Example Corp COO as new CEO"
-> category=leadership_change, importance=4, sentiment=0.1,
   summary="Acme Boards appointed a former Example Corp COO as its CEO.",
   why_it_matters="New CEOs often reset pricing and roadmap, so watch for changes in Acme's enterprise motion."
"""

ENRICH_TOOL = {
    "name": "record_enrichment",
    "description": "Record the structured analysis of one competitive signal.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "is_about_tracked_competitor": {"type": "boolean"},
            "competitors": {"type": "array", "items": {"type": "string", "enum": list(COMPETITORS)}},
            "category": {"type": "string", "enum": CATEGORIES},
            "summary": {"type": "string"},
            "why_it_matters": {"type": "string"},
            "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number"},
            "confidence": {"type": "number"},
        },
        "required": ["is_about_tracked_competitor", "competitors", "category", "summary",
                     "why_it_matters", "importance", "sentiment", "confidence"],
        "additionalProperties": False,
    },
}


class Enrichment(BaseModel):
    """Validated Claude output. Strict tool schemas guarantee shape; Pydantic enforces ranges."""

    is_about_tracked_competitor: bool
    competitors: list[str]
    category: Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
    summary: str = Field(min_length=10, max_length=600)
    why_it_matters: str = Field(min_length=10, max_length=400)
    importance: int = Field(ge=1, le=5)
    sentiment: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)

    @field_validator("competitors")
    @classmethod
    def only_tracked(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(COMPETITORS)
        if unknown:
            raise ValueError(f"untracked competitors: {unknown}")
        return sorted(set(v))


def cost_usd(usage: dict) -> float:
    p = PRICING[MODEL]
    return (usage["input_tokens"] * p["input"] + usage["output_tokens"] * p["output"]
            + usage["cache_creation_input_tokens"] * p["cache_write"]
            + usage["cache_read_input_tokens"] * p["cache_read"]) / 1_000_000


def signal_prompt(s: Signal) -> str:
    text = s.text[:6000]  # ~1,500 tokens; snippets are far shorter than this anyway
    return (f"Source type: {s.source_type}\nSource: {s.source}\nPublished: {s.published_at}\n"
            f"Pre-matched competitors: {', '.join(s.matched_competitors)}\n"
            f"Title: {s.title}\nURL: {s.url}\n\nText:\n{text}")


class Enricher:
    """Calls Claude with bounded concurrency and stops when the prototype budget is used up."""

    def __init__(self, max_cost: float, concurrency: int = 4) -> None:
        self.client = anthropic.AsyncAnthropic(max_retries=4)  # SDK retries 429/5xx honoring retry-after
        self.sem = asyncio.Semaphore(concurrency)
        self.max_cost = max_cost
        self.spent = 0.0
        self.calls = 0
        self.totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self.latencies: list[float] = []

    async def _call(self, s: Signal) -> Enrichment:
        start = time.perf_counter()
        resp = await self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[ENRICH_TOOL],
            # Haiku 5.5 accepts a forced tool call; it also skips thinking, which keeps this cheap.
            tool_choice={"type": "tool", "name": "record_enrichment"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": signal_prompt(s)}],
        )
        self.latencies.append(time.perf_counter() - start)
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
                 "cache_read_input_tokens": u.cache_read_input_tokens or 0}
        for k, v in usage.items():
            self.totals[k] += v
        self.calls += 1
        self.spent += cost_usd(usage)
        s.llm_usage = {**usage, "cost_usd": round(cost_usd(usage), 6), "model": MODEL, "prompt_version": PROMPT_VERSION}
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            raise ValueError(f"no tool_use block (stop_reason={resp.stop_reason})")
        return Enrichment.model_validate(block.input)

    async def enrich(self, s: Signal) -> None:
        async with self.sem:
            if self.spent >= self.max_cost:
                s.status = "deferred_budget"
                return
            for attempt in (1, 2):  # retry once on a bad response, then give up on this signal
                try:
                    e = await self._call(s)
                    break
                except (ValidationError, ValueError) as exc:
                    if attempt == 2:
                        s.status = "enrichment_failed"
                        s.enrichment = {"error": str(exc)[:300]}
                        return
            s.enrichment = e.model_dump()
            s.status = "enriched" if e.is_about_tracked_competitor and e.competitors else "not_relevant"

    async def run(self, signals: list[Signal]) -> None:
        if not signals:
            return
        # One call first so the system prompt is written to the cache before the concurrent burst;
        # otherwise the first few parallel calls would all miss and each pay the cache-write price.
        await self.enrich(signals[0])
        await asyncio.gather(*(self.enrich(s) for s in signals[1:]))


# ---------------------------------------------------------------------------
# Clustering + ranking
# ---------------------------------------------------------------------------

IMPORTANCE_WEIGHT = {1: 0.1, 2: 0.25, 3: 0.5, 4: 0.8, 5: 1.0}
SOURCE_WEIGHT = {"press_release": 1.0, "changelog": 1.0, "news": 0.9, "hn": 0.6}
HALF_LIFE_HOURS = 48
CLUSTER_WINDOW_HOURS = 72
CLUSTER_JACCARD = 0.25
STOPWORDS = {"the", "and", "for", "with", "its", "new", "now", "has", "from", "that", "this", "are", "you", "your",
             "into", "about", "will", "what", "how", "can", "more", "says", "said", "their", "they"}


@dataclass
class Insight:
    competitor: str
    category: str
    signals: list[Signal]
    tokens: set[str]
    rank_score: float = 0.0

    @property
    def lead(self) -> Signal:
        return max(self.signals, key=lambda s: (s.enrichment["importance"], SOURCE_WEIGHT[s.source_type]))


def content_tokens(s: Signal) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", f"{s.title} {s.enrichment['summary']}".lower())
            if len(w) > 2 and w not in STOPWORDS}


def cluster(signals: list[Signal]) -> list[Insight]:
    """Greedy clustering: same competitor + category, within 72h, and overlapping vocabulary."""
    insights: list[Insight] = []
    for s in sorted(signals, key=lambda s: s.published_at):
        comp, cat, toks = s.enrichment["competitors"][0], s.enrichment["category"], content_tokens(s)
        t = datetime.fromisoformat(s.published_at)
        for ins in insights:
            newest = datetime.fromisoformat(ins.signals[-1].published_at)
            if (ins.competitor == comp and ins.category == cat
                    and abs((t - newest).total_seconds()) <= CLUSTER_WINDOW_HOURS * 3600
                    and jaccard(toks, ins.tokens) >= CLUSTER_JACCARD):
                ins.signals.append(s)
                ins.tokens |= toks
                break
        else:
            insights.append(Insight(comp, cat, [s], toks))
    return insights


def rank(insights: list[Insight], now: datetime) -> None:
    """rank = importance x recency_decay x source_weight x corroboration x novelty (novelty=1 in the prototype)."""
    for ins in insights:
        importance = max(s.enrichment["importance"] for s in ins.signals)
        age_h = max(0.0, (now - max(datetime.fromisoformat(s.published_at) for s in ins.signals)).total_seconds() / 3600)
        recency = 0.5 ** (age_h / HALF_LIFE_HOURS)
        source_w = max(SOURCE_WEIGHT[s.source_type] for s in ins.signals)
        corroboration = 1 + math.log(len(ins.signals))
        ins.rank_score = IMPORTANCE_WEIGHT[importance] * recency * source_w * corroboration


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def latest_cache() -> Path | None:
    files = sorted(CACHE_DIR.glob("signals-*.json"))
    return files[-1] if files else None


def write_report(path: Path, insights: list[Insight], funnel: dict, enricher: Enricher | None, generated: datetime) -> None:
    lines = [f"# Prototype run {generated:%Y-%m-%d %H:%M} UTC", "", f"Model: `{MODEL}`, prompt `{PROMPT_VERSION}`", "",
             "## Funnel", "", "| stage | count |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in funnel.items()]
    if enricher:
        lat = sorted(enricher.latencies) or [0.0]
        lines += ["", "## Claude usage", "", f"- calls: {enricher.calls}", f"- tokens: {enricher.totals}",
                  f"- cost: ${enricher.spent:.4f}", f"- latency p50/p95: {lat[len(lat) // 2]:.2f}s / {lat[int(len(lat) * 0.95)]:.2f}s"]
    lines += ["", "## Ranked insights (top 25)", ""]
    for i, ins in enumerate(insights[:25], 1):
        e = ins.lead.enrichment
        lines += [f"### {i}. [{ins.competitor} · {ins.category} · importance {max(s.enrichment['importance'] for s in ins.signals)}] {ins.lead.title}",
                  f"score {ins.rank_score:.3f} · {len(ins.signals)} signal(s) · latest {max(s.published_at for s in ins.signals)}", "",
                  f"{e['summary']}", "", f"**Why it matters:** {e['why_it_matters']}", ""]
        lines += [f"- [{s.source_type}] [{s.title[:90]}]({s.url})" for s in ins.signals[:5]]
        lines.append("")
    path.write_text("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-llm", action="store_true", help="fetch and filter only; do not call Claude")
    ap.add_argument("--reuse", action="store_true", help="reuse the last fetched batch instead of refetching")
    ap.add_argument("--limit", type=int, default=250, help="max signals to send to Claude")
    ap.add_argument("--max-cost", type=float, default=0.50, help="stop calling Claude after this many USD")
    args = ap.parse_args()
    load_dotenv(ROOT.parent / ".env")
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    now = datetime.now(UTC)

    if args.reuse and (cached := latest_cache()):
        print(f"reusing {cached.name}", file=sys.stderr)
        signals = [Signal(**{**d, "status": "new", "enrichment": None, "llm_usage": None}) for d in json.loads(cached.read_text())]
        signals = [s if s.source_type == "changelog" else Signal(**{**asdict(s), "matched_competitors": []}) for s in signals]
    else:
        print("fetching...", file=sys.stderr)
        signals, errors = fetch_all()
        for err in errors:
            print(f"  ! {err}", file=sys.stderr)
        (CACHE_DIR / f"signals-{now:%Y%m%dT%H%M%S}.json").write_text(json.dumps([asdict(s) for s in signals], indent=1))

    funnel = filter_signals(signals)
    by_type: dict[str, int] = {}
    for s in signals:
        if s.status == "new":
            by_type[s.source_type] = by_type.get(s.source_type, 0) + 1
    print(f"funnel: {funnel}\nto enrich by source type: {by_type}", file=sys.stderr)

    enricher = None
    if not args.no_llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set (put it in .env). Use --no-llm to run without Claude.")
        todo = [s for s in signals if s.status == "new"][: args.limit]
        enricher = Enricher(max_cost=args.max_cost)
        print(f"enriching {len(todo)} signals with {MODEL}...", file=sys.stderr)
        asyncio.run(enricher.run(todo))
        for status in ("enriched", "not_relevant", "enrichment_failed", "deferred_budget"):
            funnel[f"llm_{status}"] = sum(1 for s in todo if s.status == status)

    insights = cluster([s for s in signals if s.status == "enriched"])
    rank(insights, now)
    insights.sort(key=lambda i: i.rank_score, reverse=True)
    funnel["insights"] = len(insights)

    stamp = f"{now:%Y%m%dT%H%M%S}"
    (OUTPUT_DIR / f"run-{stamp}.json").write_text(json.dumps(
        {"generated_at": to_iso(now), "model": MODEL, "prompt_version": PROMPT_VERSION, "funnel": funnel,
         "cost_usd": enricher.spent if enricher else 0.0, "signals": [asdict(s) for s in signals if s.status != "filtered_out"]},
        indent=1))
    write_report(OUTPUT_DIR / f"run-{stamp}.md", insights, funnel, enricher, now)

    print(f"\nfunnel: {json.dumps(funnel)}")
    if enricher:
        print(f"Claude: {enricher.calls} calls, ${enricher.spent:.4f}, tokens {enricher.totals}")
    for i, ins in enumerate(insights[:15], 1):
        e = ins.lead.enrichment
        print(f"\n{i:>2}. {ins.rank_score:.3f}  [{ins.competitor} | {ins.category} | imp {e['importance']} | {len(ins.signals)} src]  {ins.lead.title[:100]}")
        print(f"    {e['why_it_matters']}")
    print(f"\nreport: {OUTPUT_DIR / f'run-{stamp}.md'}")


if __name__ == "__main__":
    main()
