"""fetch_source end to end: queue -> worker -> HTTP (respx, replaying a real feed) -> MinIO + Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from signal_scout import queue
from signal_scout.config import Settings
from signal_scout.db.models import Competitor, Job, JobRun, JobRunStatus, Signal, SignalStatus, Source, SourceType
from signal_scout.db.session import SessionFactory
from signal_scout.ingest.fetch_source import IngestContext
from signal_scout.ingest.http import PoliteHttpClient, RetryPolicy
from signal_scout.ingest.ratelimit import DomainRateLimiter
from signal_scout.scheduler import enqueue_due_sources
from signal_scout.storage import S3BlobStore, text_key
from signal_scout.worker import Worker

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
NOTION_FEED = "https://www.notion.com/releases/rss.xml"
BING_FEED = "https://www.bing.com/news/search?q=%22Asana%22&format=rss"


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


async def _add_source(sessions: SessionFactory, name: str, type_: SourceType, url: str, competitor: str) -> int:
    async with sessions() as session:
        comp = (await session.execute(select(Competitor).where(Competitor.name == competitor))).scalar_one_or_none()
        if comp is None:
            comp = Competitor(name=competitor, aliases=[competitor], domains=[])
            session.add(comp)
            await session.flush()
        source = Source(name=name, type=type_, url=url, competitor_id=comp.id, poll_interval=timedelta(hours=1))
        session.add(source)
        await session.commit()
        return source.id


@respx.mock
async def test_fetch_source_writes_s3_objects_signals_and_a_job_run(ctx: IngestContext) -> None:
    source_id = await _add_source(ctx.sessions, "Notion releases", SourceType.changelog_rss, NOTION_FEED, "Notion")
    route = respx.get(NOTION_FEED).mock(
        return_value=httpx.Response(
            200, content=(FEEDS / "notion_releases.xml").read_bytes(), headers={"ETag": '"notion-v1"'}
        )
    )

    assert await enqueue_due_sources(ctx.sessions) == [source_id]
    assert await enqueue_due_sources(ctx.sessions) == []  # a pending job is not enqueued twice
    assert await Worker(ctx).run_once() is True

    async with ctx.sessions() as session:
        signals = (await session.execute(select(Signal).order_by(Signal.id))).scalars().all()
        run = (await session.execute(select(JobRun).where(JobRun.job_type == "fetch_source"))).scalar_one()
        source = await session.get(Source, source_id)
        remaining_jobs = (await session.execute(select(func.count()).select_from(Job))).scalar_one()

    assert len(signals) == 154
    assert len({s.canonical_url for s in signals}) == 154
    assert all(s.matched_competitors == ["Notion"] for s in signals)  # official feed of that competitor
    assert all(len(s.excerpt) <= ctx.settings.excerpt_chars for s in signals)
    assert {s.status for s in signals} <= {SignalStatus.new, SignalStatus.duplicate}

    first = signals[0]
    assert first.title == "Agents, rebuilt to do more for less"
    assert first.s3_key.startswith("raw/") and first.s3_key.endswith(".html.gz")
    assert (await ctx.blobs.get(first.s3_key)).decode()  # raw object exists
    # Notion's feed items have no body, so the extracted text falls back to the title.
    assert (await ctx.blobs.get(text_key(first.content_hash))).decode() == first.title

    assert run.status is JobRunStatus.success
    assert (run.items_in, run.items_out, run.errors) == (154, 154, 0)
    assert run.finished_at is not None
    assert source is not None and source.etag == '"notion-v1"' and source.last_polled_at is not None
    assert remaining_jobs == 0

    # Second poll: the conditional request gets a 304, nothing new is written.
    route.mock(return_value=httpx.Response(304))
    async with ctx.sessions() as session:
        await queue.enqueue(session, "fetch_source", {"source_id": source_id})
        await session.commit()
    assert await Worker(ctx).run_once() is True
    assert route.calls.last.request.headers["If-None-Match"] == '"notion-v1"'
    async with ctx.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Signal))).scalar_one() == 154
        runs = (await session.execute(select(JobRun).order_by(JobRun.id))).scalars().all()
    assert [r.items_in for r in runs if r.job_type == "fetch_source"] == [154, 0]


@respx.mock
async def test_text_objects_hold_the_extracted_text(ctx: IngestContext) -> None:
    await _add_source(ctx.sessions, "Bing: Asana", SourceType.news_rss, BING_FEED, "Asana")
    respx.get(BING_FEED).mock(return_value=httpx.Response(200, content=(FEEDS / "bing_news_asana.xml").read_bytes()))
    await enqueue_due_sources(ctx.sessions)
    await Worker(ctx).run_once()
    async with ctx.sessions() as session:
        signals = (await session.execute(select(Signal))).scalars().all()
    assert len(signals) == 10
    assert all("bing.com" not in s.url for s in signals)
    outage = next(s for s in signals if "down for thousands" in s.title)
    text = (await ctx.blobs.get(text_key(outage.content_hash))).decode()
    assert "outage" in text and "<" not in text
    # Search results only count when they actually mention an alias.
    for s in signals:
        assert (s.status is SignalStatus.filtered_out) == (s.matched_competitors == [])


@respx.mock
async def test_repeated_failures_trip_the_circuit_breaker(ctx: IngestContext) -> None:
    source_id = await _add_source(ctx.sessions, "Broken", SourceType.blog_rss, "https://broken.example.com/rss", "X")
    respx.get("https://broken.example.com/rss").mock(return_value=httpx.Response(503))
    threshold = ctx.settings.circuit_breaker_threshold
    for _ in range(threshold):
        async with ctx.sessions() as session:
            await queue.enqueue(session, "fetch_source", {"source_id": source_id}, max_attempts=1)
            await session.commit()
        await Worker(ctx).run_once()

    async with ctx.sessions() as session:
        source = await session.get(Source, source_id)
        runs = (await session.execute(select(JobRun).where(JobRun.job_type == "fetch_source"))).scalars().all()
    assert source is not None
    assert source.enabled is False and source.consecutive_failures == threshold
    assert source.disabled_reason is not None and "HTTP 503" in source.disabled_reason
    assert len(runs) == threshold and all(r.status is JobRunStatus.failed for r in runs)
    assert all(r.last_error and "HTTP 503" in r.last_error for r in runs)
    assert await enqueue_due_sources(ctx.sessions) == []  # disabled sources are not scheduled
