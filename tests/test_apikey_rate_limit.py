"""Pure tests for the per-key rate limiter. Injected clock, no sleeping."""

from app.apikeys.throttle import RateLimiter


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _limiter(per_minute=60, burst=5):
    clock = _Clock()
    return RateLimiter(per_minute=per_minute, burst=burst, clock=clock), clock


def test_the_first_call_is_allowed():
    limiter, _ = _limiter()
    assert limiter.check("k1") is None


def test_the_burst_is_spent_then_refused():
    limiter, _ = _limiter(per_minute=60, burst=3)
    assert [limiter.check("k1") for _ in range(3)] == [None, None, None]
    assert limiter.check("k1") is not None


def test_a_refusal_reports_seconds_never_zero():
    """Retry-After: 0 tells a client to retry immediately, which is a loop."""
    limiter, _ = _limiter(per_minute=60, burst=1)
    limiter.check("k1")
    assert limiter.check("k1") >= 1


def test_tokens_refill_over_time():
    limiter, clock = _limiter(per_minute=60, burst=2)
    limiter.check("k1")
    limiter.check("k1")
    assert limiter.check("k1") is not None
    clock.advance(1.0)          # 60/min = 1 per second
    assert limiter.check("k1") is None


def test_refill_never_exceeds_the_burst():
    """An hour of idling must not bank an hour of tokens.

    The clock advance has to happen AFTER the bucket exists: a fresh bucket is
    built with updated=now, so elapsed is 0 on its first refill and an advance
    before creation is silently discarded — which is how the original version of
    this test passed with the clamp deleted.
    """
    limiter, clock = _limiter(per_minute=60, burst=2)
    limiter.check("k1")            # creates the bucket and spends one token -> 1 left
    clock.advance(3600)            # unclamped this would bank ~3600 tokens
    assert limiter.check("k1") is None      # 1st of the clamped burst
    assert limiter.check("k1") is None      # 2nd -> bucket empty
    assert limiter.check("k1") is not None  # refused: refill was capped at burst=2


def test_keys_are_limited_independently():
    limiter, _ = _limiter(per_minute=60, burst=1)
    assert limiter.check("k1") is None
    assert limiter.check("k2") is None


def test_a_zero_per_minute_limit_refuses_everything():
    """Fail closed: a misconfigured 0 must not mean 'unlimited'."""
    limiter, _ = _limiter(per_minute=0, burst=0)
    assert limiter.check("k1") is not None


def test_tracking_is_bounded_so_a_flood_cannot_exhaust_memory():
    limiter = RateLimiter(per_minute=60, burst=1, max_tracked=50)
    for i in range(500):
        limiter.check(f"k{i}")
    assert len(limiter) <= 50
