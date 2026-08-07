"""End-to-end chat-history tests against real Postgres, with Ollama/MCP faked.

Skips cleanly if the database is unreachable (like the MCP integration test), so
the offline suite stays green. Exercises the real auth + persistence stack:
multi-turn context, agent trace persistence, streaming accumulation + the
X-Session-Id header, ownership 404, and cascade delete.
"""

import json

import pytest
from starlette.testclient import TestClient

from app.main import app

TEST_EMAIL = "history-itest@example.com"
TEST_PASSWORD = "supersecret123"


def _tool_turn(name, arguments):
    """One model turn that calls a tool, as normalized client events."""
    return [
        {"type": "tool_calls", "calls": [
            {"id": "call_1", "name": name, "arguments": arguments},
        ]},
        {"type": "finish", "reason": "tool_calls"},
    ]


def _text_turn(text):
    """One model turn streaming a plain answer in two content events."""
    mid = len(text) // 2
    return [
        {"type": "content", "text": text[:mid]},
        {"type": "content", "text": text[mid:]},
        {"type": "finish", "reason": "stop"},
    ]


class FakeOllama:
    """Streaming stand-in for the loop. Default = one plain reply; pass `turns`
    (a list of chunk-lists) to script a multi-turn tool sequence."""

    def __init__(self, content: str = "hello there world", turns=None) -> None:
        self.content = content
        self._turns = list(turns) if turns else None

    async def aclose(self) -> None:  # lifespan shutdown calls this
        pass

    async def stream_chat(self, payload: dict):
        if self._turns is not None:
            turn = self._turns.pop(0) if len(self._turns) > 1 else self._turns[0]
            for chunk in turn:
                yield chunk
            return
        for chunk in _text_turn(self.content):
            yield chunk


class FakeMCP:
    """Not configured -> the agent loop runs local tools only, no network."""

    configured = False

    async def ensure_reachable(self) -> None:  # streaming pre-flight no-op
        pass


def _auth_headers(client: TestClient) -> dict:
    """Ensure the test user exists, then log in. Skip if DB is unreachable.

    Register may 200 (created) or 409/400 (already exists) — all fine. If the DB
    is down the request raises (TestClient re-raises server errors), so we catch
    that and skip. Capture the error outside the try so pytest.skip's own
    exception isn't swallowed.
    """
    err = None
    resp = None
    try:
        client.post("/auth/register", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
        resp = client.post("/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    except Exception as exc:  # noqa: BLE001 - DB down -> skip, don't fail
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _cleanup(client: TestClient, headers: dict) -> None:
    for s in client.get("/v1/sessions", headers=headers).json():
        client.delete(f"/v1/sessions/{s['id']}", headers=headers)


def test_chat_history_end_to_end():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        app.state.ollama = FakeOllama()
        app.state.mcp = FakeMCP()
        _cleanup(client, headers)  # start from a clean slate for this user
        try:
            # --- turn 1: new session (no session_id) ---
            r1 = client.post("/v1/chat", json={"message": "hi"}, headers=headers)
            assert r1.status_code == 200
            sid = r1.json()["session_id"]
            assert r1.json()["message"]["content"] == "hello there world"

            # --- turn 2: same session ---
            r2 = client.post(
                "/v1/chat", json={"session_id": sid, "message": "again"}, headers=headers
            )
            assert r2.status_code == 200
            assert r2.json()["session_id"] == sid

            # --- thread is clean and ordered: user, assistant, user, assistant ---
            detail = client.get(f"/v1/sessions/{sid}", headers=headers).json()
            assert [m["role"] for m in detail["messages"]] == [
                "user", "assistant", "user", "assistant",
            ]
            assert [m["seq"] for m in detail["messages"]] == [1, 2, 3, 4]
            assert detail["messages"][0]["content"] == "hi"
            assert detail["title"] == "hi"
            # chat assistant rows carry no trace
            assert detail["messages"][1]["trace"] is None

            # --- session appears in the list with a count ---
            listed = client.get("/v1/sessions", headers=headers).json()
            assert any(s["id"] == sid and s["message_count"] == 4 for s in listed)

            # --- ownership / unknown id -> 404 ---
            assert client.get("/v1/sessions/deadbeef", headers=headers).status_code == 404
            assert client.delete("/v1/sessions/deadbeef", headers=headers).status_code == 404

            # --- delete cascades ---
            assert client.delete(f"/v1/sessions/{sid}", headers=headers).status_code == 204
            assert client.get(f"/v1/sessions/{sid}", headers=headers).status_code == 404
        finally:
            _cleanup(client, headers)


def test_tool_turn_persists_trace_on_chat():
    """A tool-using /v1/chat turn stores the execution trace as JSONB; the model
    calls the real local get_current_time tool, then answers."""
    with TestClient(app) as client:
        headers = _auth_headers(client)
        app.state.ollama = FakeOllama(
            turns=[_tool_turn("get_current_time", {}), _text_turn("It is now.")]
        )
        app.state.mcp = FakeMCP()
        try:
            r = client.post("/v1/chat", json={"message": "what time is it?"}, headers=headers)
            assert r.status_code == 200
            assert r.json()["message"]["content"] == "It is now."
            assert r.json()["stop_reason"] == "completed"
            assert isinstance(r.json()["trace"], list) and len(r.json()["trace"]) >= 1
            sid = r.json()["session_id"]

            detail = client.get(f"/v1/sessions/{sid}", headers=headers).json()
            assistant = [m for m in detail["messages"] if m["role"] == "assistant"][0]
            # Execution trace persisted as JSONB on the clean assistant row.
            assert isinstance(assistant["trace"], list) and len(assistant["trace"]) >= 1
            assert assistant["content"] == "It is now."
        finally:
            _cleanup(client, headers)


def test_streaming_emits_events_and_persists_final_answer():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        app.state.ollama = FakeOllama(content="streamed reply text")
        app.state.mcp = FakeMCP()
        try:
            r = client.post(
                "/v1/chat", json={"message": "stream please", "stream": True}, headers=headers
            )
            assert r.status_code == 200
            sid = r.headers["x-session-id"]  # session id delivered via header
            assert sid

            events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
            types = [e["type"] for e in events]
            assert "token" in types and types[-1] == "done"
            assert events[-1]["session_id"] == sid
            # token deltas reassemble to the answer
            answer = "".join(e["content"] for e in events if e["type"] == "token")
            assert answer == "streamed reply text"

            detail = client.get(f"/v1/sessions/{sid}", headers=headers).json()
            assistant = [m for m in detail["messages"] if m["role"] == "assistant"][0]
            assert assistant["content"] == "streamed reply text"
        finally:
            _cleanup(client, headers)
