"""`ck_users_credential` and `ck_users_auth_provider`, asserted in Postgres.

These constraints are the reason the "one identity, one credential store" rule is
a guarantee rather than a convention. The login route already dispatches on
`auth_provider`; the constraints make the illegal states unrepresentable, so no
future code path — a password-reset feature, a bulk import, a migration script —
can hand a directory user a local password they could fall back on after their AD
account is disabled.

Real Postgres; skips if it is unreachable.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

PLACEHOLDER_HASH = "x" * 60


def _sql(fn):
    async def run():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _insert(provider, password_hash):
    email = f"ck-{uuid.uuid4().hex[:8]}@example.com"

    def make(conn):
        return conn.execute(
            text(
                "INSERT INTO users (email, auth_provider, password_hash, role, is_active)"
                " VALUES (:e, :p, :h, 'member', true)"
            ),
            {"e": email, "p": provider, "h": password_hash},
        )

    _sql(make)
    return email


def _delete(email):
    _sql(lambda c: c.execute(text("DELETE FROM users WHERE email = :e"), {"e": email}))


# --------------------------------------------------------------------------
# Legal rows
# --------------------------------------------------------------------------

def test_a_local_user_with_a_hash_is_allowed():
    _skip_if_no_db()
    email = _insert("local", PLACEHOLDER_HASH)
    try:
        assert email
    finally:
        _delete(email)


def test_a_directory_user_without_a_hash_is_allowed():
    _skip_if_no_db()
    email = _insert("ad", None)
    try:
        assert email
    finally:
        _delete(email)


# --------------------------------------------------------------------------
# Illegal rows
# --------------------------------------------------------------------------

def test_a_directory_user_cannot_hold_a_password_hash():
    """The hole this constraint closes: a second way in that outlives AD."""
    _skip_if_no_db()
    with pytest.raises(IntegrityError) as err:
        _insert("ad", PLACEHOLDER_HASH)
    assert "ck_users_credential" in str(err.value)


def test_a_local_user_cannot_exist_without_a_password_hash():
    """Such a user could never log in — an invalid row, not a locked account."""
    _skip_if_no_db()
    with pytest.raises(IntegrityError) as err:
        _insert("local", None)
    assert "ck_users_credential" in str(err.value)


@pytest.mark.parametrize("provider", ["google", "microsoft", "", "LOCAL", "AD"])
def test_the_provider_vocabulary_is_closed(provider):
    """The login route branches on this column, so an unrecognised value would
    decide which credential store is consulted. Adding a provider means editing
    the CHECK — the same rule as ck_documents_status."""
    _skip_if_no_db()
    with pytest.raises(IntegrityError) as err:
        _insert(provider, None)
    assert "ck_users_" in str(err.value)
