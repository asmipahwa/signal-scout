"""Search query building (structure only; the integration test runs these against real Elasticsearch)."""

from __future__ import annotations

from datetime import UTC, datetime

from signal_scout.search.mapping import MAPPINGS, index_body
from signal_scout.search.query import MAX_PAGE_SIZE, SearchParams, build_competitor_suggest, build_search


def test_text_search_blends_bm25_with_rank_score_and_keeps_filters_out_of_scoring() -> None:
    body = build_search(
        SearchParams(q="pricing change", competitor="Notion", category="pricing_change", min_importance=4)
    )
    fs = body["query"]["function_score"]
    assert fs["boost_mode"] == "multiply"
    assert fs["functions"][0]["field_value_factor"]["field"] == "rank_score"
    bool_q = fs["query"]["bool"]
    assert bool_q["must"][0]["multi_match"]["query"] == "pricing change"
    assert {"term": {"competitor": "Notion"}} in bool_q["filter"]
    assert {"term": {"category": "pricing_change"}} in bool_q["filter"]
    assert {"range": {"importance": {"gte": 4}}} in bool_q["filter"]
    assert "highlight" in body


def test_empty_query_is_a_ranked_feed() -> None:
    body = build_search(SearchParams())
    assert body["query"] == {"match_all": {}}
    assert body["sort"][0] == {"rank_score": "desc"}
    assert "highlight" not in body


def test_date_range_and_pagination() -> None:
    body = build_search(
        SearchParams(
            date_from=datetime(2026, 10, 1, tzinfo=UTC), date_to=datetime(2026, 10, 31, tzinfo=UTC), page=3, size=10
        )
    )
    (date_filter,) = body["query"]["bool"]["filter"]
    assert date_filter["range"]["last_seen"]["gte"].startswith("2026-10-01")
    assert date_filter["range"]["last_seen"]["lte"].startswith("2026-10-31")
    assert (body["from"], body["size"]) == (20, 10)


def test_page_size_is_capped_and_page_floor_is_one() -> None:
    body = build_search(SearchParams(size=10_000, page=0))
    assert body["size"] == MAX_PAGE_SIZE and body["from"] == 0


def test_only_needed_fields_are_returned() -> None:
    assert "signals_text" not in build_search(SearchParams(q="x"))["_source"]


def test_mapping_is_strict_single_shard_with_keyword_filters() -> None:
    body = index_body()
    assert body["settings"]["number_of_shards"] == 1
    assert MAPPINGS["dynamic"] == "strict"
    for field in ("competitor", "category", "source_types"):
        assert MAPPINGS["properties"][field]["type"] == "keyword"
    assert MAPPINGS["properties"]["headline"]["analyzer"] == "english"


def test_competitor_suggest() -> None:
    body = build_competitor_suggest("Cli")
    assert body["suggest"]["competitors"]["prefix"] == "Cli"
