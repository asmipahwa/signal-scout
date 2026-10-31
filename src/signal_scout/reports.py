"""Plain-text reports straight from Postgres: how the pipeline is doing and where signals come from.

These are the evidence behind the README's numbers, so they read only from the source of truth and
print raw counts (no estimates).

    uv run python -m signal_scout.reports runs      # make runs-report
    uv run python -m signal_scout.reports sources   # make source-report
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select

from .config import get_settings
from .db.models import Enrichment, Insight, JobRun, JobRunStatus, LlmCall, Signal, SignalStatus, Source
from .db.session import SessionFactory, make_engine, make_session_factory

FUNNEL = [
    SignalStatus.new,
    SignalStatus.filtered_out,
    SignalStatus.duplicate,
    SignalStatus.deferred,
    SignalStatus.enrichment_failed,
    SignalStatus.not_relevant,
    SignalStatus.enriched,
]


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [list(map(str, headers)), *([str(c) for c in r] for r in rows)]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def line(row: list[str]) -> str:
        padded = (
            c.rjust(w) if c.replace(".", "").replace("-", "").isdigit() else c.ljust(w)
            for c, w in zip(row, widths, strict=True)
        )
        return "| " + " | ".join(padded) + " |"

    return "\n".join([line(cells[0]), "|" + "|".join("-" * (w + 2) for w in widths) + "|", *map(line, cells[1:])])


async def runs_report(sessions: SessionFactory, hours: int = 24) -> str:
    since = datetime.now(UTC) - timedelta(hours=hours)
    out = [f"# Signal Scout runs report ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)", ""]
    async with sessions() as session:
        jobs = (
            await session.execute(
                select(
                    JobRun.job_type,
                    func.count(),
                    func.sum(case((JobRun.status == JobRunStatus.success, 1), else_=0)),
                    func.sum(case((JobRun.status == JobRunStatus.failed, 1), else_=0)),
                    func.sum(JobRun.items_in),
                    func.sum(JobRun.items_out),
                    func.sum(JobRun.tokens),
                    func.sum(JobRun.cost_usd),
                )
                .where(JobRun.started_at >= since)
                .group_by(JobRun.job_type)
                .order_by(JobRun.job_type)
            )
        ).all()
        by_type = (
            await session.execute(
                select(Source.type, Signal.status, func.count())
                .join(Source, Source.id == Signal.source_id)
                .group_by(Source.type, Signal.status)
            )
        ).all()
        enriched = (await session.execute(select(func.count()).select_from(Enrichment))).scalar_one()
        insights = (await session.execute(select(func.count()).select_from(Insight))).scalar_one()
        llm = (await session.execute(select(func.count(), func.coalesce(func.sum(LlmCall.cost_usd), 0)))).one()
        span = (await session.execute(select(func.min(Signal.fetched_at), func.max(Signal.fetched_at)))).one()

    out += [f"## Job runs, last {hours} h", ""]
    out.append(
        _table(
            ["job_type", "runs", "ok", "failed", "items_in", "items_out", "tokens", "cost_usd"],
            [[j, n, ok, bad, i or 0, o or 0, t or 0, f"{c or 0:.4f}"] for j, n, ok, bad, i, o, t, c in jobs],
        )
    )
    types = sorted({t.value for t, _, _ in by_type})
    counts = {(t.value, s.value): n for t, s, n in by_type}
    total_by_type = {t: sum(n for (tt, _), n in counts.items() if tt == t) for t in types}
    out += ["", "## All signals ever ingested, by source type and pipeline status", ""]
    rows = [[t, total_by_type[t], *[counts.get((t, s.value), 0) for s in FUNNEL]] for t in types]
    rows.append(
        ["TOTAL", sum(total_by_type.values()), *[sum(counts.get((t, s.value), 0) for t in types) for s in FUNNEL]]
    )
    out.append(_table(["source_type", "ingested", *[s.value for s in FUNNEL]], rows))
    total = sum(total_by_type.values())
    relevant_input = total - sum(
        counts.get((t, s.value), 0) for t in types for s in (SignalStatus.filtered_out, SignalStatus.duplicate)
    )
    out += [
        "",
        "## Funnel",
        "",
        f"- ingested: {total}",
        f"- after dedupe + relevance filters (sent or queued for the LLM): {relevant_input}",
        f"- enriched by the LLM (any outcome): {enriched}",
        f"- relevant after enrichment: {sum(counts.get((t, 'enriched'), 0) for t in types)}",
        f"- insights: {insights}",
        f"- LLM calls: {llm[0]}, paid spend ${llm[1]:.4f}",
        f"- first signal fetched {span[0]:%Y-%m-%d %H:%M}, latest {span[1]:%Y-%m-%d %H:%M} UTC"
        if span[0]
        else "- no signals",
    ]
    return "\n".join(out) + "\n"


async def sources_report(sessions: SessionFactory) -> str:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    Source.name,
                    Source.type,
                    Source.enabled,
                    Source.consecutive_failures,
                    Source.last_polled_at,
                    func.count(Signal.id),
                    func.sum(case((Signal.status == SignalStatus.enriched, 1), else_=0)),
                )
                .outerjoin(Signal, Signal.source_id == Source.id)
                .group_by(Source.id)
                .order_by(func.count(Signal.id).desc())
            )
        ).all()
    table = _table(
        ["source", "type", "enabled", "failures", "last_polled (UTC)", "signals", "relevant"],
        [
            [n, t.value, "yes" if e else "NO", f, p.strftime("%Y-%m-%d %H:%M") if p else "never", c, r or 0]
            for n, t, e, f, p, c, r in rows
        ],
    )
    return f"# Signal Scout source yield ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n\n{table}\n"


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline reports from Postgres.")
    parser.add_argument("report", choices=["runs", "sources"])
    parser.add_argument("--hours", type=int, default=24, help="window for the job-runs table")
    args = parser.parse_args()
    engine = make_engine(get_settings().sqlalchemy_url, pool_size=1)
    sessions = make_session_factory(engine)
    try:
        print(await runs_report(sessions, args.hours) if args.report == "runs" else await sources_report(sessions))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
