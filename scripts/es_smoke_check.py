"""Manual smoke check: index the prototype run's real insights into a throwaway Elasticsearch and search them.

Proves the mapping and query builder work against a real server, using real data, before the full
pipeline exists. Output: docs/results/2026-10-31-search-smoke/es-8.19.5-smoke.txt

Start a throwaway ES first:
    docker run -d --name ss-es-check -p 127.0.0.1:19200:9200 -e discovery.type=single-node \\
      -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \\
      docker.elastic.co/elasticsearch/elasticsearch:8.19.5
Then: uv run python scripts/es_smoke_check.py   (and `docker rm -f ss-es-check` afterwards)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch, helpers

from signal_scout.processing.clustering import ClusterInput, cluster_signals
from signal_scout.processing.ranking import RankInput, rank_score
from signal_scout.search.mapping import index_body, versioned_index_name
from signal_scout.search.query import SearchParams, build_competitor_suggest, build_search

RUN = Path(__file__).resolve().parents[1] / "docs" / "results" / "2026-10-31-prototype" / "run.json"
QUERIES = [
    SearchParams(q="pricing credits"),
    SearchParams(q="outage", competitor="Asana"),
    SearchParams(q="layoffs workforce"),
    SearchParams(category="acquisition"),
    SearchParams(min_importance=4, size=5),
    SearchParams(q="AI agents", competitor="Notion", size=3),
]


def build_documents() -> tuple[list[dict[str, Any]], int]:
    run = json.loads(RUN.read_text())
    now = datetime.fromisoformat(run["generated_at"])
    signals = {s["url"]: s for s in run["signals"] if s["status"] == "enriched" and s["enrichment"]["importance"] >= 2}
    clusters = cluster_signals(
        [
            ClusterInput(
                url,
                s["enrichment"]["competitors"][0],
                datetime.fromisoformat(s["published_at"]),
                s["title"],
                s["enrichment"]["summary"],
            )
            for url, s in signals.items()
        ]
    )
    docs = []
    for i, cluster in enumerate(clusters):
        members = [signals[m.id] for m in cluster.members]
        lead = max(members, key=lambda s: s["enrichment"]["importance"])
        importance = max(s["enrichment"]["importance"] for s in members)
        source_types = sorted({s["source_type"] for s in members})
        score = rank_score(RankInput(importance, cluster.last_seen, frozenset(source_types), len(members)), now)
        docs.append(
            {
                "_index": versioned_index_name(),
                "_id": f"ins_{i}",
                "_source": {
                    "insight_id": f"ins_{i}",
                    "headline": lead["title"],
                    "summary": lead["enrichment"]["summary"],
                    "why_it_matters": lead["enrichment"]["why_it_matters"],
                    "signals_text": " ".join(s["title"] + " " + s["text"][:500] for s in members),
                    "competitor": cluster.competitor,
                    "competitor_suggest": {"input": [cluster.competitor]},
                    "category": lead["enrichment"]["category"],
                    "importance": importance,
                    "sentiment": lead["enrichment"]["sentiment"],
                    "rank_score": score,
                    "signal_count": len(members),
                    "source_types": source_types,
                    "source_urls": [s["url"] for s in members],
                    "first_seen": cluster.first_seen.isoformat(),
                    "last_seen": cluster.last_seen.isoformat(),
                },
            }
        )
    return docs, len(signals)


def main() -> None:
    es = Elasticsearch("http://localhost:19200")
    print("server version:", es.info()["version"]["number"])
    index = versioned_index_name()
    es.indices.delete(index=index, ignore_unavailable=True)
    es.indices.create(index=index, **index_body())
    docs, n_signals = build_documents()
    ok, errors = helpers.bulk(es, docs, refresh="wait_for")
    print(f"indexed {ok} insight documents from {n_signals} real enriched signals; errors: {errors}")
    print("count:", es.count(index=index)["count"])
    for params in QUERIES:
        result = es.search(index=index, **build_search(params))
        label = {k: v for k, v in vars(params).items() if v not in (None, "", 1, 20)}
        print(f"\nquery {label}: {result['hits']['total']['value']} hits, took {result['took']} ms")
        for hit in result["hits"]["hits"][:3]:
            src = hit["_source"]
            highlight = hit.get("highlight", {}).get("headline", [""])[0]
            line = f"   {src['competitor']:<8} {src['category']:<20} imp {src['importance']}  {src['headline'][:70]}"
            print(line + (f"   [hl: {highlight[:60]}]" if highlight else ""))
    suggest = es.search(index=index, **build_competitor_suggest("No"))
    print("\nsuggest 'No':", [o["text"] for o in suggest["suggest"]["competitors"][0]["options"]])


if __name__ == "__main__":
    main()
