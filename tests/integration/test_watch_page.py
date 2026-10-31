"""watch_page on real Postgres + MinIO: baseline, cosmetic edits ignored, a price change becomes one signal.

The pages are small hand-written HTML fixtures (not captured from a real site) so each version differs in
exactly one known way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from signal_scout.config import Settings
from signal_scout.db.models import Competitor, PageSnapshot, Signal, SignalStatus, Source, SourceType
from signal_scout.db.session import SessionFactory
from signal_scout.ingest.fetch_source import IngestContext
from signal_scout.ingest.http import PoliteHttpClient, RetryPolicy
from signal_scout.ingest.ratelimit import DomainRateLimiter
from signal_scout.ingest.watch_page import watch_page
from signal_scout.jobruns import track_job_run
from signal_scout.storage import S3BlobStore, text_key

PAGE = "https://acme.example.test/pricing"


def pricing_html(plus_price: str, footer_year: str = "2026", updated: str = "3 days ago") -> bytes:
    return f"""<html><body>
<div class="cookie">Accept all cookies</div>
<main><article>
<h1>Acme Boards pricing</h1>
<p>Simple plans for teams of every size. Start free and upgrade when your team grows.</p>
<table>
<tr><th>Plan</th><th>Price</th><th>Includes</th></tr>
<tr><td>Free</td><td>$0 per seat / month</td><td>Up to 10 members and unlimited boards</td></tr>
<tr><td>Plus</td><td>{plus_price} per seat / month</td><td>Timelines, automations and guest access</td></tr>
<tr><td>Business</td><td>$20 per seat / month</td><td>SAML SSO, admin controls and audit logs</td></tr>
</table>
<p>Prices updated {updated}. Annual billing saves 20 percent on every paid plan.</p>
</article></main>
<footer>© {footer_year} Acme Boards</footer>
</body></html>""".encode()


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def ctx(settings: Settings, sessions: SessionFactory, blobs: S3BlobStore) -> AsyncIterator[IngestContext]:
    client = PoliteHttpClient.build_client(settings.http_user_agent, 5)
    http = PoliteHttpClient(
        client, DomainRateLimiter(1000, 1000), RetryPolicy(max_retries=1, base_s=0.01), sleep=_no_sleep
    )
    yield IngestContext(settings=settings, sessions=sessions, blobs=blobs, http=http)
    await client.aclose()


async def _poll(ctx: IngestContext, source_id: int, body: bytes) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(PAGE).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "text/html"}))
        async with track_job_run(ctx.sessions, "watch_page") as run:
            await watch_page(ctx, {"source_id": source_id}, run)


async def _counts(ctx: IngestContext) -> tuple[int, int]:
    async with ctx.sessions() as session:
        snapshots = (await session.execute(select(func.count()).select_from(PageSnapshot))).scalar_one()
        signals = (await session.execute(select(func.count()).select_from(Signal))).scalar_one()
    return snapshots, signals


async def test_only_meaningful_page_changes_become_signals(ctx: IngestContext) -> None:
    async with ctx.sessions() as session:
        comp = Competitor(name="Acme Boards", aliases=["Acme Boards"], domains=["acme.example.test"])
        session.add(comp)
        await session.flush()
        source = Source(
            name="Acme pricing page",
            type=SourceType.webpage_watch,
            url=PAGE,
            competitor_id=comp.id,
            poll_interval=timedelta(hours=12),
        )
        session.add(source)
        await session.commit()
        source_id = source.id

    await _poll(ctx, source_id, pricing_html("$10"))
    assert await _counts(ctx) == (1, 0)  # baseline snapshot, no signal

    await _poll(ctx, source_id, pricing_html("$10", footer_year="2027", updated="5 days ago"))
    assert await _counts(ctx) == (1, 0)  # cosmetic only: same normalized hash, nothing stored

    await _poll(ctx, source_id, pricing_html("$12"))
    assert await _counts(ctx) == (2, 1)  # price change: new snapshot and exactly one signal

    async with ctx.sessions() as session:
        signal = (await session.execute(select(Signal))).scalar_one()
    assert signal.status == SignalStatus.new
    assert signal.matched_competitors == ["Acme Boards"]
    diff_text = (await ctx.blobs.get(text_key(signal.content_hash))).decode()
    removed = [line for line in diff_text.splitlines() if line.startswith("- ")]
    added = [line for line in diff_text.splitlines() if line.startswith("+ ")]
    assert len(removed) == len(added) == 1  # only the Plus row changed
    assert "Plus" in removed[0] and "$10" in removed[0]
    assert "Plus" in added[0] and "$12" in added[0]

    await _poll(ctx, source_id, pricing_html("$12"))
    assert await _counts(ctx) == (2, 1)  # polling the unchanged page again adds nothing
