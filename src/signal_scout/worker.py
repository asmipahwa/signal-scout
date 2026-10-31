"""The worker process: claim jobs from the Postgres queue and run them, one at a time, forever.

Every job runs inside `track_job_run`, so each execution leaves a `job_runs` row and its log lines
carry the same `run_id`. Run several workers for more throughput; SKIP LOCKED keeps them apart.

    uv run python -m signal_scout.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from . import queue
from .config import get_settings
from .ingest.fetch_source import IngestContext, fetch_source
from .jobruns import RunStats, track_job_run
from .log import configure_logging, get_logger, redact_text
from .pipeline.alerts import deliver_webhook, send_alerts
from .pipeline.enrich import enrich_signals
from .pipeline.index import index_insights
from .pipeline.insights import build_insights
from .runtime import ingest_context

log = get_logger(__name__)

Handler = Callable[[IngestContext, dict[str, Any], RunStats], Awaitable[None]]

HANDLERS: dict[str, Handler] = {
    "fetch_source": fetch_source,
    "enrich_signals": enrich_signals,
    "build_insights": build_insights,
    "index_insights": index_insights,
    "send_alerts": send_alerts,
    "deliver_webhook": deliver_webhook,
}


class Worker:
    """Polls the queue and dispatches each job to its handler by `kind`."""

    def __init__(
        self,
        ctx: IngestContext,
        handlers: Mapping[str, Handler] | None = None,
        *,
        worker_id: str | None = None,
        poll_s: float = 2.0,
    ) -> None:
        self.ctx = ctx
        self.handlers = dict(handlers or HANDLERS)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_s = poll_s

    async def run_once(self) -> bool:
        """Run at most one job. Returns False if there was nothing to do."""
        async with self.ctx.sessions() as session:
            job = await queue.claim(session, self.worker_id, kinds=list(self.handlers))
        if job is None:
            return False
        handler = self.handlers[job.kind]
        try:
            async with track_job_run(self.ctx.sessions, job.kind, job_id=job.id, attempt=job.attempts) as run:
                await handler(self.ctx, job.payload, run)
        except Exception as exc:
            async with self.ctx.sessions() as session:
                retry = await queue.fail(session, job, redact_text(f"{type(exc).__name__}: {exc}"))
            log.warning("job_will_retry" if retry else "job_dead_lettered", job_id=job.id, kind=job.kind)
        else:
            async with self.ctx.sessions() as session:
                await queue.complete(session, job.id)
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("worker_started", worker_id=self.worker_id, kinds=sorted(self.handlers))
        while not stop.is_set():
            try:
                did_work = await self.run_once()
            except Exception:  # a DB blip must not kill the worker; log and back off
                log.exception("worker_loop_error")
                did_work = False
            if not did_work:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
        log.info("worker_stopped", worker_id=self.worker_id)


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    async with ingest_context(settings) as ctx:
        await Worker(ctx, poll_s=settings.worker_poll_s).run_forever(_stop_event())


if __name__ == "__main__":
    asyncio.run(_main())
