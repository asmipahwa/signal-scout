"""Sprint 1 prototype: does "public signals -> LLM -> ranked insights" produce anything useful?

This is deliberately throwaway code. It answers one question before we build infrastructure:
if we pull a few hundred real news / press-release / changelog / Hacker News items for three
competitors and ask an LLM to classify and summarize them, do the top-ranked results read
like something a sales rep would actually want?

Everything runs in one file, in memory, with no database. Sprint 2 rebuilds the parts that
worked as a real service (see docs/PROTOTYPE_TO_PROD.md).

Enrichment goes through `signal_scout.enrichment` (the first module promoted to production code),
so the provider is chosen with LLM_PROVIDER=gemini|claude like everywhere else.

Usage:
    uv run python prototype/scout.py --no-llm   # fetch + filter only, no LLM calls
    uv run python prototype/scout.py --reuse    # enrich the last fetched batch again (no refetch)
    uv run python prototype/scout.py            # fetch, enrich, rank, print
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import feedparser
import httpx
from dotenv import load_dotenv

from signal_scout.enrichment import (
    BaseLLMClient,
    LlmError,
    LlmQuotaExhaustedError,
    LlmTransientError,
    SignalInput,
    llm_client_from_env,
)

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
    "",
    "pricing",
    "launch OR launches OR announces",
    "funding OR acquisition OR acquires",
    "AI",
    "integration OR partnership",
    "CEO OR executive",
    "customers",
    "outage",
    "earnings OR revenue",
]

# General feeds: items only count if they mention a tracked competitor.
GENERAL_FEEDS: list[tuple[str, str]] = [
    (
        "press_release",
        "https://www.prnewswire.com/rss/computer-electronics-latest-news/computer-electronics-latest-news-list.rss",
    ),
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
    return urlunparse(
        (
            p.scheme.lower() or "https",
            p.netloc.lower().removeprefix("www."),
            p.path.rstrip("/"),
            "",
            urlencode(query),
            "",
        )
    )


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
            out.append(
                Signal(
                    "news",
                    f"bing:{e.get('news_source', 'unknown')}",
                    strip_html(e.get("title", "")),
                    real,
                    to_iso(entry_time(e)),
                    strip_html(e.get("summary", "")),
                )
            )
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
        text = (
            strip_html(h.get("story_text") or "")
            or f"Hacker News story with {h.get('points', 0)} points and {h.get('num_comments', 0)} comments."
        )
        out.append(
            Signal("hn", "hn.algolia.com", h.get("title") or "", url, h["created_at"].replace("Z", "+00:00"), text)
        )
    return out


def fetch_gdelt(f: Fetcher, competitor: str) -> list[Signal]:
    params = {
        "query": f'"{competitor}" sourcelang:english',
        "mode": "artlist",
        "maxrecords": 75,
        "format": "json",
        "timespan": f"{LOOKBACK_DAYS}d",
    }
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
        out.append(
            Signal(
                "news", f"gdelt:{a.get('domain', '')}", a.get("title", ""), a["url"], to_iso(seen), a.get("title", "")
            )
        )
    return out


def fetch_feed(f: Fetcher, source_type: SourceType, url: str, competitor: str | None = None) -> list[Signal]:
    resp = f.get(url)
    if not resp:
        return []
    out = []
    for e in feedparser.parse(resp.content).entries:
        s = Signal(
            source_type,
            urlparse(url).netloc,
            strip_html(e.get("title", "")),
            e.get("link", ""),
            to_iso(entry_time(e)),
            strip_html(e.get("summary", "") or e.get("description", "")),
        )
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
# Enrichment (provider-agnostic; see src/signal_scout/enrichment)
# ---------------------------------------------------------------------------


class Enricher:
    """Runs the configured LLMClient over many signals with bounded concurrency and a spend cap.

    Rate limits are handled inside the client. A daily-quota error stops the whole run; the
    remaining signals are marked `deferred` so a later run can pick them up.
    """

    def __init__(self, client: BaseLLMClient, max_cost: float, concurrency: int = 4) -> None:
        self.client = client
        self.sem = asyncio.Semaphore(concurrency)
        self.max_cost = max_cost
        self.spent = 0.0
        self.calls = 0
        self.totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "thinking_tokens": 0,
        }
        self.latencies: list[float] = []
        self.stopped: str | None = None

    async def enrich(self, s: Signal) -> None:
        async with self.sem:
            if self.stopped or self.spent >= self.max_cost:
                s.status = "deferred"
                return
            signal_in = SignalInput(
                source_type=s.source_type,
                source=s.source,
                title=s.title,
                url=s.url,
                published_at=datetime.fromisoformat(s.published_at),
                text=s.text,
                matched_competitors=s.matched_competitors,
            )
            try:
                result = await self.client.enrich(signal_in)
            except LlmQuotaExhaustedError as exc:
                self.stopped = str(exc)
                s.status = "deferred"
                return
            except LlmTransientError as exc:  # provider outage outlasted our retries; try again next run
                s.status, s.enrichment = "deferred", {"error": str(exc)[:300]}
                return
            except LlmError as exc:
                s.status, s.enrichment = "enrichment_failed", {"error": str(exc)[:300]}
                return
            u = result.usage
            self.calls += result.attempts
            self.spent += u.cost_usd
            self.latencies.append(u.latency_s)
            for k in self.totals:
                self.totals[k] += getattr(u, k)
            s.llm_usage = {**u.model_dump(), "prompt_version": result.prompt_version, "attempts": result.attempts}
            e = result.enrichment
            s.enrichment = e.model_dump()
            s.status = "enriched" if e.is_about_tracked_competitor and e.competitors else "not_relevant"

    async def run(self, signals: list[Signal]) -> None:
        if not signals:
            return
        # One call first so the stable system prompt is cached before the concurrent burst.
        await self.enrich(signals[0])
        await asyncio.gather(*(self.enrich(s) for s in signals[1:]))


# ---------------------------------------------------------------------------
# Clustering + ranking
# ---------------------------------------------------------------------------

IMPORTANCE_WEIGHT = {1: 0.1, 2: 0.25, 3: 0.5, 4: 0.8, 5: 1.0}
SOURCE_WEIGHT = {"press_release": 1.0, "changelog": 1.0, "news": 0.9, "hn": 0.6}
# 7 days, not 48 h: with a 180-day window, 48 h let a minor post from yesterday outrank a major pricing
# change from last week. Measured in docs/results/2026-10-31-prototype/ranking-variants.txt.
HALF_LIFE_HOURS = 168
MIN_INSIGHT_IMPORTANCE = 2  # importance 1 is noise by the rubric; keep it out of the feed
CLUSTER_WINDOW_HOURS = 72
CLUSTER_JACCARD = 0.25
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "its",
    "new",
    "now",
    "has",
    "from",
    "that",
    "this",
    "are",
    "you",
    "your",
    "into",
    "about",
    "will",
    "what",
    "how",
    "can",
    "more",
    "says",
    "said",
    "their",
    "they",
}


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
    return {
        w
        for w in re.findall(r"[a-z0-9]+", f"{s.title} {s.enrichment['summary']}".lower())
        if len(w) > 2 and w not in STOPWORDS
    }


def cluster(signals: list[Signal]) -> list[Insight]:
    """Greedy clustering: same competitor + category, within 72h, and overlapping vocabulary."""
    insights: list[Insight] = []
    for s in sorted(signals, key=lambda s: s.published_at):
        comp, cat, toks = s.enrichment["competitors"][0], s.enrichment["category"], content_tokens(s)
        t = datetime.fromisoformat(s.published_at)
        for ins in insights:
            newest = datetime.fromisoformat(ins.signals[-1].published_at)
            if (
                ins.competitor == comp
                and ins.category == cat
                and abs((t - newest).total_seconds()) <= CLUSTER_WINDOW_HOURS * 3600
                and jaccard(toks, ins.tokens) >= CLUSTER_JACCARD
            ):
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
        age_h = max(
            0.0, (now - max(datetime.fromisoformat(s.published_at) for s in ins.signals)).total_seconds() / 3600
        )
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


def write_report(
    path: Path, insights: list[Insight], funnel: dict, enricher: Enricher | None, generated: datetime
) -> None:
    model = f"{enricher.client.provider}/{enricher.client.model}" if enricher else "none"
    lines = [
        f"# Prototype run {generated:%Y-%m-%d %H:%M} UTC",
        "",
        f"Model: `{model}`",
        "",
        "## Funnel",
        "",
        "| stage | count |",
        "|---|---:|",
    ]
    lines += [f"| {k} | {v} |" for k, v in funnel.items()]
    if enricher:
        lat = sorted(enricher.latencies) or [0.0]
        lines += [
            "",
            "## LLM usage",
            "",
            f"- calls: {enricher.calls}",
            f"- tokens: {enricher.totals}",
            f"- cost: ${enricher.spent:.4f}",
            f"- latency p50/p95: {lat[len(lat) // 2]:.2f}s / {lat[int(len(lat) * 0.95)]:.2f}s",
        ]
    lines += ["", "## Ranked insights (top 25)", ""]
    for i, ins in enumerate(insights[:25], 1):
        e = ins.lead.enrichment
        lines += [
            f"### {i}. [{ins.competitor} · {ins.category} · importance {max(s.enrichment['importance'] for s in ins.signals)}] {ins.lead.title}",
            f"score {ins.rank_score:.3f} · {len(ins.signals)} signal(s) · latest {max(s.published_at for s in ins.signals)}",
            "",
            f"{e['summary']}",
            "",
            f"**Why it matters:** {e['why_it_matters']}",
            "",
        ]
        lines += [f"- [{s.source_type}] [{s.title[:90]}]({s.url})" for s in ins.signals[:5]]
        lines.append("")
    path.write_text("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-llm", action="store_true", help="fetch and filter only; do not call the LLM")
    ap.add_argument("--reuse", action="store_true", help="reuse the last fetched batch instead of refetching")
    ap.add_argument("--limit", type=int, default=250, help="max signals to send to the LLM")
    ap.add_argument("--max-cost", type=float, default=0.50, help="stop after this much paid spend in USD (daily cap)")
    args = ap.parse_args()
    load_dotenv(ROOT.parent / ".env")
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    now = datetime.now(UTC)

    if args.reuse and (cached := latest_cache()):
        print(f"reusing {cached.name}", file=sys.stderr)
        signals = [
            Signal(**{**d, "status": "new", "enrichment": None, "llm_usage": None})
            for d in json.loads(cached.read_text())
        ]
        signals = [
            s if s.source_type == "changelog" else Signal(**{**asdict(s), "matched_competitors": []}) for s in signals
        ]
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
        todo = [s for s in signals if s.status == "new"][: args.limit]
        enricher = Enricher(llm_client_from_env(list(COMPETITORS)), max_cost=args.max_cost)
        print(
            f"enriching {len(todo)} signals with {enricher.client.provider}/{enricher.client.model}...", file=sys.stderr
        )
        asyncio.run(enricher.run(todo))
        if enricher.stopped:
            print(f"  ! stopped early: {enricher.stopped}", file=sys.stderr)
        for status in ("enriched", "not_relevant", "enrichment_failed", "deferred"):
            funnel[f"llm_{status}"] = sum(1 for s in todo if s.status == status)

    insights = cluster(
        [s for s in signals if s.status == "enriched" and s.enrichment["importance"] >= MIN_INSIGHT_IMPORTANCE]
    )
    rank(insights, now)
    insights.sort(key=lambda i: i.rank_score, reverse=True)
    funnel["insights"] = len(insights)

    stamp = f"{now:%Y%m%dT%H%M%S}"
    (OUTPUT_DIR / f"run-{stamp}.json").write_text(
        json.dumps(
            {
                "generated_at": to_iso(now),
                "llm": f"{enricher.client.provider}/{enricher.client.model}" if enricher else None,
                "funnel": funnel,
                "cost_usd": enricher.spent if enricher else 0.0,
                "signals": [asdict(s) for s in signals if s.status != "filtered_out"],
            },
            indent=1,
        )
    )
    write_report(OUTPUT_DIR / f"run-{stamp}.md", insights, funnel, enricher, now)

    print(f"\nfunnel: {json.dumps(funnel)}")
    if enricher:
        print(f"LLM: {enricher.calls} calls, ${enricher.spent:.4f}, tokens {enricher.totals}")
    for i, ins in enumerate(insights[:15], 1):
        e = ins.lead.enrichment
        print(
            f"\n{i:>2}. {ins.rank_score:.3f}  [{ins.competitor} | {ins.category} | imp {e['importance']} | {len(ins.signals)} src]  {ins.lead.title[:100]}"
        )
        print(f"    {e['why_it_matters']}")
    print(f"\nreport: {OUTPUT_DIR / f'run-{stamp}.md'}")


if __name__ == "__main__":
    main()
