"""Settings, log redaction, competitor matching, and the seed file's invariants."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import structlog
from pydantic import SecretStr

from signal_scout.config import Settings
from signal_scout.db.models import SourceType
from signal_scout.ingest.matching import CompetitorMatcher
from signal_scout.log import REDACTED, configure_logging, redact_secrets, redact_text
from signal_scout.seed import load_seed

ROOT = Path(__file__).resolve().parents[2]

# --- settings ----------------------------------------------------------------------------------


def test_database_url_is_built_from_parts_and_escapes_the_password() -> None:
    s = Settings(_env_file=None, postgres_password=SecretStr("p@ss/word"), postgres_port=5433)  # type: ignore[call-arg]
    assert s.sqlalchemy_url == "postgresql+asyncpg://signal_scout:p%40ss%2Fword@localhost:5433/signal_scout"


def test_database_url_override_wins() -> None:
    s = Settings(_env_file=None, database_url=SecretStr("postgresql+asyncpg://u:p@db:5432/x"))  # type: ignore[call-arg]
    assert s.sqlalchemy_url == "postgresql+asyncpg://u:p@db:5432/x"


def test_secrets_never_appear_in_repr() -> None:
    s = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-test-value"), postgres_password=SecretStr("hunter2"))  # type: ignore[call-arg]
    assert "sk-test-value" not in repr(s) and "hunter2" not in repr(s)


def test_cost_caps_default_to_the_budget() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.cost_daily_cap_usd, s.cost_total_cap_usd) == (0.50, 4.50)


# --- logging -----------------------------------------------------------------------------------


def test_redaction_masks_secret_keys_and_url_credentials() -> None:
    event = redact_secrets(
        None, "info", {"event": "x", "api_key": "abc", "dsn": "postgresql://u:pw@host/db", "tokens": 12}
    )
    assert event["api_key"] == REDACTED
    assert event["dsn"] == f"postgresql://{REDACTED}@host/db"
    assert event["tokens"] == 12  # token *counts* are not secrets
    assert redact_text("no creds https://example.com/a") == "no creds https://example.com/a"


def test_logs_are_json_with_bound_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    with structlog.contextvars.bound_contextvars(run_id="r-1"):
        structlog.get_logger("t").info("hello", password="should-not-appear")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "hello" and line["run_id"] == "r-1" and line["password"] == REDACTED


# --- matching ----------------------------------------------------------------------------------


@pytest.fixture
def matcher() -> CompetitorMatcher:
    seed = load_seed(ROOT / "seeds" / "competitors.yaml")
    return CompetitorMatcher({c.name: c.aliases for c in seed.competitors})


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Notion launches a standalone calendar app", ["Notion"]),
        ("the notion that pricing matters", []),
        ("Linear regression is linear algebra", []),
        ("Linear app adds stacked pull requests", ["Linear"]),
        ("See you on Monday at the standup", []),
        ("monday.com reports Q3 revenue", ["Monday.com"]),
        ("Atlassian moves Jira and Confluence to the cloud", ["Atlassian"]),
        ("Asana and ClickUp compete with Smartsheet", ["Asana", "ClickUp", "Smartsheet"]),
        ("a yoga asana for focus", []),
        ("Airtablesque is not a word", []),
    ],
)
def test_case_sensitive_alias_matching(matcher: CompetitorMatcher, text: str, expected: list[str]) -> None:
    assert matcher.match(text) == expected


# --- seed file ---------------------------------------------------------------------------------


def test_seed_file_is_valid_and_covers_all_eight_competitors() -> None:
    seed = load_seed(ROOT / "seeds" / "competitors.yaml")
    names = [c.name for c in seed.competitors]
    assert names == ["Asana", "Monday.com", "ClickUp", "Notion", "Smartsheet", "Airtable", "Linear", "Atlassian"]
    assert all(c.aliases and c.domains and c.sources for c in seed.competitors)


def test_seed_sources_are_unique_and_never_use_google_news() -> None:
    seed = load_seed(ROOT / "seeds" / "competitors.yaml")
    sources = [s for c in seed.competitors for s in c.sources] + seed.general_sources
    assert len({(s.type, s.url) for s in sources}) == len(sources)
    assert not any(urlsplit(s.url).hostname == "news.google.com" for s in sources)
    assert {s.type for s in sources} <= {
        SourceType.news_rss,
        SourceType.press_release_rss,
        SourceType.blog_rss,
        SourceType.changelog_rss,
        SourceType.hn_search,
    }
