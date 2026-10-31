"""Small helpers shared by the pipeline jobs (enrich, build insights, index, alert)."""

from __future__ import annotations

import re

from elasticsearch import AsyncElasticsearch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Competitor, SourceType
from ..enrichment import BaseLLMClient, llm_client_from_env
from ..enrichment.models import SourceType as SignalSourceType

# Ingestion's source types (how we fetch) mapped to the enrichment/ranking vocabulary (what it is).
SIGNAL_SOURCE_TYPE: dict[SourceType, SignalSourceType] = {
    SourceType.news_rss: "news",
    SourceType.press_release_rss: "press_release",
    SourceType.blog_rss: "blog",
    SourceType.changelog_rss: "changelog",
    SourceType.webpage_watch: "webpage_change",
    SourceType.hn_search: "hn",
    SourceType.inbound_webhook: "inbound_webhook",
}
OFFICIAL_SOURCE_TYPES = frozenset(
    {SourceType.blog_rss, SourceType.changelog_rss, SourceType.press_release_rss, SourceType.webpage_watch}
)

_ENGLISH_STOPWORDS = frozenset(
    "the and of to in is for on with that this are from by as at be has it an or was".split()  # noqa: SIM905
)


def guess_language(text: str) -> str:
    """'en' when the text reads like English, else 'und' (undetermined).

    Deliberately crude: every enabled source is English-language, so this only flags oddities
    (non-English Bing results, garbled extraction) for review rather than routing anything.
    """
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) < 8:
        return "und"
    hits = sum(1 for w in words if w in _ENGLISH_STOPWORDS)
    return "en" if hits / len(words) >= 0.05 else "und"


async def competitor_names(session: AsyncSession) -> list[str]:
    return list((await session.execute(select(Competitor.name).order_by(Competitor.name))).scalars())


def llm_client_for(settings: Settings, competitors: list[str]) -> BaseLLMClient:
    """The configured LLM client, built from the process settings (which read .env)."""
    env = {
        "LLM_PROVIDER": settings.llm_provider,
        "LLM_MODEL": settings.llm_model,
        "LLM_RPM": str(settings.llm_rpm or ""),
        "LLM_RPD": str(settings.llm_rpd or ""),
        "LLM_STATE_DIR": settings.llm_state_dir,
        "LLM_PROMPT_VERSION": settings.llm_prompt_version,
        "GEMINI_API_KEY": settings.gemini_api_key.get_secret_value(),
        "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
    }
    return llm_client_from_env(competitors, env)


def elasticsearch_client(settings: Settings) -> AsyncElasticsearch:
    password = settings.elastic_password.get_secret_value()
    return AsyncElasticsearch(
        settings.elasticsearch_url,
        basic_auth=("elastic", password) if password else None,
        request_timeout=30,
    )
