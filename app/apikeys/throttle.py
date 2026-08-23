"""Two different limits, deliberately not one thing.

  * `RateLimiter` is a token bucket on SUCCESSFUL use: it stops one key
    monopolising the OCR capacity. Answer: 429.
  * `LoginThrottle` (reused wholesale from `app/auth/throttle.py`) counts
    credential FAILURES and locks a prefix out, for exactly the reason
    `/auth/login` is throttled: an unthrottled credential endpoint is a
    brute-force surface.

Both counters are PER PROCESS. N uvicorn workers means N x the limit — the same
documented caveat as the login throttle. That is acceptable for capacity
protection and would not be for a billing quota; if this ever becomes a billing
quota it needs Postgres or Redis, not a bigger comment.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ..auth.throttle import LoginThrottle
from ..config import get_settings

__all__ = ["RateLimiter", "get_rate_limiter", "get_auth_throttle"]

DEFAULT_MAX_TRACKED = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A per-identifier token bucket. `check` consumes a token when it allows."""

    def __init__(
        self,
        *,
        per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        self.per_minute = per_minute
        self.burst = burst
        self._clock = clock
        self._max_tracked = max_tracked
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(self, identifier: str) -> int | None:
        """Seconds to wait, or None if the call may proceed.

        Consumes a token when allowing, so this both tests and consumes.
        It's called once per request, which is why the side effect is intentional.
        """
        # Fail closed: a misconfigured 0 means "none allowed", not "unlimited".
        if self.per_minute <= 0 or self.burst <= 0:
            return 60

        now = self._clock()
        bucket = self._buckets.get(identifier)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), updated=now)
        self._buckets[identifier] = bucket
        self._buckets.move_to_end(identifier)

        rate = self.per_minute / 60.0
        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            self._enforce_bound()
            return None

        needed = (1.0 - bucket.tokens) / rate
        self._enforce_bound()
        # Never 0: a Retry-After of 0 tells a client to retry immediately.
        return max(1, math.ceil(needed))

    def __len__(self) -> int:
        return len(self._buckets)

    def _enforce_bound(self) -> None:
        # Evict the least recently touched. Unlike the login throttle there is
        # no lockout state to protect here, so plain LRU is correct: an evicted
        # bucket refills to full, which is generous, not a security hole.
        while len(self._buckets) > self._max_tracked:
            self._buckets.popitem(last=False)


_rate_limiter: RateLimiter | None = None
_auth_throttle: LoginThrottle | None = None


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        settings = get_settings()
        _rate_limiter = RateLimiter(
            per_minute=settings.ocr_rate_per_minute,
            burst=settings.ocr_rate_burst,
        )
    return _rate_limiter


def get_auth_throttle() -> LoginThrottle:
    """Lockout on repeated bad keys, keyed on the presented PREFIX.

    Reuses the login throttle unchanged, which brings its eviction rule with it:
    eviction PREFERS UNLOCKED entries, so a flood of junk prefixes cannot evict
    a locked one and thereby clear a lockout.
    """
    global _auth_throttle
    if _auth_throttle is None:
        settings = get_settings()
        _auth_throttle = LoginThrottle(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_attempt_window_seconds,
            lockout_seconds=settings.login_lockout_seconds,
        )
    return _auth_throttle
