"""Per-identifier login attempt throttling.

`/auth/login` forwards a credential to Active Directory, which means an
unthrottled endpoint is a remote way to trip the **domain** lockout counter on
every account in the company. That is why this exists, and why the limit applies
to local logins too: an unthrottled password endpoint was never fine, it was
merely less dangerous. Set `LOGIN_MAX_ATTEMPTS` below the domain's own lockout
threshold so we trip first, on one identity, instead of AD tripping for real.

**Counters are per PROCESS.** With N uvicorn workers the effective limit is N x
`max_attempts`, because there is no shared store — pick the setting accordingly.
A durable, shared counter (Postgres or Redis) is the upgrade path if the gateway
is ever scaled out; it is deliberately not built yet, because an in-process limit
that exists beats a distributed one that is still being designed.

Two design points worth keeping:

- **A success clears the tally.** Without that, someone who mistypes twice a day
  eventually locks themselves out of an account whose password they know.
- **Eviction prefers UNLOCKED entries.** The map is hard-bounded so the throttle
  cannot itself become a memory-exhaustion vector, but if a flood of distinct
  identifiers could evict a locked one, the flood would be a way to CLEAR a
  lockout — turning the defence into the bypass.

This module holds no HTTP concepts: it answers "how many seconds until this
identifier may try again", and the router turns that into 429 + `Retry-After`.
"""

import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from ..config import get_settings

# Above this many tracked identifiers, the least-recently-active unlocked entry
# is dropped. Far above any real login volume; it is a backstop, not a policy.
DEFAULT_MAX_TRACKED = 10_000


@dataclass
class _Entry:
    """Recent failures for one identifier, and its lockout if it has one."""

    failures: list[float] = field(default_factory=list)
    locked_until: float | None = None


class LoginThrottle:
    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._max_tracked = max_tracked
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._next_purge = 0.0

    # -- public API --------------------------------------------------------

    def retry_after(self, identifier: str) -> int | None:
        """Seconds until this identifier may try again, or None if it may now."""
        entry = self._entries.get(self._key(identifier))
        if entry is None or entry.locked_until is None:
            return None

        now = self._clock()
        if now >= entry.locked_until:
            entry.locked_until = None
            return None
        # Never 0: a Retry-After of 0 tells a client to retry immediately.
        return max(1, math.ceil(entry.locked_until - now))

    def record_failure(self, identifier: str) -> None:
        """Count a rejected credential, and lock out once the limit is reached."""
        now = self._clock()
        self._maybe_purge(now)

        key = self._key(identifier)
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry()
        self._entries[key] = entry
        self._entries.move_to_end(key)

        cutoff = now - self.window_seconds
        entry.failures = [t for t in entry.failures if t > cutoff]
        entry.failures.append(now)

        if len(entry.failures) >= self.max_attempts:
            entry.locked_until = now + self.lockout_seconds
            # The lockout supersedes the tally, so an expiring window cannot
            # shorten it and a long window cannot immediately re-lock.
            entry.failures = []

        self._enforce_bound()

    def reset(self, identifier: str) -> None:
        """A successful login forgets the identifier's failures entirely."""
        self._entries.pop(self._key(identifier), None)

    def __len__(self) -> int:
        return len(self._entries)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _key(identifier: str) -> str:
        # One identity, one counter: mixed case must not buy extra attempts.
        return identifier.strip().casefold()

    def _is_dead(self, entry: _Entry, now: float) -> bool:
        if entry.locked_until is not None and now < entry.locked_until:
            return False
        cutoff = now - self.window_seconds
        return not any(t > cutoff for t in entry.failures)

    def _maybe_purge(self, now: float) -> None:
        """Drop fully-expired entries, at most once per window."""
        if now < self._next_purge:
            return
        self._next_purge = now + self.window_seconds
        dead = [k for k, e in self._entries.items() if self._is_dead(e, now)]
        for key in dead:
            del self._entries[key]

    def _enforce_bound(self) -> None:
        """Hard cap, evicting UNLOCKED entries first — see the module docstring."""
        if len(self._entries) <= self._max_tracked:
            return
        now = self._clock()
        while len(self._entries) > self._max_tracked:
            victim = next(
                (
                    k
                    for k, e in self._entries.items()
                    if e.locked_until is None or now >= e.locked_until
                ),
                None,
            )
            if victim is None:
                # Everything tracked is actively locked out. Drop the oldest to
                # keep the bound; the alternative is unbounded growth.
                victim = next(iter(self._entries))
            del self._entries[victim]


@lru_cache
def get_throttle() -> LoginThrottle:
    """The process-wide throttle the login route uses."""
    settings = get_settings()
    return LoginThrottle(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_attempt_window_seconds,
        lockout_seconds=settings.login_lockout_seconds,
    )
