"""Asyncio-safe sliding-window rate limiter."""

import asyncio
import time


class _RateLimiter:
    """Sliding-window rate limiter with asyncio lock for concurrency safety."""

    def __init__(self, max_per_minute: int = 50):
        self._max = max_per_minute
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def check(self) -> bool:
        """Return True if a request is allowed, False if rate-limited."""
        async with self._lock:
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < 60]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True

    @property
    def remaining(self) -> int:
        now = time.monotonic()
        active = [t for t in self._timestamps if now - t < 60]
        return max(0, self._max - len(active))
