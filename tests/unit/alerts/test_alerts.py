"""Webhook signing/verification and the Slack message format."""

from __future__ import annotations

import json

import pytest

from signal_scout.alerts.signing import SignatureError, sign, verify
from signal_scout.alerts.slack import AlertInsight, build_slack_message

SECRET = "whsec_test"
BODY = json.dumps({"title": "Acme raises prices"}).encode()
NOW = 1_800_000_000


def test_valid_signature_verifies() -> None:
    verify(SECRET, BODY, sign(SECRET, BODY, timestamp=NOW), now=NOW + 10)


@pytest.mark.parametrize(
    "header,match",
    [
        (None, "missing"),
        ("garbage", "malformed"),
        ("t=abc,v1=00", "malformed"),
        (f"t={NOW},v1=" + "0" * 64, "mismatch"),
    ],
)
def test_bad_signatures_are_rejected(header: str | None, match: str) -> None:
    with pytest.raises(SignatureError, match=match):
        verify(SECRET, BODY, header, now=NOW)


def test_tampered_body_is_rejected() -> None:
    header = sign(SECRET, BODY, timestamp=NOW)
    with pytest.raises(SignatureError, match="mismatch"):
        verify(SECRET, BODY + b" ", header, now=NOW)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(SignatureError, match="mismatch"):
        verify("other", BODY, sign(SECRET, BODY, timestamp=NOW), now=NOW)


def test_stale_timestamp_is_rejected_as_replay() -> None:
    header = sign(SECRET, BODY, timestamp=NOW)
    with pytest.raises(SignatureError, match="replay"):
        verify(SECRET, BODY, header, now=NOW + 301)


def test_slack_message_has_fallback_text_sections_and_capped_source_links() -> None:
    insight = AlertInsight(
        insight_id="ins_1",
        competitor="Notion",
        category="pricing_change",
        importance=5,
        headline="Notion AI moves to a credit system",
        summary="Notion switched AI pricing from flat seats to credits <for Business>.",
        why_it_matters="Prospects worried about unpredictable AI costs will ask about this.",
        sources=[(f"Source {i}", f"https://example.com/{i}") for i in range(7)],
    )
    msg = build_slack_message(insight, dashboard_url="https://scout.example.com/insights/ins_1")
    assert msg["text"] == "Notion: Notion AI moves to a credit system"
    rendered = json.dumps(msg["blocks"])
    assert "Pricing change" in rendered and "importance 5/5" in rendered
    assert "&lt;for Business&gt;" in rendered  # mrkdwn-escaped
    assert "Source 4" in rendered and "Source 5" not in rendered and "and 2 more" in rendered
    assert msg["blocks"][-1]["type"] == "actions"
    assert len(msg["blocks"][0]["text"]["text"]) <= 150
