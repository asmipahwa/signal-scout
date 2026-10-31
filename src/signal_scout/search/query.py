"""Build Elasticsearch queries for insight search.

Relevance is BM25 text relevance blended with our own `rank_score` through `function_score`, so a
search for "pricing" returns pricing news that is also important and recent, not just the item that
repeats "pricing" most often. All filters go in `filter` context: they don't affect scoring and ES
caches them, which matters for the p95 latency target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_PAGE_SIZE = 50
SOURCE_FIELDS = [
    "insight_id",
    "headline",
    "summary",
    "why_it_matters",
    "competitor",
    "category",
    "importance",
    "rank_score",
    "signal_count",
    "source_types",
    "source_urls",
    "first_seen",
    "last_seen",
]


@dataclass(frozen=True)
class SearchParams:
    q: str = ""
    competitor: str | None = None
    category: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_importance: int | None = None
    page: int = 1
    size: int = 20


def build_search(params: SearchParams) -> dict[str, Any]:
    size = max(1, min(params.size, MAX_PAGE_SIZE))
    page = max(1, params.page)

    filters: list[dict[str, Any]] = []
    if params.competitor:
        filters.append({"term": {"competitor": params.competitor}})
    if params.category:
        filters.append({"term": {"category": params.category}})
    if params.min_importance:
        filters.append({"range": {"importance": {"gte": params.min_importance}}})
    if params.date_from or params.date_to:
        bounds: dict[str, str] = {}
        if params.date_from:
            bounds["gte"] = params.date_from.isoformat()
        if params.date_to:
            bounds["lte"] = params.date_to.isoformat()
        filters.append({"range": {"last_seen": bounds}})

    if params.q.strip():
        text_query: dict[str, Any] = {
            "multi_match": {
                "query": params.q,
                "fields": ["headline^3", "summary^2", "why_it_matters", "signals_text"],
                "type": "best_fields",
                # OR semantics: for a sales search, missing a result is worse than one extra result.
                # Docs that match more of the words still score higher. (Requiring all words of a
                # 2-word query returned nothing for "layoffs workforce" against articles saying "laid off".)
                "operator": "or",
            }
        }
        query: dict[str, Any] = {
            "function_score": {
                "query": {"bool": {"must": [text_query], "filter": filters}},
                # Multiply BM25 by (1 + rank_score): text relevance leads, importance/recency break ties.
                "functions": [{"field_value_factor": {"field": "rank_score", "modifier": "log1p", "missing": 0}}],
                "boost_mode": "multiply",
            }
        }
        sort: list[Any] = ["_score", {"rank_score": "desc"}]
        highlight: dict[str, Any] | None = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fields": {
                "headline": {"number_of_fragments": 0},
                "summary": {"fragment_size": 160, "number_of_fragments": 1},
            },
        }
    else:
        # No text: it's a ranked feed with filters.
        query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
        sort = [{"rank_score": "desc"}, {"last_seen": "desc"}]
        highlight = None

    body: dict[str, Any] = {
        "query": query,
        "sort": sort,
        "from": (page - 1) * size,
        "size": size,
        "_source": SOURCE_FIELDS,
        "track_total_hits": 10_000,
    }
    if highlight:
        body["highlight"] = highlight
    return body


def build_competitor_suggest(prefix: str, size: int = 8) -> dict[str, Any]:
    return {
        "_source": False,
        "suggest": {
            "competitors": {
                "prefix": prefix,
                "completion": {"field": "competitor_suggest", "size": size, "skip_duplicates": True},
            }
        },
    }
