"""Elasticsearch index definition for insights.

Explicit mappings instead of dynamic ones, because search quality and speed depend on them:
- Free-text fields use the `english` analyzer (stemming: "launches" matches "launch").
- Fields we filter on are `keyword`, so filters are exact and cacheable.
- `competitor_suggest` is a `completion` field for fast competitor autocomplete.
- One shard: at tens of thousands of documents, more shards only add coordination overhead.
- `_source` stays enabled (we return it), but the large `signals_text` field is excluded from it.

Postgres is the source of truth; this index can be deleted and rebuilt from it at any time.
"""

from __future__ import annotations

from typing import Any

INDEX_NAME = "insights"
INDEX_VERSION = 1  # bump when the mapping changes; reindex into insights_v<N> and swap the alias

SETTINGS: dict[str, Any] = {
    "number_of_shards": 1,
    "number_of_replicas": 0,  # single-node dev/demo cluster
    "refresh_interval": "5s",  # search doesn't need sub-second freshness; saves indexing work
}

MAPPINGS: dict[str, Any] = {
    "dynamic": "strict",
    "_source": {"excludes": ["signals_text"]},
    "properties": {
        "insight_id": {"type": "keyword"},
        "headline": {
            "type": "text",
            "analyzer": "english",
            "fields": {"raw": {"type": "keyword", "ignore_above": 512}},
        },
        "summary": {"type": "text", "analyzer": "english"},
        "why_it_matters": {"type": "text", "analyzer": "english"},
        "signals_text": {"type": "text", "analyzer": "english"},  # member titles + excerpts, for recall
        "competitor": {"type": "keyword"},
        "competitor_suggest": {"type": "completion"},
        "category": {"type": "keyword"},
        "importance": {"type": "integer"},
        "sentiment": {"type": "float"},
        "rank_score": {"type": "float"},
        "signal_count": {"type": "integer"},
        "source_types": {"type": "keyword"},
        "source_urls": {"type": "keyword", "index": False},
        "first_seen": {"type": "date"},
        "last_seen": {"type": "date"},
    },
}


def index_body() -> dict[str, Any]:
    return {"settings": SETTINGS, "mappings": MAPPINGS}


def versioned_index_name(version: int = INDEX_VERSION) -> str:
    return f"{INDEX_NAME}_v{version}"
