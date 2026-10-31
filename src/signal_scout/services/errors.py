"""Errors the services raise, independent of transport.

The API maps them to problem+json responses and the MCP server to tool errors, so neither has to
inspect SQLAlchemy or Elasticsearch exceptions.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class: a request the services refused, with a message safe to show to the caller."""

    status = 400
    title = "Bad request"
    slug = "bad-request"


class NotFoundError(ServiceError):
    """The requested entity does not exist."""

    status = 404
    title = "Not found"
    slug = "not-found"


class InvalidRequestError(ServiceError):
    """The request is well-formed but semantically invalid (bad cursor, unknown competitor, ...)."""

    status = 422
    title = "Invalid request"
    slug = "invalid-request"


class DependencyError(ServiceError):
    """A backing service (Elasticsearch, S3) failed; the request may succeed if retried."""

    status = 503
    title = "Dependency unavailable"
    slug = "dependency-unavailable"
