"""MCP server: lets AI agents (Claude Desktop, Claude Code, any MCP client) query Signal Scout.

Read-only tools over the same services the REST API uses, two resources and a "weekly competitive
digest" prompt. Every tool call is logged (tool, arguments, latency) and recorded in `job_runs` as
`mcp_tool:<name>`, so agent usage shows up next to the pipeline's own jobs.

    uv run python -m signal_scout.mcp_server --transport stdio      # Claude Desktop / Inspector
    uv run python -m signal_scout.mcp_server --transport http       # Streamable HTTP on MCP_PORT (/mcp)

Built on the official `mcp` SDK v2, where FastMCP is called `MCPServer`.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings, get_settings
from .jobruns import track_job_run
from .log import configure_logging, get_logger
from .metrics import MCP_TOOL_SECONDS
from .search.query import MAX_PAGE_SIZE, SearchParams
from .services import competitors as competitor_service
from .services import insights as insight_service
from .services.context import ServiceContext, service_context
from .services.errors import ServiceError
from .services.insights import InsightFilters
from .services.schemas import Comparison, CompetitorBrief, InsightSources, RecentChanges, SearchResult

log = get_logger(__name__)

SERVER_NAME = "signal-scout"
INSTRUCTIONS = """Signal Scout tracks competitors' news, launches, pricing and website changes and turns them
into ranked insights. Start with the `competitors://list` resource to see who is tracked. Use
search_insights for questions about a topic, get_competitor_brief for "what is X doing", list_recent_changes
for pricing/launch/page changes, compare_competitors for side-by-side numbers, and get_signal_sources to
cite the articles behind an insight. Always cite source URLs when you report an insight."""
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
TOP_RESOURCE_LIMIT = 10

ChangeType = Literal["pricing_change", "product_launch", "webpage_change"]
Days = Annotated[int, Field(ge=1, le=365, description="Look-back window in days.")]


class _Deps:
    """Holds the ServiceContext: set by the lifespan in production, or injected by tests."""

    def __init__(self, ctx: ServiceContext | None) -> None:
        self.ctx = ctx

    def require(self) -> ServiceContext:
        if self.ctx is None:
            raise ToolError("server is not initialised")
        return self.ctx


def create_mcp_server(settings: Settings | None = None, context: ServiceContext | None = None) -> MCPServer[Any]:
    """Build the MCP server. With `context`, tools use it (tests); otherwise the lifespan connects."""
    deps = _Deps(context)
    cfg = settings or (context.settings if context else get_settings())

    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[None]:
        async with service_context(cfg) as ctx:
            deps.ctx = ctx
            try:
                yield
            finally:
                deps.ctx = None

    server: MCPServer[Any] = MCPServer(
        SERVER_NAME,
        title="Signal Scout",
        instructions=INSTRUCTIONS,
        lifespan=None if context is not None else lifespan,
    )

    @asynccontextmanager
    async def tool_call(name: str, args: dict[str, Any]) -> AsyncIterator[ServiceContext]:
        """Log, time and record one tool call; turn service errors into MCP tool errors."""
        ctx = deps.require()
        started = time.perf_counter()
        outcome = "error"
        try:
            async with track_job_run(ctx.sessions, f"mcp_tool:{name}", tool=name) as run:
                run.details["args"] = args
                try:
                    yield ctx
                except ServiceError as exc:
                    raise ToolError(str(exc)) from exc
                run.items_out = 1
                outcome = "ok"
        finally:
            elapsed = time.perf_counter() - started
            MCP_TOOL_SECONDS.labels(tool=name, outcome=outcome).observe(elapsed)
            log.info("mcp_tool_call", tool=name, args=args, outcome=outcome, latency_ms=round(elapsed * 1000, 1))

    # --- Tools ------------------------------------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def search_insights(
        query: Annotated[str, Field(description="Free-text query, e.g. 'pricing credits' or 'outage'.")],
        competitor: Annotated[str | None, Field(description="Exact competitor name, e.g. 'Notion'.")] = None,
        category: Annotated[str | None, Field(description="e.g. pricing_change, product_launch, funding.")] = None,
        days: Annotated[int | None, Field(ge=1, le=365, description="Only insights seen in the last N days.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Maximum results.")] = 10,
    ) -> SearchResult:
        """Full-text search over competitor insights, ranked by relevance x importance x recency.

        Returns headlines, summaries, why each matters for a seller, highlights and source URLs.
        """
        args = {"query": query, "competitor": competitor, "category": category, "days": days, "limit": limit}
        async with tool_call("search_insights", args) as ctx:
            since = datetime.now(UTC) - timedelta(days=days) if days else None
            params = SearchParams(q=query, competitor=competitor, category=category, date_from=since, size=limit)
            return await insight_service.search_insights(ctx.es, params)

    @server.tool(annotations=READ_ONLY)
    async def get_competitor_brief(
        competitor: Annotated[str, Field(description="Competitor name or alias, e.g. 'ClickUp'.")],
        days: Days = 30,
    ) -> CompetitorBrief:
        """What one competitor has been doing: top insights, counts by category, notable pricing/page changes."""
        args = {"competitor": competitor, "days": days}
        async with tool_call("get_competitor_brief", args) as ctx, ctx.sessions() as session:
            return await competitor_service.competitor_brief(session, competitor, days)

    @server.tool(annotations=READ_ONLY)
    async def list_recent_changes(
        competitor: Annotated[str | None, Field(description="Competitor name; omit for all competitors.")] = None,
        change_type: Annotated[ChangeType | None, Field(description="Omit for every change type.")] = None,
        days: Days = 7,
    ) -> RecentChanges:
        """Recent pricing changes, product launches and watched-web-page changes (with the diff), newest first."""
        args = {"competitor": competitor, "change_type": change_type, "days": days}
        async with tool_call("list_recent_changes", args) as ctx, ctx.sessions() as session:
            return await competitor_service.list_recent_changes(session, competitor, change_type, days)

    @server.tool(annotations=READ_ONLY)
    async def compare_competitors(
        competitors: Annotated[list[str], Field(min_length=1, max_length=10, description="Names to compare.")],
        days: Days = 30,
    ) -> Comparison:
        """Side by side: insight counts by category, average importance and the top insight per competitor."""
        args = {"competitors": competitors, "days": days}
        async with tool_call("compare_competitors", args) as ctx, ctx.sessions() as session:
            return await competitor_service.compare_competitors(session, competitors, days)

    @server.tool(annotations=READ_ONLY)
    async def get_signal_sources(
        insight_id: Annotated[int, Field(ge=1, description="Insight id from another tool's results.")],
    ) -> InsightSources:
        """The source articles behind an insight (title, URL, source type, date), so you can cite them."""
        async with tool_call("get_signal_sources", {"insight_id": insight_id}) as ctx, ctx.sessions() as session:
            return await insight_service.get_signal_sources(session, insight_id)

    # --- Resources --------------------------------------------------------------------------------

    @server.resource("competitors://list", name="competitors", mime_type="application/json")
    async def competitors_resource() -> str:
        """Every tracked competitor with aliases and domains."""
        async with deps.require().sessions() as session:
            items = await competitor_service.list_competitors(session)
        return "[" + ",".join(c.model_dump_json() for c in items) + "]"

    @server.resource("insights://top", name="top-insights", mime_type="application/json")
    async def top_insights_resource() -> str:
        """The 10 highest-ranked insights right now (the head of the ranked feed)."""
        async with deps.require().sessions() as session:
            page = await insight_service.list_insights(session, InsightFilters(), None, TOP_RESOURCE_LIMIT)
        return page.model_dump_json()

    # --- Prompts ----------------------------------------------------------------------------------

    @server.prompt(name="weekly_competitive_digest", title="Weekly competitive digest")
    def weekly_competitive_digest(competitors: str = "") -> str:
        """A digest of the last 7 days for sellers. `competitors`: comma-separated names (empty = all)."""
        scope = f"these competitors: {competitors}" if competitors.strip() else "every tracked competitor"
        return (
            f"Write a weekly competitive digest for a sales team covering {scope}, for the last 7 days.\n\n"
            "1. Read competitors://list to get the tracked names.\n"
            "2. Call list_recent_changes(days=7) for pricing changes, launches and web-page changes.\n"
            "3. For each competitor in scope, call get_competitor_brief(competitor, days=7).\n"
            "4. For the 3 most important insights overall, call get_signal_sources to get citable links.\n\n"
            "Format: a 3-bullet executive summary, then one section per competitor with what changed, why it "
            "matters in a deal, and a suggested talk track. Cite every claim with its source URL. If a "
            "competitor had no activity, say so in one line. Do not invent facts that the tools did not return."
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Scout MCP server.")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default=None, help="HTTP bind host (default MCP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default MCP_PORT)")
    args = parser.parse_args()
    settings = get_settings()
    # On stdio, stdout is the protocol channel: logs must go to stderr.
    configure_logging(settings.log_level, stream=sys.stderr if args.transport == "stdio" else None)
    server = create_mcp_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host or settings.mcp_host, port=args.port or settings.mcp_port)


if __name__ == "__main__":
    main()
