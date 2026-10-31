"""`make seed` is idempotent and never resets a source's operational state."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select, update

from signal_scout.db.models import Competitor, Source
from signal_scout.db.session import SessionFactory
from signal_scout.seed import apply_seed, load_seed

SEED = Path(__file__).resolve().parents[2] / "seeds" / "competitors.yaml"


async def test_seeding_twice_creates_no_duplicates_and_keeps_source_state(sessions: SessionFactory) -> None:
    seed = load_seed(SEED)
    assert await apply_seed(sessions, seed) == (8, 22)

    async with sessions() as session:
        await session.execute(
            update(Source).where(Source.name == "Notion releases").values(enabled=False, etag='"kept"')
        )
        await session.commit()

    await apply_seed(sessions, seed)
    async with sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Competitor))).scalar_one() == 8
        assert (await session.execute(select(func.count()).select_from(Source))).scalar_one() == 22
        notion = (await session.execute(select(Source).where(Source.name == "Notion releases"))).scalar_one()
        general = (await session.execute(select(Source).where(Source.name == "TechCrunch"))).scalar_one()
    assert notion.enabled is False and notion.etag == '"kept"'
    assert notion.competitor_id is not None and general.competitor_id is None
