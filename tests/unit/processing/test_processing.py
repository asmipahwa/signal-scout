"""Relevance filter, near-duplicate detection, clustering, ranking, and page diffing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from signal_scout.ingest.matching import CompetitorMatcher
from signal_scout.processing.clustering import ClusterInput, cluster_signals
from signal_scout.processing.near_dup import NearDuplicateIndex
from signal_scout.processing.page_diff import content_hash, diff_pages
from signal_scout.processing.ranking import RankingConfig, RankInput, rank_score
from signal_scout.processing.relevance import decide

MATCHER = CompetitorMatcher(
    {
        "Asana": ["Asana"],
        "Notion": ["Notion", "Notion AI"],
        "Linear": ["Linear"],
        "Monday.com": ["Monday.com", "monday.com"],
    }
)
NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)

# --- Competitor matching ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Notion raises prices on its Plus plan", ["Notion"]),
        ("Notion AI adds agents", ["Notion"]),
        ("We reject the notion that pricing is fixed", []),  # common word, lowercase
        ("A linear process for onboarding", []),
        ("Linear ships a new triage view", ["Linear"]),
        ("monday.com reports Q3 revenue", ["Monday.com"]),
        ("See you on Monday", []),
        ("Asana's new AI teammate", ["Asana"]),
        ("Notion's outage", ["Notion"]),
        ("Asanas for flexibility", []),  # yoga, plural
        ("Asana and Notion both shipped agents", ["Asana", "Notion"]),
    ],
)
def test_competitor_matching_respects_case_and_word_boundaries(text: str, expected: list[str]) -> None:
    assert MATCHER.match(text) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Asana vs Basecamp: Features and Pricing Review",
        "Asana pricing plans explained",
        "How much does Asana cost per user",
        "How to Use Notion AI: Every Feature, Explained (2026)",
        "I replaced Notion, Obsidian, and Todoist with Anytype",
        "10 best project management tools (Asana and more)",
        "Notion Pricing 2026 : Everything You Need to Know",
    ],
)
def test_low_value_titles_are_dropped_before_the_llm(title: str) -> None:
    decision = decide(title, MATCHER.match(title))
    assert not decision.keep
    assert decision.reason.startswith("low_value:")


@pytest.mark.parametrize(
    "title",
    [
        "Asana acquired Stack AI for $75 million",
        "Notion AI Business Plan Shifts to Credit System",
        "Asana down for thousands of users",
        "Notion launches developer platform for AI agents",
    ],
)
def test_real_news_is_kept(title: str) -> None:
    assert decide(title, MATCHER.match(title)).keep


def test_official_feed_skips_matching_and_pattern_checks() -> None:
    decision = decide("How to use Plan Mode", ["Notion"], official_source=True)
    assert decision.keep and decision.competitors == ["Notion"]


def test_no_competitor_is_dropped() -> None:
    assert decide("Show HN: a new task app", MATCHER.match("Show HN: a new task app")).reason == "no_competitor"


# --- Near-duplicates ----------------------------------------------------------------------------

PRESS_RELEASE = (
    "SAN FRANCISCO, Oct. 20, 2026 -- Acme Boards today announced a $120 million Series D round led by "
    "Example Ventures. The company will use the funding to expand its AI agent platform, grow its "
    "enterprise sales team in Europe and Asia, and invest in security certifications for regulated industries."
)


def test_syndicated_copy_with_extra_boilerplate_is_a_near_duplicate() -> None:
    index = NearDuplicateIndex()
    assert index.add("original", PRESS_RELEASE) is None
    syndicated = PRESS_RELEASE + " About Example Wire: Example Wire distributes press releases."
    assert index.add("copy", syndicated) == "original"
    assert len(index) == 1


def test_different_story_is_not_a_duplicate() -> None:
    index = NearDuplicateIndex()
    index.add("a", PRESS_RELEASE)
    other = "Acme Boards suffered a six hour outage on Tuesday that affected customers in the EU and the UK region."
    assert index.add("b", other) is None
    assert len(index) == 2


def test_adding_the_same_id_twice_is_a_no_op() -> None:
    index = NearDuplicateIndex()
    index.add("a", PRESS_RELEASE)
    assert index.add("a", PRESS_RELEASE) is None
    assert len(index) == 1


# --- Clustering ---------------------------------------------------------------------------------


def ci(id_: str, competitor: str, hours_ago: float, title: str, summary: str = "") -> ClusterInput:
    return ClusterInput(id_, competitor, NOW - timedelta(hours=hours_ago), title, summary)


def test_same_story_from_different_outlets_clusters_across_categories() -> None:
    """Realistic input: titles plus LLM summaries, among unrelated signals so TF-IDF has a corpus.

    Mirrors the real ClickUp layoff coverage that clustered correctly in
    docs/results/2026-10-31-processing/relevance-and-clustering.txt.
    """
    layoffs = [
        ci(
            "1",
            "ClickUp",
            50,
            "ClickUp cuts 22% of its workforce in AI restructuring",
            "ClickUp laid off about 22% of its workforce as it shifts to an AI-first operating model.",
        ),
        ci(
            "2",
            "ClickUp",
            48,
            "ClickUp CEO cuts 22% workforce despite strong business",
            "ClickUp's CEO cut roughly 22% of the workforce while saying the business is the strongest ever.",
        ),
        ci(
            "3",
            "ClickUp",
            47,
            "ClickUp lays off staff, promises million-dollar pay bands",
            "ClickUp cut about a fifth of its workforce in an AI restructuring and promised higher pay bands.",
        ),
    ]
    background = [
        ci(
            "b1",
            "ClickUp",
            40,
            "Unfold adds ClickUp to A/NZ distribution portfolio",
            "Unfold signed a distribution deal.",
        ),
        ci("b2", "ClickUp", 30, "ClickUp Brain gains agentic capabilities", "ClickUp overhauled its Brain assistant."),
        ci("b3", "Asana", 45, "Asana acquires Stack AI", "Asana bought Stack AI for $75 million."),
        ci("b4", "Notion", 44, "Notion launches developer platform", "Notion released Workers and an agent API."),
    ]
    clusters = cluster_signals(layoffs + background)
    layoff_clusters = [c for c in clusters if {m.id for m in c.members} & {"1", "2", "3"}]
    assert len(layoff_clusters) == 1
    assert {m.id for m in layoff_clusters[0].members} == {"1", "2", "3"}


def test_different_competitors_never_cluster() -> None:
    clusters = cluster_signals([ci("1", "Asana", 2, "Major outage today"), ci("2", "Notion", 1, "Major outage today")])
    assert len(clusters) == 2


def test_same_topic_outside_the_window_starts_a_new_cluster() -> None:
    clusters = cluster_signals(
        [ci("1", "Asana", 200, "Asana outage hits users"), ci("2", "Asana", 1, "Asana outage hits users")]
    )
    assert len(clusters) == 2


def test_unrelated_stories_stay_apart() -> None:
    clusters = cluster_signals(
        [
            ci("1", "Notion", 5, "Notion acquires search startup ZeroEntropy"),
            ci("2", "Notion", 4, "Notion adds high contrast mode"),
        ]
    )
    assert len(clusters) == 2


# --- Ranking ------------------------------------------------------------------------------------


def item(
    importance: int = 3, hours_ago: float = 0, sources: frozenset[str] = frozenset({"news"}), n: int = 1
) -> RankInput:
    return RankInput(importance, NOW - timedelta(hours=hours_ago), sources, n)


def test_major_week_old_change_beats_minor_post_from_yesterday() -> None:
    """The exact failure the prototype hit with a 48 h half-life."""
    assert rank_score(item(5, hours_ago=24 * 7), NOW) > rank_score(item(2, hours_ago=24), NOW)


def test_score_halves_after_one_half_life() -> None:
    cfg = RankingConfig()
    fresh = rank_score(item(hours_ago=0), NOW, cfg)
    assert rank_score(item(hours_ago=cfg.half_life_hours), NOW, cfg) == pytest.approx(fresh / 2)


def test_corroboration_has_diminishing_returns() -> None:
    one, ten, forty = (rank_score(item(n=n), NOW) for n in (1, 10, 40))
    assert one < ten < forty
    assert forty / ten < ten / one


def test_official_sources_outrank_forum_posts_and_novelty_penalizes_repeats() -> None:
    assert rank_score(item(sources=frozenset({"changelog"})), NOW) > rank_score(item(sources=frozenset({"hn"})), NOW)
    repeat = RankInput(3, NOW, frozenset({"news"}), 1, repeats_recent_theme=True)
    assert rank_score(repeat, NOW) < rank_score(item(), NOW)


@given(st.integers(1, 5), st.floats(0, 24 * 365), st.integers(1, 500))
def test_score_is_positive_and_bounded(importance: int, hours_ago: float, n: int) -> None:
    score = rank_score(item(importance, hours_ago, n=n), NOW)
    assert 0 < score <= 1 * 1 * 1 * (1 + 7)  # all factors <= 1 except corroboration (ln 500 < 7)


# --- Page diffs ---------------------------------------------------------------------------------

PRICING_V1 = """Accept all cookies
Pricing
Plus: $10 per seat / month
Business: $20 per seat / month
Updated 3 days ago
© 2026 Acme Boards"""


def test_cosmetic_changes_are_ignored() -> None:
    v2 = (
        PRICING_V1.replace("3 days ago", "5 days ago")
        .replace("© 2026", "© 2027")
        .replace("Accept all cookies", "Accept all")
    )
    assert diff_pages(PRICING_V1, v2) is None
    assert content_hash(PRICING_V1) == content_hash(v2)


def test_price_change_is_reported_as_removed_and_added_lines() -> None:
    v2 = PRICING_V1.replace("Plus: $10 per seat", "Plus: $12 per seat")
    change = diff_pages(PRICING_V1, v2)
    assert change is not None
    assert change.removed == ["Plus: $10 per seat / month"]
    assert change.added == ["Plus: $12 per seat / month"]
    assert change.as_signal_text() == "- Plus: $10 per seat / month\n+ Plus: $12 per seat / month"


def test_new_plan_added_is_reported() -> None:
    v2 = PRICING_V1.replace(
        "Business: $20", "Business: $20 per seat / month\nEnterprise: contact sales for custom pricing\nBusiness: $20"
    )
    change = diff_pages(PRICING_V1, v2)
    assert change is not None and "Enterprise: contact sales for custom pricing" in change.added


def test_punctuation_case_and_spacing_edits_are_ignored() -> None:
    assert diff_pages("Plans for every team.", "Plans  for every Team!") is None


def test_one_digit_price_change_is_not_ignored() -> None:
    change = diff_pages("Pro: $10 per user", "Pro: $12 per user")
    assert change is not None and change.added == ["Pro: $12 per user"]


def test_dates_inside_lines_are_normalized() -> None:
    assert diff_pages("Prices as of 2026-10-01 apply", "Prices as of 2026-10-30 apply") is None
    assert diff_pages("Effective October 1, 2026 for all plans", "Effective November 3, 2026 for all plans") is None
