"""FastAPI dependencies: the per-app ServiceContext, a per-request DB session, and API-key auth.

Reads are public; every write endpoint depends on `require_api_key`, which compares the
`X-API-Key` header to API_KEY in constant time. With no API_KEY configured, writes are refused
(503) rather than silently left open.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.context import ServiceContext

API_KEY_HEADER = "X-API-Key"
_api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, description="Required on write endpoints.")


def get_ctx(request: Request) -> ServiceContext:
    ctx: ServiceContext = request.app.state.ctx
    return ctx


async def get_session(ctx: Annotated[ServiceContext, Depends(get_ctx)]) -> AsyncIterator[AsyncSession]:
    async with ctx.sessions() as session:
        yield session


async def require_api_key(
    ctx: Annotated[ServiceContext, Depends(get_ctx)],
    provided: Annotated[str | None, Depends(_api_key_header)],
) -> None:
    expected = ctx.settings.api_key.get_secret_value()
    if not expected:
        raise HTTPException(503, "write endpoints are disabled: API_KEY is not configured")
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(401, f"missing or invalid {API_KEY_HEADER} header", headers={"WWW-Authenticate": "ApiKey"})


Ctx = Annotated[ServiceContext, Depends(get_ctx)]
Session = Annotated[AsyncSession, Depends(get_session)]
