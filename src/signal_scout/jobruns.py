"""Record every job execution in `job_runs`, and tag its log lines with a `run_id`.

The row is written (and committed) before the job does any work and updated when it finishes, in
its own sessions. So a crash still leaves a `running` row behind, and a job whose own transaction
rolls back still gets its failure recorded. This table is what dashboards and freshness checks
read; logs are for drilling into one run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import update

from .db.models import JobRun, JobRunStatus
from .db.session import SessionFactory
from .log import get_logger, redact_text

log = get_logger(__name__)
MAX_ERROR_CHARS = 2000


@dataclass
class RunStats:
    """Counters a job updates while it runs; written to `job_runs` when it finishes."""

    job_type: str
    run_id: uuid.UUID
    items_in: int = 0
    items_out: int = 0
    errors: int = 0
    tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    last_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def record_error(self, message: str) -> None:
        """Count a non-fatal, per-item error (the run can still succeed)."""
        self.errors += 1
        self.last_error = redact_text(message)[:MAX_ERROR_CHARS]


@asynccontextmanager
async def track_job_run(sessions: SessionFactory, job_type: str, **log_context: Any) -> AsyncIterator[RunStats]:
    """Wrap one job execution: insert a `running` row, then mark it `success` or `failed`.

    Exceptions are recorded and re-raised, so the queue still sees the failure and retries.
    """
    stats = RunStats(job_type=job_type, run_id=uuid.uuid4())
    async with sessions() as session:
        session.add(JobRun(job_type=job_type, run_id=stats.run_id))
        await session.commit()

    started = time.monotonic()
    with structlog.contextvars.bound_contextvars(run_id=str(stats.run_id), job_type=job_type, **log_context):
        log.info("job_started")
        status = JobRunStatus.success
        try:
            yield stats
        except BaseException as exc:
            status = JobRunStatus.failed
            stats.errors += 1
            stats.last_error = redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]
            raise
        finally:
            async with sessions() as session:
                await session.execute(
                    update(JobRun)
                    .where(JobRun.run_id == stats.run_id)
                    .values(
                        finished_at=datetime.now(UTC),
                        status=status,
                        items_in=stats.items_in,
                        items_out=stats.items_out,
                        errors=stats.errors,
                        last_error=stats.last_error,
                        tokens=stats.tokens,
                        cost_usd=stats.cost_usd,
                        details=stats.details,
                    )
                )
                await session.commit()
            event = "job_finished" if status is JobRunStatus.success else "job_failed"
            (log.info if status is JobRunStatus.success else log.error)(
                event,
                status=status.value,
                items_in=stats.items_in,
                items_out=stats.items_out,
                errors=stats.errors,
                duration_s=round(time.monotonic() - started, 3),
                error=stats.last_error if status is JobRunStatus.failed else None,
            )
