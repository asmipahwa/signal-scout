"""Opaque keyset cursors for the ranked insight feed.

The feed is ordered by (score DESC, id DESC). A cursor stores the last row's pair, so the next page
is `WHERE (score, id) < (last_score, last_id)`: stable under concurrent inserts and O(page) at any
depth, unlike OFFSET. It is base64url JSON so clients treat it as opaque but we can still debug it.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import dataclass

from .errors import InvalidRequestError


@dataclass(frozen=True)
class FeedCursor:
    """Position after the last row returned: that row's score and id."""

    score: float
    id: int


def encode_cursor(cursor: FeedCursor) -> str:
    raw = json.dumps({"s": cursor.score, "i": cursor.id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> FeedCursor:
    """Parse a cursor made by `encode_cursor`; raise InvalidRequestError for anything else."""
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        score, row_id = data["s"], data["i"]
    except (binascii.Error, ValueError, UnicodeDecodeError, TypeError, KeyError) as exc:
        raise InvalidRequestError("invalid cursor") from exc
    if (
        isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(score)
        or isinstance(row_id, bool)
        or not isinstance(row_id, int)
    ):
        raise InvalidRequestError("invalid cursor")
    return FeedCursor(score=float(score), id=row_id)
