"""McpServerIT: the MCP server in-process, driven by the SDK's own client, against real containers.

`mcp.Client(server)` connects to an `MCPServer` in memory (no subprocess, no network), as the SDK docs
recommend for tests. Every tool, both resources and the prompt are exercised, and every tool call
must leave a `job_runs` row. Seed data is invented fixture data (see interfaces_data.py).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from elasticsearch import AsyncElasticsearch
from mcp import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from signal_scout.config import Settings
from signal_scout.db.models import JobRun, JobRunStatus
from signal_scout.db.session import SessionFactory
from signal_scout.mcp_server import create_mcp_server
from signal_scout.services.context import ServiceContext
from signal_scout.storage import S3BlobStore

from .interfaces_data import PAGE_DIFF, Seeded, index_insights, seed


@pytest.fixture
async def mcp_env(
    settings: Settings, sessions: SessionFactory, blobs: S3BlobStore, es_url: str
) -> AsyncIterator[tuple[MCPServer[Any], Seeded, SessionFactory]]:
    """A seeded server. Tests open the Client themselves: its task group must start and end in one task."""
    es = AsyncElasticsearch(es_url)
    data = await seed(sessions, blobs)
    await index_insights(es, sessions)
    ctx = ServiceContext(settings=settings, sessions=sessions, es=es, blobs=blobs)
    yield create_mcp_server(context=ctx), data, sessions
    await es.close()


async def _call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return dict(result.structured_content)


async def test_every_tool_resource_and_prompt(mcp_env: tuple[MCPServer[Any], Seeded, SessionFactory]) -> None:
    server, seeded, sessions = mcp_env
    async with Client(server) as client:
        assert {t.name for t in (await client.list_tools()).tools} == {
            "search_insights",
            "get_competitor_brief",
            "list_recent_changes",
            "compare_competitors",
            "get_signal_sources",
        }

        search = await _call(client, "search_insights", {"query": "pricing", "competitor": "Notion", "days": 30})
        assert search["hits"][0]["insight_id"] == str(seeded.pricing_insight)

        brief = await _call(client, "get_competitor_brief", {"competitor": "notion ai", "days": 30})  # alias works
        assert brief["competitor"]["name"] == "Notion" and brief["insight_count"] == 2

        changes = await _call(client, "list_recent_changes", {"competitor": "Notion", "change_type": "webpage_change"})
        assert [c["diff"] for c in changes["items"]] == [PAGE_DIFF]

        compare = await _call(client, "compare_competitors", {"competitors": ["Notion", "Asana"]})
        assert [c["competitor"] for c in compare["competitors"]] == ["Notion", "Asana"]

        sources = await _call(client, "get_signal_sources", {"insight_id": seeded.pricing_insight})
        assert len(sources["sources"]) == 2 and all(s["url"].startswith("https://") for s in sources["sources"])

        competitors = json.loads((await client.read_resource("competitors://list")).contents[0].text)  # type: ignore[union-attr]
        assert [c["name"] for c in competitors] == ["Asana", "ClickUp", "Notion"]
        top = json.loads((await client.read_resource("insights://top")).contents[0].text)  # type: ignore[union-attr]
        assert top["items"][0]["id"] == seeded.pricing_insight

        prompt = await client.get_prompt("weekly_competitive_digest", {"competitors": "Notion"})
        assert "list_recent_changes" in str(prompt.messages[0].content)

        async with sessions() as s:
            runs = (await s.execute(select(JobRun).where(JobRun.job_type.like("mcp_tool:%")))).scalars().all()
        assert sorted(r.job_type for r in runs) == sorted(
            f"mcp_tool:{n}"
            for n in (
                "search_insights",
                "get_competitor_brief",
                "list_recent_changes",
                "compare_competitors",
                "get_signal_sources",
            )
        )
        assert all(r.status is JobRunStatus.success and r.details["args"] for r in runs)


async def test_tool_errors_are_reported_and_recorded(mcp_env: tuple[MCPServer[Any], Seeded, SessionFactory]) -> None:
    server, _, sessions = mcp_env
    async with Client(server) as client:
        missing = await client.call_tool("get_signal_sources", {"insight_id": 999_999})
        assert missing.is_error and "not found" in str(missing.content)
        unknown = await client.call_tool("get_competitor_brief", {"competitor": "Globex"})
        assert unknown.is_error
        invalid = await client.call_tool("get_competitor_brief", {"competitor": "Notion", "days": 0})
        assert invalid.is_error  # rejected by the input schema before the tool runs

        async with sessions() as s:
            failed = (
                (await s.execute(select(JobRun.job_type).where(JobRun.status == JobRunStatus.failed))).scalars().all()
            )
        assert sorted(failed) == ["mcp_tool:get_competitor_brief", "mcp_tool:get_signal_sources"]
