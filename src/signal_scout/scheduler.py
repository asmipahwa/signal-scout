"""The scheduler process: every tick, enqueue a `fetch_source` job for each source that is due.

A source is due when it is enabled, has a fetcher, was never polled or was last polled more than
`poll_interval` ago, and has no live job already queued. Deciding from the database on every tick
(instead of one in-memory timer per source) means new or re-enabled sources are picked up
without a restart, and a restart never double-enqueues. The tick also releases locks left by
crashed workers.

    uv run python -m signal_scout.scheduler
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import Text, cast, exists, func, or_, select

from . import queue
from .config import Settings, get_settings
from .db.models import RSS_SOURCE_TYPES, Job, Source, SourceType
from .db.session import SessionFactory, make_engine, make_session_factory
from .jobruns import track_job_run
from .log import configure_logging, get_logger

log = get_logger(__name__)

FETCHABLE_TYPES = sorted(RSS_SOURCE_TYPES | {SourceType.hn_search})
FETCH_SOURCE_MAX_ATTEMPTS = 3


async def enqueue_due_sources(sessions: SessionFactory) -> list[int]:
    """Enqueue fetch_source for every due source. Returns the source ids enqueued."""
    pending = exists().where(
        Job.kind == "fetch_source",
        Job.failed_at.is_(None),
        Job.payload["source_id"].astext == cast(Source.id, Text),
    )
    due = select(Source.id).where(
        Source.enabled.is_(True),
        Source.type.in_(FETCHABLE_TYPES),
        or_(Source.last_polled_at.is_(None), Source.last_polled_at + Source.poll_interval <= func.now()),
        ~pending,
    )
    async with sessions() as session:
        source_ids = list((await session.execute(due.order_by(Source.id).with_for_update(skip_locked=True))).scalars())
        for source_id in source_ids:
            await queue.enqueue(
                session, "fetch_source", {"source_id": source_id}, max_attempts=FETCH_SOURCE_MAX_ATTEMPTS
            )
        await session.commit()
    return source_ids


async def tick(sessions: SessionFactory, settings: Settings) -> None:
    """One scheduler pass, recorded in job_runs like any other job."""
    async with track_job_run(sessions, "schedule_sources") as run:
        async with sessions() as session:
            released = await queue.release_stale_locks(session, timedelta(seconds=settings.job_lock_timeout_s))
        enqueued = await enqueue_due_sources(sessions)
        run.items_out = len(enqueued)
        run.details.update(released_stale_locks=released, source_ids=enqueued)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.sqlalchemy_url, pool_size=2)
    sessions = make_session_factory(engine)
    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.add_job(
        tick,
        "interval",
        seconds=settings.scheduler_tick_s,
        args=[sessions, settings],
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler_started", tick_s=settings.scheduler_tick_s)
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
