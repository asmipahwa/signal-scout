"""Feed cursor encoding, and the MCP server's registered tools, resources and prompt (no database)."""

from __future__ import annotations

import base64
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from signal_scout.config import Settings
from signal_scout.mcp_server import create_mcp_server
from signal_scout.services.cursor import FeedCursor, decode_cursor, encode_cursor
from signal_scout.services.errors import InvalidRequestError


@given(st.floats(allow_nan=False, allow_infinity=False), st.integers(min_value=1, max_value=2**63 - 1))
def test_cursor_round_trips_exactly(score: float, row_id: int) -> None:
    token = encode_cursor(FeedCursor(score, row_id))
    assert "=" not in token and "/" not in token and "+" not in token  # URL-safe, unpadded
    assert decode_cursor(token) == FeedCursor(score, row_id)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!",
        _b64(b"not json"),
        _b64(b"[]"),
        _b64(b'{"s": 1.0}'),
        _b64(b'{"s": "1.0", "i": 3}'),
        _b64(b'{"s": 1.0, "i": "3"}'),
        _b64(b'{"s": true, "i": 3}'),
        _b64(b'{"s": 1e999, "i": 3}'),
    ],
)
def test_bad_cursors_are_rejected(token: str) -> None:
    with pytest.raises(InvalidRequestError, match="invalid cursor"):
        decode_cursor(token)


SERVER = create_mcp_server(Settings(_env_file=None))  # type: ignore[call-arg]
EXPECTED_TOOLS = {
    "search_insights": ({"query", "competitor", "category", "days", "limit"}, {"query"}),
    "get_competitor_brief": ({"competitor", "days"}, {"competitor"}),
    "list_recent_changes": ({"competitor", "change_type", "days"}, set()),
    "compare_competitors": ({"competitors", "days"}, {"competitors"}),
    "get_signal_sources": ({"insight_id"}, {"insight_id"}),
}


async def test_tools_are_registered_with_typed_schemas() -> None:
    tools = {t.name: t for t in await SERVER.list_tools()}
    assert set(tools) == set(EXPECTED_TOOLS)
    for name, (params, required) in EXPECTED_TOOLS.items():
        tool = tools[name]
        assert set(tool.input_schema["properties"]) == params
        assert set(tool.input_schema.get("required", [])) == required
        assert tool.description and len(tool.description) > 30
        assert tool.output_schema is not None  # structured output from the Pydantic return type
        assert tool.annotations is not None and tool.annotations.read_only_hint is True


async def test_tool_parameter_constraints_reach_the_schema() -> None:
    tools = {t.name: t for t in await SERVER.list_tools()}
    days = tools["get_competitor_brief"].input_schema["properties"]["days"]
    assert days["minimum"] == 1 and days["maximum"] == 365 and days["default"] == 30
    change = json.dumps(tools["list_recent_changes"].input_schema["properties"]["change_type"])
    assert all(f'"{v}"' in change for v in ("pricing_change", "product_launch", "webpage_change"))
    assert tools["compare_competitors"].input_schema["properties"]["competitors"]["type"] == "array"


async def test_resources_and_prompt_are_registered() -> None:
    assert {str(r.uri) for r in await SERVER.list_resources()} == {"competitors://list", "insights://top"}
    prompts = {p.name: p for p in await SERVER.list_prompts()}
    assert set(prompts) == {"weekly_competitive_digest"}
    result = await SERVER.get_prompt("weekly_competitive_digest", {"competitors": "Notion, Asana"})
    text = str(result.messages[0].content)
    assert "Notion, Asana" in text and "get_signal_sources" in text
