# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory token buckets, keyed per token and per profile."""

from __future__ import annotations

import threading
import time

from ankido.config import RateLimit, RateLimits
from ankido.errors import RateLimited


class _Bucket:
    __slots__ = ("capacity", "rate", "tokens", "updated")

    def __init__(self, limit: RateLimit) -> None:
        self.capacity = float(limit.burst)
        self.rate = limit.per_minute / 60.0
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def take(self) -> float:
        """Consume one unit; return 0 if allowed, else seconds until one is available."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate


class RateLimiter:
    def __init__(self, limits: RateLimits) -> None:
        self._limits = limits
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, *, token_id: str, profile: str, klass: str) -> None:
        self._take(klass, f"t:{token_id}", f"p:{profile}")

    def check_key(self, key: str, *, klass: str) -> None:
        """One bucket for an arbitrary key (e.g. a client IP on unauthenticated endpoints)."""
        self._take(klass, f"k:{key}")

    def _take(self, klass: str, *names: str) -> None:
        limit = getattr(self._limits, klass)
        with self._lock:
            wait = 0.0
            for key in ((name, klass) for name in names):
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = self._buckets[key] = _Bucket(limit)
                wait = max(wait, bucket.take())
        if wait > 0:
            raise RateLimited(
                f"rate limit exceeded for {klass} operations",
                details={"retry_after_seconds": round(wait, 2)},
            )
