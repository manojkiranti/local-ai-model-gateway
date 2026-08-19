"""Sources persist on the assistant row and replay through GET /v1/sessions/{id}.

Real Postgres + TestClient; skips if the DB is down. No model call: the rows are
written directly, because what is under test is the storage/serialization
contract, not the agent loop.

Two invariants matter here:
  * `download_url` is NEVER in the stored row — it is derived on read, so a
    route change cannot strand stale URLs in the database.
  * sources survive `EXPOSE_TRACE=false`. The trace is diagnostics and gets
    suppressed; citations are part of the answer and must not be.
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app

PASSWORD = "supersecret123"

STORED_SOURCES = [
    {
        "document_id": "doc1111",
        "title": "Leave Policy",
        "department_code": "hr",
        "file_name": "leave.pdf",
        "file_type": "pdf",
        "pages": [2, 5],
        "cited": True,
    },
    {
        "document_id": "doc2222",
        "title": "Pay Policy",
        "department_code": "hr",
        "file_name": "pay.pdf",
        "file_type": "pdf",
        "pages": [],
        "cited": False,
    },
]


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


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
def thread():
    """A session with one assistant message carrying persisted sources."""
    session_id = uuid.uuid4().hex
    with TestClient(app) as client:
        headers = _auth(client, f"src-{uuid.uuid4().hex[:8]}@example.com")
        user_id = client.get("/users/me", headers=headers).json()["id"]

        async def seed(conn):
            await conn.execute(
                text(
                    "INSERT INTO chat_sessions (id, user_id, title) "
                    "VALUES (:i, :u, 'sources thread')"
                ),
                {"i": session_id, "u": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO chat_messages (id, session_id, seq, role, content) "
                    "VALUES (:i, :s, 1, 'user', 'How much leave do I get?')"
                ),
                {"i": uuid.uuid4().hex, "s": session_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO chat_messages "
                    "  (id, session_id, seq, role, content, trace, sources) "
                    "VALUES (:i, :s, 2, 'assistant', 'You get 20 days [1].', "
                    "        CAST(:t AS jsonb), CAST(:src AS jsonb))"
                ),
                {
                    "i": uuid.uuid4().hex,
                    "s": session_id,
                    "t": json.dumps([{"iteration": 1, "tool_calls": [
                        {"name": "search_department_docs", "status": "ok"}]}]),
                    "src": json.dumps(STORED_SOURCES),
                },
            )

        try:
            _sql(seed)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"could not seed thread: {type(exc).__name__}")

        try:
            yield client, headers, session_id
        finally:
            _sql(lambda c: c.execute(
                text("DELETE FROM chat_sessions WHERE id = :i"), {"i": session_id}
            ))


def _assistant(body):
    return next(m for m in body["messages"] if m["role"] == "assistant")


def test_sources_replay_on_session_read(thread):
    client, headers, session_id = thread
    body = client.get(f"/v1/sessions/{session_id}", headers=headers).json()
    sources = _assistant(body)["sources"]

    assert [s["document_id"] for s in sources] == ["doc1111", "doc2222"]
    assert sources[0]["title"] == "Leave Policy"
    assert sources[0]["pages"] == [2, 5]
    assert sources[0]["cited"] is True
    assert sources[1]["cited"] is False


def test_download_url_is_derived_on_read(thread):
    client, headers, session_id = thread
    body = client.get(f"/v1/sessions/{session_id}", headers=headers).json()
    sources = _assistant(body)["sources"]

    assert sources[0]["download_url"] == (
        "/v1/departments/hr/documents/doc1111/download"
    )


def test_download_url_is_not_persisted(thread):
    """The derived field must not leak into storage."""
    _client, _headers, session_id = thread
    stored = _sql(lambda c: c.execute(
        text("SELECT sources FROM chat_messages "
             "WHERE session_id = :s AND role = 'assistant'"),
        {"s": session_id},
    ))
    rows = stored.scalar_one()

    assert rows, "sources should be stored"
    assert all("download_url" not in row for row in rows)


def test_user_messages_have_no_sources(thread):
    client, headers, session_id = thread
    body = client.get(f"/v1/sessions/{session_id}", headers=headers).json()
    user_msg = next(m for m in body["messages"] if m["role"] == "user")
    assert user_msg["sources"] is None


def test_sources_survive_expose_trace_false(thread):
    """Citations are a product feature; the trace is diagnostics. Turning off
    EXPOSE_TRACE must suppress one and not the other."""
    client, headers, session_id = thread
    settings = app.state.settings
    original = settings.expose_trace
    app.state.settings = settings.model_copy(update={"expose_trace": False})
    try:
        body = client.get(f"/v1/sessions/{session_id}", headers=headers).json()
        message = _assistant(body)
        assert message["trace"] is None, "trace must be suppressed"
        assert message["sources"], "sources must NOT be suppressed"
        assert message["sources"][0]["download_url"]
    finally:
        app.state.settings = settings.model_copy(update={"expose_trace": original})


def test_foreign_session_still_404s(thread):
    """Adding sources must not weaken ownership scoping.

    Reuses the fixture's client: nesting a second TestClient inside a live one
    re-runs the lifespan on a different event loop and dies with a RuntimeError.
    """
    client, _headers, session_id = thread
    other = _auth(client, f"src-other-{uuid.uuid4().hex[:8]}@example.com")
    assert client.get(f"/v1/sessions/{session_id}", headers=other).status_code == 404
