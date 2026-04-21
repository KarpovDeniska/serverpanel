"""In-memory sliding-window rate limiter for login / register.

Keyed by (identifier, action). Good enough for a single-process deployment;
for multi-worker setups, replace with Redis or similar.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, window_seconds: int, max_events: int) -> None:
        self.window = window_seconds
        self.max_events = max_events
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        """Return True if under the limit and record the event. False if throttled."""
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[key]
            # Drop expired
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.max_events:
                return False
            bucket.append(now)
            return True


login_limiter = RateLimiter(window_seconds=300, max_events=10)  # 10 per 5 min
register_limiter = RateLimiter(window_seconds=3600, max_events=5)  # 5 per hour
