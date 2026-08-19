"""`POST /v1/chat` returns the citations behind its answer — the wiring, end to end.

Real Postgres + TestClient; Ollama and retrieval are faked. What is under test is
everything BETWEEN the tool and the client, which no other suite covers:

  * the collector contextvar is live where the tool actually runs, in BOTH turn
    paths — and for streaming that means inside the async generator Starlette
    iterates, which is where `file_sink` and `rag_context` have each been got
    wrong before;
  * the `[N]` markers in the model's final answer select the right documents;
  * `download_url` is derived on the way out;
  * NRB provenance survives the whole trip (route, machine_recovered, verify note,
    source URL) while an ordinary upload's citation stays plain;
  * the streamed `done` event carries the same payload the JSON body does.

`tests/test_rag_sources_persistence.py` covers storage and replay by writing rows
directly. This file is the other half: a real turn producing them.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app.main import app
from app.rag.retrieval import RetrievedChunk
from app.rag.sources import VERIFY_NOTE

PASSWORD = "supersecret123"
CALL_ID = "call_1"
ANSWER = "Per [1] the limit is 5 lakh, and [2] describes the process."


def _chunk(doc_id, *, title, page, nrb=False, route=None, authoritative=None):
    """One retrieved passage, as `search_chunks` would return it."""
    return RetrievedChunk(
        chunk_id=abs(hash(doc_id)) % 10_000,
        document_id=doc_id,
        title=title,
        content="Body text for " + doc_id + ". " + ("x" * 120),
        page_number=page,
        section=None,
        element_type="text",
        rrf_score=0.5,
        dense_distance=0.1,
        lexical_score=0.2,
        dense_rank=1,
        lexical_rank=1,
        chunk_metadata=(
            {"origin": "nrb", "route": route, "authoritative": authoritative}
            if nrb
            else {}
        ),
        doc_metadata=(
            {
                "origin": "nrb",
                "page_url": "https://www.nrb.org.np/circular/x/",
                "published_at": "2024-05-02",
            }
            if nrb
            else {}
        ),
        file_name=f"{doc_id}.pdf",
        file_type="pdf",
        doc_source="upload",
    )


CHUNKS = [
    _chunk("nrbdoc1", title="Unified Directive", page=7, nrb=True,
           route="ocr", authoritative=False),
    _chunk("updoc1", title="Leave Policy", page=3),
]


class FakeOllama:
    """Calls the retrieval tool once, then answers citing [1] and [2]."""

    def __init__(self):
        self.turns = 0

    async def aclose(self):
        pass

    async def stream_chat(self, payload):
        self.turns += 1
        if self.turns == 1:
            yield {
                "type": "tool_calls",
                "calls": [
                    {
                        "id": CALL_ID,
                        "name": "search_department_docs",
                        "arguments": json.dumps({"query": "leave limit"}),
                    }
                ],
            }
            yield {"type": "finish", "reason": "tool_calls"}
            return
        yield {"type": "content", "text": ANSWER}
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
def env(monkeypatch):
    """A member granted one department, with retrieval and embedding faked.

    The tool builds its OWN OllamaClient for the query embedding, so patching
    `app.state.ollama` is not enough — `embed_texts` is patched in the tool's
    namespace instead.
    """
    from app.tools.local import search_department_docs as tool

    async def fake_embed(client, texts, *, mode, model, dim, batch_size):
        return [[0.0] * dim for _ in texts]

    async def fake_search(**kwargs):
        return list(CHUNKS)

    monkeypatch.setattr(tool, "embed_texts", fake_embed)
    monkeypatch.setattr(tool, "search_chunks", fake_search)

    code = f"cit{uuid.uuid4().hex[:6]}"
    try:
        with TestClient(app) as client:
            app.state.ollama = FakeOllama()
            app.state.mcp = FakeMCP()

            admin = _auth(client, "admin@example.com")
            if client.get("/users/me", headers=admin).json().get("role") != "admin":
                pytest.skip("admin@example.com is not an admin in this database")
            member = _auth(client, f"cit-{uuid.uuid4().hex[:8]}@example.com")
            uid = client.get("/users/me", headers=member).json()["id"]

            client.post("/v1/departments", json={"code": code, "name": "Citations"},
                        headers=admin)
            client.post(f"/v1/departments/{code}/members", json={"user_id": uid},
                        headers=admin)
            yield client, member, code
    finally:
        _purge(code)


def _purge(code):
    """Departments have no DELETE API (ON DELETE RESTRICT protects audit history),
    so teardown goes straight to SQL or every run leaves an orphan behind."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.config import get_settings

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                dept = (await conn.execute(
                    text("SELECT id FROM departments WHERE code = :c"), {"c": code}
                )).scalar()
                if dept is None:
                    return
                await conn.execute(text(
                    "DELETE FROM chat_messages WHERE session_id IN "
                    "(SELECT id FROM chat_sessions WHERE department_id = :d)"
                ), {"d": dept})
                await conn.execute(
                    text("DELETE FROM chat_sessions WHERE department_id = :d"), {"d": dept})
                await conn.execute(
                    text("DELETE FROM user_departments WHERE department_id = :d"), {"d": dept})
                await conn.execute(
                    text("DELETE FROM departments WHERE id = :d"), {"d": dept})
        finally:
            await engine.dispose()

    try:
        asyncio.run(main())
    except Exception:  # noqa: BLE001 - teardown must not mask a test failure
        pass


def _turn(client, headers, code, **body):
    return client.post(
        "/v1/chat",
        json={"message": "what is the leave limit?", "department": code, **body},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# Non-streaming
# --------------------------------------------------------------------------- #
def test_a_rag_turn_returns_document_level_sources(env):
    client, member, code = env
    body = _turn(client, member, code).json()

    assert body["message"]["content"] == ANSWER
    ids = [s["document_id"] for s in body["sources"]]
    assert ids == ["nrbdoc1", "updoc1"], body["sources"]
    assert all(s["cited"] is True for s in body["sources"])


def test_the_download_url_is_derived_for_each_source(env):
    client, member, code = env
    for source in _turn(client, member, code).json()["sources"]:
        assert source["download_url"] == (
            f"/v1/departments/{code}/documents/{source['document_id']}/download"
        )


def test_an_nrb_source_carries_its_route_and_the_verify_note(env):
    client, member, code = env
    sources = {s["document_id"]: s for s in _turn(client, member, code).json()["sources"]}

    nrb = sources["nrbdoc1"]
    assert nrb["origin"] == "nrb"
    assert nrb["routes"] == ["ocr"]
    assert nrb["machine_recovered"] is True
    assert nrb["verify_note"] == VERIFY_NOTE
    assert nrb["source_url"] == "https://www.nrb.org.np/circular/x/"
    assert nrb["published_at"] == "2024-05-02"
    assert nrb["pages"] == [7]


def test_an_ordinary_upload_source_stays_plain(env):
    client, member, code = env
    sources = {s["document_id"]: s for s in _turn(client, member, code).json()["sources"]}

    upload = sources["updoc1"]
    assert upload["machine_recovered"] is None
    assert upload["verify_note"] is None
    assert upload["routes"] is None
    assert upload["title"] == "Leave Policy"


def test_a_turn_that_searches_nothing_has_null_sources(env):
    """A general chat has no corpus, so there is nothing to cite — null, not []."""
    client, member, _code = env
    app.state.ollama = FakeOllama()
    app.state.ollama.turns = 1  # skip the tool call: answer straight away
    body = client.post(
        "/v1/chat", json={"message": "hello"}, headers=member
    ).json()
    assert body["sources"] is None


# --------------------------------------------------------------------------- #
# Streaming — the path where a contextvar is easy to lose
# --------------------------------------------------------------------------- #
def test_the_streamed_done_event_carries_the_same_sources(env):
    client, member, code = env
    with client.stream(
        "POST",
        "/v1/chat",
        json={"message": "limit?", "department": code, "stream": True},
        headers=member,
    ) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line]

    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1
    sources = done[0]["sources"]
    assert [s["document_id"] for s in sources] == ["nrbdoc1", "updoc1"]
    assert sources[0]["machine_recovered"] is True
    assert sources[0]["download_url"].endswith("/nrbdoc1/download")


def test_a_streamed_turns_sources_are_persisted_and_replay(env):
    client, member, code = env
    with client.stream(
        "POST",
        "/v1/chat",
        json={"message": "limit?", "department": code, "stream": True},
        headers=member,
    ) as resp:
        sid = resp.headers["X-Session-Id"]
        for _ in resp.iter_lines():
            pass

    messages = client.get(f"/v1/sessions/{sid}", headers=member).json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant) == 1
    replayed = assistant[0]["sources"]
    assert [s["document_id"] for s in replayed] == ["nrbdoc1", "updoc1"]
    # Derived on read, never stored (see test_rag_sources_persistence.py).
    assert replayed[0]["download_url"].endswith("/nrbdoc1/download")
