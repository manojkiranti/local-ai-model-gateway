"""Pure tests for the API-key decision. Every gate must fail CLOSED.

Same rule as `app/rag/permissions.py`: an unknown or absent input must be
refused, never allowed by falling through a comparison.
"""

from datetime import datetime, timedelta, timezone

from app.apikeys import policy

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _facts(**over):
    base = dict(is_active=True, expires_at=None, scopes=("ocr:read",))
    base.update(over)
    return policy.KeyFacts(**base)


def test_an_active_unexpired_key_is_usable():
    assert policy.is_usable(_facts(), now=NOW) is True


def test_a_missing_key_is_not_usable():
    """None means 'no row matched that prefix'. It must never be truthy."""
    assert policy.is_usable(None, now=NOW) is False


def test_a_revoked_key_is_not_usable():
    assert policy.is_usable(_facts(is_active=False), now=NOW) is False


def test_an_expired_key_is_not_usable():
    past = NOW - timedelta(seconds=1)
    assert policy.is_usable(_facts(expires_at=past), now=NOW) is False


def test_a_key_expiring_in_the_future_is_usable():
    assert policy.is_usable(_facts(expires_at=NOW + timedelta(days=1)), now=NOW) is True


def test_expiry_exactly_now_is_expired():
    """A boundary decided by the operator, written down so it cannot drift."""
    assert policy.is_usable(_facts(expires_at=NOW), now=NOW) is False


def test_a_naive_expiry_is_treated_as_utc_not_crashed():
    naive = datetime(2026, 8, 22, 12, 0)
    assert policy.is_usable(_facts(expires_at=naive), now=NOW) is False


def test_the_required_scope_must_be_present():
    assert policy.scope_refusal(_facts(), required="ocr:read") is None


def test_an_empty_scope_set_is_refused():
    assert policy.scope_refusal(_facts(scopes=()), required="ocr:read") is not None


def test_a_key_with_a_different_scope_is_refused():
    refusal = policy.scope_refusal(_facts(scopes=("other:thing",)), required="ocr:read")
    assert refusal is not None
    assert "ocr:read" in refusal


def test_an_unknown_scope_string_never_satisfies_anything():
    """A value that escaped ck_api_keys_scopes must not compare as satisfied."""
    assert policy.scope_refusal(_facts(scopes=("ocr:reed",)), required="ocr:read")
    assert policy.scope_refusal(_facts(scopes=("*",)), required="ocr:read")
    assert policy.scope_refusal(_facts(scopes=("ocr:read ",)), required="ocr:read")


def test_the_401_detail_is_one_message_for_every_cause():
    """Distinguishing 'unknown key' from 'wrong secret' tells an attacker which
    prefixes are real; 'expired' tells them a valid key existed."""
    assert policy.INVALID_KEY == "Invalid API key"


def test_the_scope_vocabulary_is_closed_and_matches_the_db_check():
    assert policy.ALL_SCOPES == frozenset({"ocr:read"})
