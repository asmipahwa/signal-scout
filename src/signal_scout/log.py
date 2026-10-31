"""Structured JSON logging shared by every process.

Each log line is one JSON object, so logs can be filtered by `run_id` or `job_type` (bound by
`signal_scout.jobruns`) instead of grepped. A redaction processor masks secret-looking keys and
credentials embedded in URLs as a last line of defence; code should still never log secrets.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

_SECRET_KEY = re.compile(r"(password|passwd|secret|api_key|apikey|authorization|credential|access_key)", re.I)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I)
REDACTED = "[redacted]"


def redact_text(value: str) -> str:
    """Remove `user:password@` from any URLs inside a string."""
    return _URL_CREDENTIALS.sub(r"\g<scheme>" + REDACTED + "@", value)


def redact_secrets(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor: mask values whose key looks secret, and credentials inside URLs."""
    for key, value in list(event_dict.items()):
        if _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = redact_text(value)
    return event_dict


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure structlog (and stdlib logging) to write JSON lines to stdout.

    Pass `stream=sys.stderr` for processes whose stdout is a protocol channel (the MCP stdio server).
    """
    out = sys.stdout if stream is None else stream
    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=out, level=log_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
