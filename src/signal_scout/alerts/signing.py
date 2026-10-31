"""HMAC-SHA256 signatures for webhooks, in both directions.

Outbound: every delivery carries `X-SignalScout-Signature: t=<unix>,v1=<hex>` where the signature
covers `<timestamp>.<raw body>`, so receivers can prove the payload came from us and wasn't altered.

Inbound: external tools pushing signals to us must sign the same way. We verify with a constant-time
comparison and reject timestamps older than 5 minutes, so a captured request can't be replayed later.
This is the same scheme Stripe and Slack use, which makes it familiar to integrators.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-SignalScout-Signature"
IDEMPOTENCY_HEADER = "X-SignalScout-Delivery"
DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """The request's signature is missing, malformed, wrong, or too old."""


def _digest(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Return the signature header value for a body."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={_digest(secret, ts, body)}"


def verify(
    secret: str, body: bytes, header: str | None, tolerance: int = DEFAULT_TOLERANCE_SECONDS, now: int | None = None
) -> None:
    """Raise SignatureError unless `header` is a valid, fresh signature of `body`."""
    if not header:
        raise SignatureError("missing signature header")
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        provided = parts["v1"]
    except (ValueError, KeyError) as exc:
        raise SignatureError("malformed signature header") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        raise SignatureError("timestamp outside tolerance (possible replay)")
    if not hmac.compare_digest(_digest(secret, timestamp, body), provided):
        raise SignatureError("signature mismatch")
