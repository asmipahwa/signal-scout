"""Read the `job_runs` audit log, so operators (and agents) can see what the pipeline did and when."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import JobRun, JobRunStatus
from .errors import InvalidRequestError
from .schemas import RunOut

MAX_LIMIT = 200


async def list_runs(
    session: AsyncSession, job_type: str | None = None, status: str | None = None, limit: int = 50
) -> list[RunOut]:
    """Newest runs first, optionally filtered by job type and status (running/success/failed)."""
    stmt = select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(max(1, min(limit, MAX_LIMIT)))
    if job_type:
        stmt = stmt.where(JobRun.job_type == job_type)
    if status:
        try:
            stmt = stmt.where(JobRun.status == JobRunStatus(status))
        except ValueError as exc:
            raise InvalidRequestError(f"status must be one of {', '.join(s.value for s in JobRunStatus)}") from exc
    return [
        RunOut(
            id=r.id,
            job_type=r.job_type,
            run_id=str(r.run_id),
            started_at=r.started_at,
            finished_at=r.finished_at,
            status=r.status.value,
            items_in=r.items_in,
            items_out=r.items_out,
            errors=r.errors,
            last_error=r.last_error,
            tokens=r.tokens,
            cost_usd=float(r.cost_usd),
            details=dict(r.details),
        )
        for r in (await session.execute(stmt)).scalars()
    ]
