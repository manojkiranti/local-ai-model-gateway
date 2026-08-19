"""Login attempt throttling.

This exists because of AD. `/auth/login` now forwards a credential to the
domain, so without a limit anyone on the network can trip the AD **lockout
counter** on every account in the company by hammering one HTTP endpoint. The
throttle is applied to local logins too — an unthrottled password endpoint was
never fine, it was just less dangerous.

The clock is injected so none of this sleeps.
"""

import pytest

from app.auth.throttle import LoginThrottle

KEY = "user@example.com"
OTHER = "someone.else@example.com"


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock, *, max_attempts=3, window=300, lockout=900, max_tracked=10_000):
    return LoginThrottle(
        max_attempts=max_attempts,
        window_seconds=window,
        lockout_seconds=lockout,
        clock=clock,
        max_tracked=max_tracked,
    )


def test_a_fresh_key_is_allowed():
    assert _throttle(FakeClock()).retry_after(KEY) is None


def test_under_the_limit_stays_allowed():
    t = _throttle(FakeClock(), max_attempts=3)
    t.record_failure(KEY)
    t.record_failure(KEY)
    assert t.retry_after(KEY) is None


def test_hitting_the_limit_locks_out():
    t = _throttle(FakeClock(), max_attempts=3, lockout=900)
    for _ in range(3):
        t.record_failure(KEY)
    retry = t.retry_after(KEY)
    assert retry is not None
    assert 0 < retry <= 900


def test_a_success_clears_the_counter():
    """Otherwise a user who mistypes twice a day eventually locks themselves out."""
    t = _throttle(FakeClock(), max_attempts=3)
    t.record_failure(KEY)
    t.record_failure(KEY)
    t.reset(KEY)
    t.record_failure(KEY)
    assert t.retry_after(KEY) is None


def test_failures_outside_the_window_do_not_count():
    """A sliding window, not a lifetime tally."""
    clock = FakeClock()
    t = _throttle(clock, max_attempts=3, window=300)
    t.record_failure(KEY)
    t.record_failure(KEY)
    clock.advance(301)
    t.record_failure(KEY)
    assert t.retry_after(KEY) is None


def test_the_lockout_expires():
    clock = FakeClock()
    t = _throttle(clock, max_attempts=3, lockout=900)
    for _ in range(3):
        t.record_failure(KEY)
    assert t.retry_after(KEY) is not None
    clock.advance(901)
    assert t.retry_after(KEY) is None


def test_retry_after_counts_down():
    clock = FakeClock()
    t = _throttle(clock, max_attempts=2, lockout=600)
    t.record_failure(KEY)
    t.record_failure(KEY)
    first = t.retry_after(KEY)
    clock.advance(100)
    second = t.retry_after(KEY)
    assert first is not None and second is not None
    assert second < first


def test_retry_after_is_always_a_positive_whole_number():
    """It becomes a Retry-After header; 0 would tell a client to retry at once."""
    clock = FakeClock()
    t = _throttle(clock, max_attempts=1, lockout=10)
    t.record_failure(KEY)
    clock.advance(9.5)
    retry = t.retry_after(KEY)
    assert isinstance(retry, int)
    assert retry >= 1


def test_keys_are_independent():
    t = _throttle(FakeClock(), max_attempts=2)
    t.record_failure(KEY)
    t.record_failure(KEY)
    assert t.retry_after(KEY) is not None
    assert t.retry_after(OTHER) is None


def test_keys_are_case_insensitive():
    """One identity, one counter — mixed case must not buy extra attempts."""
    t = _throttle(FakeClock(), max_attempts=2)
    t.record_failure("User@Example.com")
    t.record_failure("USER@EXAMPLE.COM")
    assert t.retry_after("user@example.com") is not None


def test_expired_entries_are_evicted():
    """The throttle must not be a memory-exhaustion vector of its own."""
    clock = FakeClock()
    t = _throttle(clock, max_attempts=3, window=300, lockout=900)
    for i in range(500):
        t.record_failure(f"user{i}@example.com")
    assert len(t) == 500
    clock.advance(1000)
    t.record_failure("someone-new@example.com")
    assert len(t) < 500


def test_tracking_is_hard_bounded_even_without_expiry():
    """A flood of distinct identifiers inside one window still cannot grow forever."""
    t = _throttle(FakeClock(), max_attempts=3, max_tracked=100)
    for i in range(1000):
        t.record_failure(f"user{i}@example.com")
    assert len(t) <= 100


def test_a_locked_key_survives_the_bound_it_would_otherwise_be_evicted_by():
    """Eviction under flood must not be a way to CLEAR someone's lockout."""
    clock = FakeClock()
    t = _throttle(clock, max_attempts=2, max_tracked=50)
    t.record_failure(KEY)
    t.record_failure(KEY)
    assert t.retry_after(KEY) is not None
    for i in range(500):
        t.record_failure(f"flood{i}@example.com")
    assert t.retry_after(KEY) is not None


def test_settings_build_the_shared_instance(monkeypatch):
    from app.auth import throttle as mod
    from app.config import get_settings

    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("LOGIN_ATTEMPT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "120")
    get_settings.cache_clear()
    mod.get_throttle.cache_clear()
    try:
        t = mod.get_throttle()
        assert t.max_attempts == 7
        assert t.window_seconds == 60
        assert t.lockout_seconds == 120
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        mod.get_throttle.cache_clear()
