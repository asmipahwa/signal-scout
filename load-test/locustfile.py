"""Load test for insight search: GET /api/v1/search with a realistic mix of queries.

The mix approximates how sales and product-marketing users search: competitor names, event types
("pricing", "outage"), filters by competitor/category/importance/date, free-text phrases, and a tail of
rare queries. Weights are per task (`@task(n)`). Run against the local stack:

    uv run locust -f load-test/locustfile.py --host http://localhost:8000 \\
        --users 50 --spawn-rate 10 --run-time 5m --headless \\
        --csv docs/results/<date>-search/search --html docs/results/<date>-search/report.html
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from locust import HttpUser, between, task

COMPETITORS = ["Asana", "Monday.com", "ClickUp", "Notion", "Smartsheet", "Airtable", "Linear", "Atlassian"]
CATEGORIES = ["pricing_change", "product_launch", "feature_update", "partnership", "outage_or_incident", "acquisition"]
COMMON = ["pricing", "AI agents", "outage", "launch", "integration", "enterprise", "security", "acquisition"]
PHRASES = [
    "new pricing plan",
    "AI agent launch",
    "price increase per seat",
    "Jira automation",
    "Notion AI credits",
    "layoffs restructuring",
    "Salesforce integration",
    "workflow automation",
]
LONG_TAIL = ["SOC 2", "HIPAA", "Gantt chart", "offline mode", "data residency EU", "SCIM provisioning", "webhooks API"]


class SearchUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def _search(self, name: str, **params: object) -> None:
        self.client.get("/api/v1/search", params={k: v for k, v in params.items() if v is not None}, name=name)

    @task(5)
    def competitor_name(self) -> None:
        self._search("q=competitor", q=random.choice(COMPETITORS))

    @task(4)
    def common_term(self) -> None:
        self._search("q=common", q=random.choice(COMMON))

    @task(3)
    def term_with_competitor_filter(self) -> None:
        self._search("q+competitor", q=random.choice(COMMON), competitor=random.choice(COMPETITORS))

    @task(3)
    def ranked_feed_filtered(self) -> None:
        self._search(
            "feed+filters", competitor=random.choice(COMPETITORS), category=random.choice(CATEGORIES), min_importance=3
        )

    @task(2)
    def phrase(self) -> None:
        self._search("q=phrase", q=random.choice(PHRASES))

    @task(2)
    def date_range(self) -> None:
        start = datetime.now(UTC) - timedelta(days=random.choice([7, 30, 90, 365]))
        self._search("q+date", q=random.choice(COMMON), **{"from": start.date().isoformat()})

    @task(1)
    def long_tail(self) -> None:
        self._search("q=long-tail", q=random.choice(LONG_TAIL), page=random.choice([1, 2]))
