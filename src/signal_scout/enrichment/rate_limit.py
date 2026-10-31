"""Client-side rate limiting so we stay inside a provider's free tier.

Two limits matter for Gemini's free tier: requests per minute (RPM) and requests per day (RPD).
RPM is enforced with a sliding window (we wait). RPD is enforced with a counter that is persisted
to disk, so restarting a multi-day backfill doesn't forget what it already used today; when the
day's budget is gone we raise instead of waiting, and the backfill resumes after the reset.

Gemini's daily quota resets at midnight US Pacific time, so "today" is computed in that zone.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import LlmQuotaExhaustedError

RESET_TZ = ZoneInfo("America/Los_Angeles")


class RateLimiter:
    def __init__(self, rpm: int, rpd: int | None = None, state_path: Path | None = None) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.rpm = rpm
        self.rpd = rpd
        self.state_path = state_path
        self._window: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._day, self._count = self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(RESET_TZ).date().isoformat()

    def _load(self) -> tuple[str, int]:
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state.get("day") == self._today():
                return state["day"], int(state["count"])
        return self._today(), 0

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"day": self._day, "count": self._count}))

    @property
    def used_today(self) -> int:
        return self._count if self._day == self._today() else 0

    async def acquire(self) -> None:
        async with self._lock:
            if self._day != self._today():
                self._day, self._count = self._today(), 0
            if self.rpd is not None and self._count >= self.rpd:
                raise LlmQuotaExhaustedError(f"used {self._count}/{self.rpd} requests today ({self._day} Pacific)")
            now = time.monotonic()
            while self._window and now - self._window[0] >= 60:
                self._window.popleft()
            if len(self._window) >= self.rpm:
                await asyncio.sleep(60 - (now - self._window[0]))
                self._window.popleft()
            self._window.append(time.monotonic())
            self._count += 1
            self._save()
