"""Integration tests for the api_keys tables and admin routes.

Builds a throwaway NullPool engine per call rather than using the app's
module-level `engine`: that one pools connections bound to the first event loop,
and each `asyncio.run` creates a new one, so the second test in the file would
die with "Event loop is closed". Same rule as the RAG integration tests.
"""

import asyncio
import contextlib
import os
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.apikeys import keygen
from app.apikeys.models import ApiKey

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")


def _run(coro_fn):
    async def main():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _an_admin_id(session):
    from sqlalchemy import select

    from app.users.models import User

    row = await session.scalar(select(User.id).where(User.role == "admin").limit(1))
    assert row is not None, "seed an admin first (admin@example.com)"
    return row


def test_a_revoked_key_must_record_when_it_was_revoked():
    """ck_api_keys_revoked: is_active=false with no revoked_at is illegal.

    The half-revoked state is unrepresentable on purpose — 'inactive since
    when?' has no answer, and 'revoked but still active' would still serve.
    """

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        session.add(
            ApiKey(
                id=uuid.uuid4().hex,
                name="ck-test",
                key_prefix=minted.prefix,
                key_hash=minted.key_hash,
                scopes=["ocr:read"],
                is_active=False,
                revoked_at=None,          # <- the illegal combination
                created_by_user_id=admin_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


def test_an_unknown_scope_cannot_be_stored():
    """ck_api_keys_scopes closes the vocabulary, like ck_documents_status."""

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        session.add(
            ApiKey(
                id=uuid.uuid4().hex,
                name="scope-test",
                key_prefix=minted.prefix,
                key_hash=minted.key_hash,
                scopes=["ocr:reed"],      # typo
                created_by_user_id=admin_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


def test_two_keys_cannot_share_a_prefix():
    """The prefix is the lookup key, so a collision makes verification
    ambiguous. UNIQUE is functional here, not tidiness."""

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        for _ in range(2):
            session.add(
                ApiKey(
                    id=uuid.uuid4().hex,
                    name="dup-prefix",
                    key_prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    scopes=["ocr:read"],
                    created_by_user_id=admin_id,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


# --- admin route tests ---------------------------------------------------

PASSWORD = "supersecret123"
ADMIN_EMAIL = "admin@example.com"


@contextlib.contextmanager
def _client():
    """A TestClient with the external API switched ON.

    The switch is read at import time by main.py, so it must be set in the
    environment BEFORE the app module is imported, and the settings cache
    cleared.

    Yielded rather than returned bare, and callers must use it as
    `with _client() as client:`. This app talks to Postgres through the
    module-level POOLED engine in `app/db/session.py`. `TestClient` only pins
    every request it makes to ONE event loop while it is entered as a context
    manager (`__enter__`/`__exit__`); used bare, each individual request spins
    up and tears down its OWN throwaway loop (see
    `starlette.testclient.TestClient._portal_factory`). The very next request
    then tries to check out a connection the pool created on a now-dead loop,
    and `pool_pre_ping` crashes with "attached to a different loop" — not a
    flake, a guaranteed failure on the second DB-touching request in the
    whole run. Entering as a context manager also runs the app's lifespan on
    exit (`engine.dispose()`), which is what lets the NEXT test's fresh loop
    reuse the same module-level engine safely instead of finding a stale
    connection bound to a loop that no longer exists. This is the same
    constraint the module docstring above already documents for the
    hand-rolled `_run` helper, met here with the tool this file didn't have
    before: a `with` block instead of a second engine.
    """
    from fastapi.testclient import TestClient

    os.environ["EXTERNAL_API_ENABLED"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    import importlib

    import app.main

    importlib.reload(app.main)
    with TestClient(app.main.app) as client:
        yield client


def _admin_token(client):
    resp = client.post(
        "/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    if resp.status_code != 200:
        pytest.skip(f"cannot log in as {ADMIN_EMAIL} ({resp.status_code})")
    return resp.json()["access_token"]


def test_minting_returns_the_plaintext_once_and_never_again():
    with _client() as client:
        headers = {"Authorization": f"Bearer {_admin_token(client)}"}

        created = client.post(
            "/v1/api-keys", json={"name": "test-mint"}, headers=headers
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["key"].startswith("lgw_live_")
        assert body["scopes"] == ["ocr:read"]

        listed = client.get("/v1/api-keys", headers=headers)
        assert listed.status_code == 200
        row = next(k for k in listed.json() if k["id"] == body["id"])
        # Asserted on the serialised JSON, not the model: a field added to the
        # model would leak through response_model only if it is also in the
        # schema, and this is the assertion that would catch it.
        assert "key" not in row
        assert "key_hash" not in row

        client.delete(f"/v1/api-keys/{body['id']}", headers=headers)


def test_an_unknown_scope_is_a_loud_422_not_a_silent_drop():
    with _client() as client:
        headers = {"Authorization": f"Bearer {_admin_token(client)}"}
        resp = client.post(
            "/v1/api-keys",
            json={"name": "bad-scope", "scopes": ["ocr:reed"]},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "ocr:reed" in resp.text


def test_an_unexpected_field_is_rejected_rather_than_ignored():
    with _client() as client:
        headers = {"Authorization": f"Bearer {_admin_token(client)}"}
        resp = client.post(
            "/v1/api-keys",
            json={"name": "sneaky", "is_active": True},
            headers=headers,
        )
        assert resp.status_code == 422


def test_a_non_admin_cannot_mint_a_key():
    with _client() as client:
        resp = client.post("/v1/api-keys", json={"name": "nope"})
        assert resp.status_code in (401, 403)


def test_revoking_twice_is_a_404_the_second_time():
    with _client() as client:
        headers = {"Authorization": f"Bearer {_admin_token(client)}"}
        key_id = client.post(
            "/v1/api-keys", json={"name": "revoke-twice"}, headers=headers
        ).json()["id"]
        assert (
            client.delete(f"/v1/api-keys/{key_id}", headers=headers).status_code
            == 204
        )
        assert (
            client.delete(f"/v1/api-keys/{key_id}", headers=headers).status_code
            == 404
        )


def test_a_revoked_key_is_still_listed_so_its_history_stays_attributable():
    with _client() as client:
        headers = {"Authorization": f"Bearer {_admin_token(client)}"}
        key_id = client.post(
            "/v1/api-keys", json={"name": "kept-after-revoke"}, headers=headers
        ).json()["id"]
        client.delete(f"/v1/api-keys/{key_id}", headers=headers)
        row = next(
            k
            for k in client.get("/v1/api-keys", headers=headers).json()
            if k["id"] == key_id
        )
        assert row["is_active"] is False
