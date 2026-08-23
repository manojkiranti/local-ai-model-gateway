"""Pure tests for `ApiKeyCreate.expires_at`. No DB needed — pydantic only."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.apikeys.schemas import ApiKeyCreate


def test_a_naive_future_expiry_is_normalised_to_utc():
    """A naive datetime would otherwise be interpreted in the SERVER's
    timezone on the way into Postgres — measured, a naive
    2027-01-01T00:00:00 stores as 2026-12-31T18:15:00+00 behind a +05:45
    server. Normalising at the schema boundary means every caller of this
    schema gets the same behaviour regardless of server timezone."""
    naive = datetime.now() + timedelta(days=365)
    naive = naive.replace(tzinfo=None)
    created = ApiKeyCreate(name="k", expires_at=naive)
    assert created.expires_at.tzinfo is not None
    assert created.expires_at.utcoffset() == timedelta(0)
    assert created.expires_at == naive.replace(tzinfo=timezone.utc)


def test_an_aware_future_expiry_passes_through():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    created = ApiKeyCreate(name="k", expires_at=future)
    assert created.expires_at == future


def test_a_past_expiry_is_rejected():
    """Without this, an admin can mint a 201 for a key that 401s forever and
    gets no signal anything is wrong."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    with pytest.raises(ValidationError):
        ApiKeyCreate(name="k", expires_at=past)


def test_a_naive_past_expiry_is_also_rejected():
    naive_past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    with pytest.raises(ValidationError):
        ApiKeyCreate(name="k", expires_at=naive_past)


def test_expiry_exactly_now_is_rejected():
    """Same boundary as policy.is_usable: expiring exactly at now is expired."""
    with pytest.raises(ValidationError):
        ApiKeyCreate(name="k", expires_at=datetime.now(timezone.utc))


def test_no_expiry_is_fine():
    created = ApiKeyCreate(name="k")
    assert created.expires_at is None
