# Manual smoke check: index the prototype run's real insights into a throwaway ES and run sample searches.
# Start ES first:  docker run -d --name ss-es-check -p 127.0.0.1:19200:9200 -e discovery.type=single-node \
#   -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" docker.elastic.co/elasticsearch/elasticsearch:8.19.5
# Output: docs/results/2026-10-31-search-smoke/es-8.19.5-smoke.txt
"""Index the prototype's real insights into a real ES 8.19.5 and run searches (manual verification)."""

import json
from datetime import datetime

from elasticsearch import Elasticsearch, helpers

from signal_scout.processing.clustering import ClusterInput, cluster_signals
from signal_scout.processing.ranking import RankInput, rank_score
from signal_scout.search.mapping import index_body, versioned_index_name
from signal_scout.search.query import SearchParams, build_competitor_suggest, build_search

run = json.load(open("docs/results/2026-10-31-prototype/run.json"))
now = datetime.fromisoformat(run["generated_at"])
sig = {s["url"]: s for s in run["signals"] if s["status"] == "enriched" and s["enrichment"]["importance"] >= 2}
clusters = cluster_signals(
    [
        ClusterInput(
            u,
            s["enrichment"]["competitors"][0],
            datetime.fromisoformat(s["published_at"]),
            s["title"],
            s["enrichment"]["summary"],
        )
        for u, s in sig.items()
    ]
)
docs = []
for i, c in enumerate(clusters):
    members = [sig[m.id] for m in c.members]
    lead = max(members, key=lambda s: s["enrichment"]["importance"])
    e = lead["enrichment"]
    imp = max(s["enrichment"]["importance"] for s in members)
    docs.append(
        {
            "_index": versioned_index_name(),
            "_id": f"ins_{i}",
            "_source": {
                "insight_id": f"ins_{i}",
                "headline": lead["title"],
                "summary": e["summary"],
                "why_it_matters": e["why_it_matters"],
                "signals_text": " ".join(s["title"] + " " + s["text"][:500] for s in members),
                "competitor": c.competitor,
                "competitor_suggest": {"input": [c.competitor]},
                "category": e["category"],
                "importance": imp,
                "sentiment": e["sentiment"],
                "rank_score": rank_score(
                    RankInput(imp, c.last_seen, frozenset(s["source_type"] for s in members), len(members)), now
                ),
                "signal_count": len(members),
                "source_types": sorted({s["source_type"] for s in members}),
                "source_urls": [s["url"] for s in members],
                "first_seen": c.first_seen.isoformat(),
                "last_seen": c.last_seen.isoformat(),
            },
        }
    )
es = Elasticsearch("http://localhost:19200")
print("server version:", es.info()["version"]["number"])
idx = versioned_index_name()
es.indices.delete(index=idx, ignore_unavailable=True)
es.indices.create(index=idx, **index_body())
ok, errors = helpers.bulk(es, docs, refresh="wait_for")
print(f"indexed {ok} insight documents from {len(sig)} real enriched signals; errors: {errors}")
print("count:", es.count(index=idx)["count"])
for p in [
    SearchParams(q="pricing credits"),
    SearchParams(q="outage", competitor="Asana"),
    SearchParams(q="layoffs workforce"),
    SearchParams(category="acquisition"),
    SearchParams(min_importance=4, size=5),
    SearchParams(q="AI agents", competitor="Notion", size=3),
]:
    r = es.search(index=idx, **build_search(p))
    label = {k: v for k, v in vars(p).items() if v not in (None, "", 1, 20)}
    print(f"\nquery {label}: {r['hits']['total']['value']} hits, took {r['took']} ms")
    for h in r["hits"]["hits"][:3]:
        hl = h.get("highlight", {}).get("headline", [""])[0]
        print(
            f"   {h['_source']['competitor']:<8} {h['_source']['category']:<20} imp {h['_source']['importance']}  {h['_source']['headline'][:70]}"
            + (f"   [hl: {hl[:60]}]" if hl else "")
        )
s = es.search(index=idx, **build_competitor_suggest("No"))
print("\nsuggest 'No':", [o["text"] for o in s["suggest"]["competitors"][0]["options"]])
