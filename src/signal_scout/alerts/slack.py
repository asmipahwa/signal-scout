"""Format a high-importance insight as a Slack Block Kit message.

Slack is where sales teams already are, so an alert should be readable without clicking: what
happened, who, why it matters for a deal, and links to the sources so a rep can cite them.
Delivery (HTTP POST to the incoming-webhook URL, with retries) lives with the other outbound
webhooks; this module only builds the payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORY_LABELS = {
    "pricing_change": "Pricing change",
    "product_launch": "Product launch",
    "feature_update": "Feature update",
    "funding": "Funding",
    "acquisition": "Acquisition",
    "partnership": "Partnership",
    "leadership_change": "Leadership change",
    "hiring_signal": "Hiring / layoffs",
    "customer_win": "Customer win",
    "outage_or_incident": "Outage / incident",
    "legal_regulatory": "Legal / regulatory",
    "other": "Other",
}
MAX_SOURCE_LINKS = 5


@dataclass(frozen=True)
class AlertInsight:
    insight_id: str
    competitor: str
    category: str
    importance: int
    headline: str
    summary: str
    why_it_matters: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url)


def _escape(text: str) -> str:
    """Slack mrkdwn treats &, <, > specially."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_message(insight: AlertInsight, dashboard_url: str | None = None) -> dict[str, Any]:
    category = CATEGORY_LABELS.get(insight.category, insight.category)
    fallback = f"{insight.competitor}: {insight.headline}"  # shown in notifications and old clients
    links = [f"• <{url}|{_escape(title[:120])}>" for title, url in insight.sources[:MAX_SOURCE_LINKS]]
    if len(insight.sources) > MAX_SOURCE_LINKS:
        links.append(f"…and {len(insight.sources) - MAX_SOURCE_LINKS} more")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{insight.competitor}: {insight.headline}"[:150]}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*{category}* · importance {insight.importance}/5"}],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _escape(insight.summary)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why it matters:* {_escape(insight.why_it_matters)}"}},
    ]
    if links:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Sources*\n" + "\n".join(links)}})
    if dashboard_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Open insight"}, "url": dashboard_url}
                ],
            }
        )
    return {"text": fallback, "blocks": blocks}
