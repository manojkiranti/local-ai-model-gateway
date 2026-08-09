"""/v1/chat department binding. Real Postgres + TestClient; Ollama faked.

Proves the slice-3 contract end to end through the HTTP surface: a new session
binds, a bound session reuses its department without being told, a mismatch is
rejected, and general chat stays general. Also proves the department contextvar
is live INSIDE the streaming generator, which is where it is easy to get wrong.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app.main import app
from app.rag.context import current_department

PASSWORD = "supersecret123"


class FakeOllama:
    """Answers in plain text, and records the department visible to the loop."""

    seen_department = None

    async def aclose(self):
        pass

    async def stream_chat(self, payload):
        # The loop calls this inside the same context the tools run in, so this
        # is exactly what a tool would see.
        FakeOllama.seen_department = current_department()
        yield {"type": "content", "text": "ok"}
        yield {"type": "finish", "reason": "stop"}


class FakeMCP:
    configured = False

    async def ensure_reachable(self):
        pass


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def env():
    """Admin + a member granted HR only; a second department they cannot use."""
    with TestClient(app) as client:
        app.state.ollama = FakeOllama()
        app.state.mcp = FakeMCP()
        FakeOllama.seen_department = None

        admin = _auth(client, "admin@example.com")
        if client.get("/users/me", headers=admin).json().get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        member = _auth(client, f"chatdept-{uuid.uuid4().hex[:8]}@example.com")
        uid = client.get("/users/me", headers=member).json()["id"]

        tag = uuid.uuid4().hex[:6]
        hr, fin = f"chr{tag}", f"cfin{tag}"
        for code in (hr, fin):
            client.post("/v1/departments", json={"code": code, "name": code.upper()},
                        headers=admin)
        client.post(f"/v1/departments/{hr}/members", json={"user_id": uid},
                    headers=admin)

        yield client, member, admin, hr, fin


def _chat(client, headers, **body):
    return client.post("/v1/chat", json={"message": "hello", **body}, headers=headers)


def _session_department(session_id):
    """Read the binding straight from the row — the server-side source of truth."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.config import get_settings

    async def go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return (await conn.execute(text(
                    "SELECT department_id FROM chat_sessions WHERE id = :i"),
                    {"i": session_id})).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# Binding
# --------------------------------------------------------------------------- #
def test_a_new_session_binds_to_the_selected_department(env):
    client, member, _admin, hr, _fin = env
    resp = _chat(client, member, department=hr)
    assert resp.status_code == 200
    assert _session_department(resp.json()["session_id"]) is not None


def test_a_bound_session_reuses_its_department_without_being_told(env):
    """`department` is required only to OPEN the chat; the server then reads
    chat_sessions.department_id. This is the slice-3 change from a 400."""
    client, member, _admin, hr, _fin = env
    sid = _chat(client, member, department=hr).json()["session_id"]

    follow_up = _chat(client, member, session_id=sid)   # no department field
    assert follow_up.status_code == 200
    assert FakeOllama.seen_department is not None
    assert FakeOllama.seen_department.code == hr


def test_a_matching_department_on_a_bound_session_is_accepted(env):
    client, member, _admin, hr, _fin = env
    sid = _chat(client, member, department=hr).json()["session_id"]
    assert _chat(client, member, session_id=sid, department=hr).status_code == 200


def test_a_mismatched_department_is_rejected(env):
    """An HR conversation must not be continued as Finance on turn five."""
    client, member, admin, hr, fin = env
    sid = _chat(client, member, department=hr).json()["session_id"]
    # Use the admin, who bypasses the grant check, so this can only be the
    # session-mismatch rule rejecting it — not a missing grant.
    admin_sid = _chat(client, admin, department=hr).json()["session_id"]
    assert _chat(client, admin, session_id=admin_sid, department=fin).status_code == 409


# --------------------------------------------------------------------------- #
# General chat stays general
# --------------------------------------------------------------------------- #
def test_a_chat_with_no_department_is_general(env):
    client, member, _admin, _hr, _fin = env
    resp = _chat(client, member)
    assert resp.status_code == 200
    assert _session_department(resp.json()["session_id"]) is None
    assert FakeOllama.seen_department is None


def test_an_existing_general_session_cannot_be_adopted_into_a_department(env):
    """Every prior turn was answered without departmental grounding; relabelling
    the thread would misrepresent all of them."""
    client, member, _admin, hr, _fin = env
    sid = _chat(client, member).json()["session_id"]
    assert _chat(client, member, session_id=sid, department=hr).status_code == 409


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
def test_an_ungranted_department_is_403(env):
    client, member, _admin, _hr, fin = env
    assert _chat(client, member, department=fin).status_code == 403


def test_an_unknown_department_is_404(env):
    client, member, _admin, _hr, _fin = env
    assert _chat(client, member, department="no-such-dept-xyz").status_code == 404


def test_an_inactive_department_is_404(env):
    client, member, admin, hr, _fin = env
    client.patch(f"/v1/departments/{hr}", json={"is_active": False}, headers=admin)
    assert _chat(client, member, department=hr).status_code == 404


def test_a_revoked_grant_takes_effect_on_the_next_turn(env):
    """Postgres stays the live authorization source — no token claims, no cache."""
    client, member, admin, hr, _fin = env
    uid = client.get("/users/me", headers=member).json()["id"]
    sid = _chat(client, member, department=hr).json()["session_id"]

    client.delete(f"/v1/departments/{hr}/members/{uid}", headers=admin)
    assert _chat(client, member, session_id=sid).status_code == 403


# --------------------------------------------------------------------------- #
# The streaming contextvar — the easy thing to get wrong
# --------------------------------------------------------------------------- #
def test_the_department_is_active_inside_the_streaming_generator(env):
    """A contextvar set in the router before returning StreamingResponse is NOT
    visible inside the generator Starlette later iterates."""
    client, member, _admin, hr, _fin = env
    FakeOllama.seen_department = None

    with client.stream("POST", "/v1/chat",
                       json={"message": "hello", "department": hr, "stream": True},
                       headers=member) as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line]

    assert any(e["type"] == "done" for e in events)
    assert FakeOllama.seen_department is not None
    assert FakeOllama.seen_department.code == hr


def test_general_streaming_turns_have_no_department(env):
    client, member, _admin, _hr, _fin = env
    FakeOllama.seen_department = None

    with client.stream("POST", "/v1/chat",
                       json={"message": "hello", "stream": True},
                       headers=member) as resp:
        assert resp.status_code == 200
        list(resp.iter_lines())

    assert FakeOllama.seen_department is None
