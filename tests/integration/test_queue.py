"""The Postgres queue against a real Postgres: exclusive claims under concurrency, backoff, dead letters."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from signal_scout import queue
from signal_scout.db.models import Job
from signal_scout.db.session import SessionFactory

N_JOBS = 200
N_WORKERS = 10


async def test_concurrent_workers_never_claim_the_same_job(sessions: SessionFactory) -> None:
    async with sessions() as session:
        for i in range(N_JOBS):
            await queue.enqueue(session, "noop", {"i": i})
        await session.commit()

    claimed: list[tuple[str, int]] = []

    async def worker(name: str) -> None:
        while True:
            async with sessions() as session:
                job = await queue.claim(session, name)
            if job is None:
                return
            claimed.append((name, job.id))
            await asyncio.sleep(0)  # let other workers interleave
            async with sessions() as session:
                await queue.complete(session, job.id)

    await asyncio.gather(*(worker(f"w{n}") for n in range(N_WORKERS)))

    ids = [job_id for _, job_id in claimed]
    assert len(ids) == N_JOBS
    assert len(set(ids)) == N_JOBS, "a job was claimed twice"
    assert len(Counter(name for name, _ in claimed)) > 1, "work was not spread across workers"
    async with sessions() as session:
        assert (await session.execute(select(Job))).first() is None


async def test_failure_backs_off_then_dead_letters(sessions: SessionFactory) -> None:
    async with sessions() as session:
        job_id = await queue.enqueue(session, "flaky", max_attempts=2)
        await session.commit()

    async with sessions() as session:
        job = await queue.claim(session, "w1")
    assert job is not None and job.attempts == 1
    before = datetime.now(UTC)
    async with sessions() as session:
        assert await queue.fail(session, job, "boom") is True
        row = await session.get(Job, job_id)
    assert row is not None
    assert row.locked_at is None and row.last_error == "boom"
    # attempt 1 -> base delay (30s) minus up to 10% jitter
    assert before + timedelta(seconds=26) <= row.run_after <= before + timedelta(seconds=31)

    async with sessions() as session:
        assert await queue.claim(session, "w1") is None  # not runnable until run_after
        await session.execute(update(Job).where(Job.id == job_id).values(run_after=datetime.now(UTC)))
        await session.commit()
        job = await queue.claim(session, "w1")
    assert job is not None and job.attempts == 2

    async with sessions() as session:
        assert await queue.fail(session, job, "boom again") is False
        row = await session.get(Job, job_id)
        assert row is not None and row.failed_at is not None and row.locked_at is not None
        assert await queue.claim(session, "w1") is None  # dead letters are never claimed


async def test_stale_locks_are_released(sessions: SessionFactory) -> None:
    async with sessions() as session:
        job_id = await queue.enqueue(session, "slow")
        await session.commit()
        assert await queue.claim(session, "crashed-worker") is not None
        await session.execute(
            update(Job).where(Job.id == job_id).values(locked_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
        assert await queue.release_stale_locks(session, timedelta(minutes=15)) == 1
        job = await queue.claim(session, "w2")
    assert job is not None and job.id == job_id and job.attempts == 2


async def test_claim_filters_by_kind(sessions: SessionFactory) -> None:
    async with sessions() as session:
        await queue.enqueue(session, "other")
        await session.commit()
        assert await queue.claim(session, "w", kinds=["fetch_source"]) is None
        assert await queue.claim(session, "w", kinds=["other"]) is not None
