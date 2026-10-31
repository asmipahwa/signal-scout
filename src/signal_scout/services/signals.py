"""Signal reads (metadata + a presigned link to the raw object) and inbound signal ingestion.

Inbound signals follow the same storage rules as polled ones (raw body and text content-addressed in
S3, metadata and an excerpt in Postgres, exact dedupe by URL and content hash), so the rest of the
pipeline cannot tell them apart except by their `inbound_webhook` source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Competitor, Enrichment, Signal, SignalStatus, Source, SourceType
from ..ingest.canonical import canonical_url
from ..ingest.matching import CompetitorMatcher
from ..ingest.text import content_hash, excerpt, normalize_text
from ..storage import BlobStore, raw_key, sha256_hex, text_key
from .competitors import resolve_competitor
from .errors import InvalidRequestError, NotFoundError
from .schemas import InboundAccepted, InboundSignal, SignalDetail

INBOUND_SOURCE_NAME = "Inbound webhook"
INBOUND_SOURCE_URL = "inbound://webhook"


class PresigningBlobStore(BlobStore, Protocol):
    """A BlobStore that can also hand out time-limited GET URLs."""

    async def presign_get(self, key: str, expires_s: int = 900) -> str: ...


async def get_signal(
    session: AsyncSession, blobs: PresigningBlobStore, signal_id: int, expires_s: int = 900
) -> SignalDetail:
    """A signal's metadata, its enrichment summary if any, and a presigned URL for the raw object."""
    row = (
        await session.execute(
            select(Signal, Source, Enrichment)
            .join(Source, Source.id == Signal.source_id)
            .outerjoin(Enrichment, Enrichment.signal_id == Signal.id)
            .where(Signal.id == signal_id)
        )
    ).first()
    if row is None:
        raise NotFoundError(f"signal {signal_id} not found")
    signal, source, enrichment = row
    return SignalDetail(
        id=signal.id,
        title=signal.title,
        url=signal.url,
        canonical_url=signal.canonical_url,
        source_id=source.id,
        source_name=source.name,
        source_type=source.type.value,
        published_at=signal.published_at,
        fetched_at=signal.fetched_at,
        status=signal.status.value,
        filter_reason=signal.filter_reason,
        language=signal.language,
        duplicate_of_id=signal.duplicate_of_id,
        matched_competitors=list(signal.matched_competitors),
        excerpt=signal.excerpt,
        content_hash=signal.content_hash,
        s3_key=signal.s3_key,
        raw_url=await blobs.presign_get(signal.s3_key, expires_s) if signal.s3_key else None,
        raw_url_expires_s=expires_s,
        category=enrichment.category if enrichment else None,
        summary=enrichment.summary if enrichment else None,
        importance=enrichment.importance if enrichment else None,
    )


async def inbound_source_id(session: AsyncSession) -> int:
    """The `inbound_webhook` source row, created on first use (idempotent under concurrency)."""
    await session.execute(
        insert(Source)
        .values(name=INBOUND_SOURCE_NAME, type=SourceType.inbound_webhook, url=INBOUND_SOURCE_URL)
        .on_conflict_do_nothing(index_elements=[Source.type, Source.url])
    )
    source_id: int = (
        await session.execute(
            select(Source.id).where(Source.type == SourceType.inbound_webhook, Source.url == INBOUND_SOURCE_URL)
        )
    ).scalar_one()
    return source_id


async def ingest_inbound_signal(
    session: AsyncSession, blobs: BlobStore, payload: InboundSignal, raw_body: bytes, excerpt_chars: int = 500
) -> InboundAccepted:
    """Store an already-verified inbound payload: raw body to S3, then a Signal row.

    Competitor names in the payload must be tracked competitors; aliases found in the title or text
    are added, as for polled sources. Status follows the polled-source rules: `new` normally,
    `duplicate` if the same content is already stored, `filtered_out` if no competitor matches.
    The same URL posted twice returns the first signal with `created=false`.
    """
    explicit: set[str] = set()
    for name in payload.competitors:
        try:
            explicit.add((await resolve_competitor(session, name)).name)
        except NotFoundError as exc:
            raise InvalidRequestError(f"unknown competitor {name!r}") from exc

    title = normalize_text(payload.title)
    text = normalize_text(payload.text) or title
    url = str(payload.url)
    chash = content_hash(title, text)
    fetched_at = datetime.now(UTC)
    key = raw_key(sha256_hex(raw_body), fetched_at)
    await blobs.put_if_absent(key, raw_body, "application/json")
    await blobs.put_if_absent(text_key(chash), text.encode(), "text/plain; charset=utf-8")

    aliases = {c.name: c.aliases for c in (await session.execute(select(Competitor))).scalars()}
    matched = sorted(explicit | set(CompetitorMatcher(aliases).match(title, text)))
    duplicate = (
        await session.execute(select(Signal.id).where(Signal.content_hash == chash).limit(1))
    ).scalar_one_or_none()
    if duplicate is not None:
        status = SignalStatus.duplicate
    elif not matched:
        status = SignalStatus.filtered_out
    else:
        status = SignalStatus.new

    source_id = await inbound_source_id(session)
    curl = canonical_url(url)
    inserted = (
        await session.execute(
            insert(Signal)
            .values(
                source_id=source_id,
                url=url,
                canonical_url=curl,
                title=title,
                published_at=payload.published_at,
                fetched_at=fetched_at,
                content_hash=chash,
                s3_key=key,
                excerpt=excerpt(text, excerpt_chars),
                matched_competitors=matched,
                status=status,
                filter_reason="no_competitor_match" if status is SignalStatus.filtered_out else None,
                duplicate_of_id=duplicate,
            )
            .on_conflict_do_nothing(index_elements=[Signal.source_id, Signal.canonical_url])
            .returning(Signal.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    if inserted is not None:
        return InboundAccepted(signal_id=inserted, created=True, s3_key=key, status=status.value)
    existing = (
        await session.execute(select(Signal).where(Signal.source_id == source_id, Signal.canonical_url == curl))
    ).scalar_one()
    return InboundAccepted(signal_id=existing.id, created=False, s3_key=existing.s3_key, status=existing.status.value)
