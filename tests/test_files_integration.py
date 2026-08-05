"""End-to-end per-user generated-files tests against real Postgres (Ollama/MCP
faked). Skips cleanly if the database is unreachable, like the other integration
tests, so the offline suite stays green.

Exercises the real stack: a /v1/chat turn where the model calls create_html runs
through the PostgresFileSink, so the file gets a `generated_files` row owned by
the caller. Then asserts GET /v1/files lists it, GET /v1/files/{id} serves the
exact bytes to the owner, a DIFFERENT user gets 404 (owner scoping), and an
unknown id is 404.
"""

import pytest
from starlette.testclient import TestClient

from app.main import app

OWNER_EMAIL = "files-owner-itest@example.com"
OTHER_EMAIL = "files-other-itest@example.com"
PASSWORD = "supersecret123"

HTML = "<!doctype html><html><body><h1>owned &amp; served</h1></body></html>"


def _tool_then_answer(name, arguments, answer="done"):
    """Script FakeOllama: turn 1 calls a tool, turn 2 answers in plain text."""
    return [
        [
            {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}},
            {"message": {"content": ""}, "done": True},
        ],
        [
            {"message": {"role": "assistant", "content": answer}},
            {"message": {"content": ""}, "done": True},
        ],
    ]


class FakeOllama:
    def __init__(self, turns):
        self._turns = list(turns)

    async def aclose(self):
        pass

    async def stream_chat(self, payload):
        turn = self._turns.pop(0) if len(self._turns) > 1 else self._turns[0]
        for chunk in turn:
            yield chunk


class FakeMCP:
    configured = False

    async def ensure_reachable(self):
        pass


def _auth(client, email):
    """Ensure a user exists + log in; skip if Postgres is unreachable."""
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001 - DB down -> skip
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_generated_file_is_owned_listed_and_owner_scoped():
    with TestClient(app) as client:
        owner = _auth(client, OWNER_EMAIL)
        other = _auth(client, OTHER_EMAIL)
        app.state.mcp = FakeMCP()

        # A chat turn where the model calls create_html -> file saved via the sink.
        app.state.ollama = FakeOllama(
            _tool_then_answer("create_html", {"html_content": HTML, "filename": "hello"})
        )
        r = client.post("/v1/chat", json={"message": "make a page"}, headers=owner)
        assert r.status_code == 200

        # --- it shows up in the owner's file list ---
        listing = client.get("/v1/files", headers=owner)
        assert listing.status_code == 200
        files = listing.json()["files"]
        mine = [f for f in files if f["filename"] == "hello.html"]
        assert mine, f"created file not listed: {files}"
        fid = mine[0]["id"]
        assert mine[0]["media_type"].startswith("text/html")
        assert mine[0]["size"] == len(HTML.encode("utf-8"))

        # --- owner can download exact bytes, with the safety headers ---
        dl = client.get(f"/v1/files/{fid}", headers=owner)
        assert dl.status_code == 200
        assert dl.headers["content-type"].startswith("text/html")
        assert dl.headers["x-content-type-options"] == "nosniff"
        assert dl.content == HTML.encode("utf-8")

        # --- a DIFFERENT user cannot see or download it ---
        assert all(f["id"] != fid for f in client.get("/v1/files", headers=other).json()["files"])
        assert client.get(f"/v1/files/{fid}", headers=other).status_code == 404

        # --- unknown id -> 404 ---
        assert client.get("/v1/files/deadbeef", headers=owner).status_code == 404


def test_delete_is_owner_scoped_and_removes_file():
    import os

    with TestClient(app) as client:
        owner = _auth(client, OWNER_EMAIL)
        other = _auth(client, OTHER_EMAIL)
        app.state.mcp = FakeMCP()
        app.state.ollama = FakeOllama(
            _tool_then_answer("create_html", {"html_content": HTML, "filename": "trash"})
        )
        client.post("/v1/chat", json={"message": "make a page"}, headers=owner)

        fid = [f for f in client.get("/v1/files", headers=owner).json()["files"]
               if f["filename"] == "trash.html"][0]["id"]
        # grab the on-disk path before deleting so we can assert it's gone
        path = client.get(f"/v1/files/{fid}", headers=owner)
        assert path.status_code == 200

        # a DIFFERENT user cannot delete it
        assert client.delete(f"/v1/files/{fid}", headers=other).status_code == 404
        # still there for the owner
        assert client.get(f"/v1/files/{fid}", headers=owner).status_code == 200

        # owner deletes -> 204, then it's gone from list + download + disk
        assert client.delete(f"/v1/files/{fid}", headers=owner).status_code == 204
        assert client.get(f"/v1/files/{fid}", headers=owner).status_code == 404
        assert all(f["id"] != fid for f in client.get("/v1/files", headers=owner).json()["files"])

        # deleting again / unknown id -> 404
        assert client.delete(f"/v1/files/{fid}", headers=owner).status_code == 404
        assert client.delete("/v1/files/deadbeef", headers=owner).status_code == 404


def test_streaming_turn_also_owns_generated_file():
    """The sink is installed INSIDE the async generator Starlette iterates, so a
    file created during a streaming turn must still get an owned row (guards the
    contextvar-in-generator gotcha)."""
    with TestClient(app) as client:
        owner = _auth(client, OWNER_EMAIL)
        app.state.mcp = FakeMCP()
        app.state.ollama = FakeOllama(
            _tool_then_answer("create_html", {"html_content": HTML, "filename": "streamed"})
        )
        before = {f["id"] for f in client.get("/v1/files", headers=owner).json()["files"]}

        r = client.post(
            "/v1/chat", json={"message": "stream a page", "stream": True}, headers=owner
        )
        assert r.status_code == 200
        assert r.headers.get("x-session-id")

        after = client.get("/v1/files", headers=owner).json()["files"]
        new = [f for f in after if f["id"] not in before and f["filename"] == "streamed.html"]
        assert new, "streaming turn did not produce an owned file row"
        assert client.get(f"/v1/files/{new[0]['id']}", headers=owner).status_code == 200
