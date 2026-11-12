"""Tiny token-bucket limiter (no dependencies, no clock drift surprises)."""

from __future__ import annotations

import time


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/second, burst up to ``capacity``."""

    __slots__ = ("rate", "capacity", "tokens", "updated")

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = max(0.001, float(rate))
        self.capacity = max(1.0, float(capacity))
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False
