"""RFC 9457 problem details for every 4xx/5xx response.

One error shape (`application/problem+json` with type/title/status/detail) for validation errors,
auth failures, missing entities and crashes, so clients write one error handler. A 500 never leaks
the exception text; it is logged instead.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..log import get_logger
from ..services.errors import ServiceError

log = get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "urn:signal-scout:problem:"


class Problem(BaseModel):
    """An RFC 9457 problem document. `errors` lists field errors for validation problems."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[dict[str, Any]] | None = None


class ProblemResponse(JSONResponse):
    """JSONResponse with the problem+json media type."""

    media_type = PROBLEM_MEDIA_TYPE


def problem(
    status: int,
    detail: str | None = None,
    *,
    slug: str | None = None,
    title: str | None = None,
    instance: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> ProblemResponse:
    """Build a problem+json response. Without `slug` the type is `about:blank` and the title is the status phrase."""
    body = Problem(
        type=f"{PROBLEM_TYPE_BASE}{slug}" if slug else "about:blank",
        title=title or HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=instance,
        errors=errors,
    )
    return ProblemResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)


def _clean_errors(errors: Any) -> list[dict[str, Any]]:
    """Keep loc/msg/type only: the raw `input` may echo secrets or huge bodies back."""
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in errors]


# Documented on every route so OpenAPI shows the error shape.
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": Problem, "content": {PROBLEM_MEDIA_TYPE: {}}, "description": HTTPStatus(status).phrase}
    for status in (400, 401, 404, 422, 500, 503)
}


def install_problem_handlers(app: FastAPI) -> None:
    """Route every error path through `problem()`."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> ProblemResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem(exc.status_code, detail, instance=request.url.path, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError) -> ProblemResponse:
        return problem(
            422,
            "request parameters or body failed validation",
            slug="validation-error",
            title="Validation error",
            instance=request.url.path,
            errors=_clean_errors(exc.errors()),
        )

    @app.exception_handler(ValidationError)
    async def body_validation_error(request: Request, exc: ValidationError) -> ProblemResponse:
        return problem(
            422,
            "request body failed validation",
            slug="validation-error",
            title="Validation error",
            instance=request.url.path,
            errors=_clean_errors(exc.errors()),
        )

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> ProblemResponse:
        return problem(exc.status, str(exc), slug=exc.slug, title=exc.title, instance=request.url.path)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> ProblemResponse:
        log.error("unhandled_error", path=request.url.path, error=f"{type(exc).__name__}: {exc}")
        return problem(500, "unexpected server error", instance=request.url.path)
