"""A small job queue on Postgres, so the pipeline needs no extra broker (Redis, SQS) to run.

Workers claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, so many workers can poll the same table
and each job goes to exactly one of them. A failed job is retried with exponential backoff (written
to `run_after`) until `max_attempts`, then parked as a dead letter (`failed_at` set, still locked)
for a human to inspect. Workers that crash leave a stale lock, which `release_stale_locks` clears.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Job

DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 6 * 3600.0
MAX_ERROR_CHARS = 2000


@dataclass(frozen=True)
class ClaimedJob:
    """A job this worker now owns. `attempts` already counts the current attempt."""

    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def backoff_seconds(
    attempts: int,
    *,
    base: float = BACKOFF_BASE_S,
    cap: float = BACKOFF_CAP_S,
    jitter: float = 0.0,
    rng: random.Random | None = None,
) -> float:
    """Delay before the next try after `attempts` failed tries: base * 2^(attempts-1), capped.

    `jitter` (0..1) shaves a random fraction off the delay so many failing jobs don't retry in lockstep.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = min(cap, base * 2.0 ** (attempts - 1))
    if jitter:
        delay *= 1 - jitter * float((rng or random).random())
    return delay


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int:
    """Add a job. The caller commits, so enqueueing can share a transaction with other writes."""
    job = Job(kind=kind, payload=payload or {}, max_attempts=max_attempts)
    if run_after is not None:
        job.run_after = run_after
    session.add(job)
    await session.flush()
    return job.id


async def claim(session: AsyncSession, worker_id: str, kinds: list[str] | None = None) -> ClaimedJob | None:
    """Lock the next runnable job for `worker_id`, or return None. Commits so the lock is visible."""
    candidate = (
        select(Job.id)
        .where(Job.locked_at.is_(None), Job.run_after <= func.now())
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        candidate = candidate.where(Job.kind.in_(kinds))
    cte = candidate.cte("next_job")
    stmt = (
        update(Job)
        .where(Job.id == cte.c.id)
        .values(locked_at=func.now(), locked_by=worker_id, attempts=Job.attempts + 1)
        .returning(Job.id, Job.kind, Job.payload, Job.attempts, Job.max_attempts)
    )
    row = (await session.execute(stmt)).one_or_none()
    await session.commit()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id, kind=row.kind, payload=row.payload, attempts=row.attempts, max_attempts=row.max_attempts
    )


async def complete(session: AsyncSession, job_id: int) -> None:
    """Remove a finished job. Its history lives in `job_runs`."""
    await session.execute(delete(Job).where(Job.id == job_id))
    await session.commit()


async def fail(session: AsyncSession, job: ClaimedJob, error: str, *, now: datetime | None = None) -> bool:
    """Record a failure. Returns True if the job will be retried, False if it is now a dead letter."""
    now = now or datetime.now(UTC)
    error = error[:MAX_ERROR_CHARS]
    if job.attempts >= job.max_attempts:
        values: dict[str, Any] = {"last_error": error, "failed_at": now}
        retry = False
    else:
        delay = backoff_seconds(job.attempts, jitter=0.1)
        values = {
            "last_error": error,
            "locked_at": None,
            "locked_by": None,
            "run_after": now + timedelta(seconds=delay),
        }
        retry = True
    await session.execute(update(Job).where(Job.id == job.id).values(**values))
    await session.commit()
    return retry


async def release_stale_locks(session: AsyncSession, older_than: timedelta) -> int:
    """Unlock jobs whose worker died mid-run (not dead letters). Returns how many were released."""
    result = await session.execute(
        update(Job)
        .where(Job.locked_at.is_not(None), Job.failed_at.is_(None), Job.locked_at < func.now() - older_than)
        .values(locked_at=None, locked_by=None)
        .returning(Job.id)
    )
    released = len(result.all())
    await session.commit()
    return released
