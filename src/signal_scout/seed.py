"""Load competitors and their sources from `seeds/competitors.yaml` into Postgres, idempotently.

Competitors are upserted by name and sources by (type, url). Re-running updates descriptive fields
(name, aliases, poll interval, owner) but never touches a source's operational state (enabled,
ETag, failure count), so seeding a live database is safe.

    uv run python -m signal_scout.seed seeds/competitors.yaml
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db.models import Competitor, Source, SourceType
from .db.session import SessionFactory, make_engine, make_session_factory
from .jobruns import track_job_run
from .log import configure_logging, get_logger

log = get_logger(__name__)


class SeedSource(BaseModel):
    """One source entry. `items` is informational (the entry count when the URL was verified)."""

    name: str
    type: SourceType
    url: str
    poll_interval_minutes: int | None = None
    items: int | None = None


class SeedCompetitor(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    sources: list[SeedSource] = Field(default_factory=list)


class SeedDefaults(BaseModel):
    poll_interval_minutes: int = 360


class SeedFile(BaseModel):
    """The whole YAML document, validated before anything is written."""

    defaults: SeedDefaults = Field(default_factory=SeedDefaults)
    competitors: list[SeedCompetitor]
    general_sources: list[SeedSource] = Field(default_factory=list)


def load_seed(path: Path) -> SeedFile:
    return SeedFile.model_validate(yaml.safe_load(path.read_text()))


async def _upsert_source(session: AsyncSession, src: SeedSource, competitor_id: int | None, default_min: int) -> None:
    values = {
        "name": src.name,
        "type": src.type,
        "url": src.url,
        "competitor_id": competitor_id,
        "poll_interval": timedelta(minutes=src.poll_interval_minutes or default_min),
    }
    stmt = insert(Source).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Source.type, Source.url],
        set_={k: stmt.excluded[k] for k in ("name", "competitor_id", "poll_interval")},
    )
    await session.execute(stmt)


async def apply_seed(sessions: SessionFactory, seed: SeedFile) -> tuple[int, int]:
    """Write the seed. Returns (competitors, sources) counts from the file."""
    n_sources = 0
    async with sessions() as session:
        for comp in seed.competitors:
            base = insert(Competitor).values(name=comp.name, aliases=comp.aliases, domains=comp.domains)
            upsert = base.on_conflict_do_update(
                index_elements=[Competitor.name],
                set_={"aliases": base.excluded.aliases, "domains": base.excluded.domains},
            ).returning(Competitor.id)
            competitor_id = (await session.execute(upsert)).scalar_one()
            for src in comp.sources:
                await _upsert_source(session, src, competitor_id, seed.defaults.poll_interval_minutes)
                n_sources += 1
        for src in seed.general_sources:
            await _upsert_source(session, src, None, seed.defaults.poll_interval_minutes)
            n_sources += 1
        await session.commit()
    return len(seed.competitors), n_sources


async def _main(path: Path) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    seed = load_seed(path)
    engine = make_engine(settings.sqlalchemy_url, pool_size=1)
    sessions = make_session_factory(engine)
    try:
        async with track_job_run(sessions, "seed", path=str(path)) as run:
            competitors, sources = await apply_seed(sessions, seed)
            run.items_in = run.items_out = competitors + sources
        log.info("seed_applied", competitors=competitors, sources=sources)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, nargs="?", default=Path("seeds/competitors.yaml"))
    asyncio.run(_main(parser.parse_args().path))
