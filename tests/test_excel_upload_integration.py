"""End-to-end tests for spreadsheet upload + attach-to-chat, against real
Postgres (Ollama/MCP faked). Skips cleanly if the DB is unreachable.

Covers: POST /v1/files (happy .xlsx + .csv, bad extension, empty), it lands in
GET /v1/files?source=uploaded and is owner-scoped, then a /v1/chat turn that
attaches the file_id and has the (faked) model call read_excel — proving the
owner-scoped file source resolves the id inside the turn — and that a foreign
file_id is a clean 404. Finally, the attachment note survives to a later turn.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook
from starlette.testclient import TestClient

from app.main import app

OWNER = "xlup-owner@example.com"
OTHER = "xlup-other@example.com"
PASSWORD = "supersecret123"


def _xlsx_bytes(sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _tool_then_answer(name, arguments, answer="done"):
    """Script FakeOllama: turn 1 calls a tool, turn 2 answers in plain text,
    as normalized client events."""
    return [
        [
            {"type": "tool_calls", "calls": [
                {"id": "call_1", "name": name, "arguments": arguments},
            ]},
            {"type": "finish", "reason": "tool_calls"},
        ],
        [
            {"type": "content", "text": answer},
            {"type": "finish", "reason": "stop"},
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


# `POST /auth/register` became admin-only when Active Directory sign-in landed: a
# public register let anyone pre-register a colleague's address as a LOCAL account
# and permanently shadow their AD identity. Creating a fresh test user therefore
# needs an admin token, which the seeded test admin supplies. If that admin is
# absent, the register call fails quietly and the caller still skips on the login
# — the same behaviour as before.
SEEDED_ADMIN_EMAIL = "admin@example.com"
SEEDED_ADMIN_PASSWORD = "supersecret123"


def _ensure_user(client, email, password):
    """Create the user if it does not exist yet, as an admin must now do."""
    headers = {}
    if email != SEEDED_ADMIN_EMAIL:
        resp = client.post(
            "/auth/login",
            json={"email": SEEDED_ADMIN_EMAIL, "password": SEEDED_ADMIN_PASSWORD},
        )
        if resp.status_code == 200:
            headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    client.post(
        "/auth/register",
        json={"email": email, "password": password},
        headers=headers,
    )


def _auth(client, email):
    err = resp = None
    try:
        _ensure_user(client, email, PASSWORD)
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(client, headers, name, data, ctype):
    return client.post(
        "/v1/files", files={"file": (name, data, ctype)}, headers=headers
    )


XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _tool_results(trace) -> str:
    """Flatten all tool-call result strings from an agent trace."""
    return "\n".join(
        (tc.get("result") or "")
        for entry in trace
        for tc in entry.get("tool_calls", [])
    )


def test_upload_xlsx_then_read_via_chat():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        app.state.mcp = FakeMCP()

        data = _xlsx_bytes({"Q1": [["name", "amount"], ["a", 10], ["b", 20]],
                            "Q2": [["name", "amount"], ["c", 30]]})
        up = _upload(client, owner, "sales.xlsx", data, XLSX_CT)
        assert up.status_code == 201, up.text
        body = up.json()
        fid = body["id"]
        assert body["source"] == "uploaded"
        assert body["summary"]["kind"] == "Excel"
        assert len(body["summary"]["sheets"]) == 2

        # shows up filtered as an upload
        listed = client.get("/v1/files?source=uploaded", headers=owner).json()["files"]
        assert any(f["id"] == fid and f["source"] == "uploaded" for f in listed)

        # a chat turn attaching it; the faked model calls read_excel with the id
        app.state.ollama = FakeOllama(
            _tool_then_answer("read_excel", {"file_id": fid, "sheet": "Q2"}, answer="ok")
        )
        r = client.post(
            "/v1/chat",
            json={"message": "what is in Q2?", "file_ids": [fid]},
            headers=owner,
        )
        assert r.status_code == 200, r.text
        blob = _tool_results(r.json().get("trace") or [])
        # the read_excel tool result must contain real sheet data (owner-scoped
        # source resolved the id inside the turn), not an ERROR.
        assert "Sheet 'Q2'" in blob, blob
        assert "ERROR" not in blob, blob


def test_upload_csv_ok():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "p.csv", b"name,age\nAda,36\n", "text/csv")
        assert up.status_code == 201, up.text
        assert up.json()["summary"]["kind"] == "CSV"


def test_upload_bad_extension_rejected():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        # .rtf, not .txt: .txt became a supported document format when
        # read_document landed. .rtf is still outside the allowlist.
        up = _upload(client, owner, "notes.rtf", b"{\\rtf1}", "application/rtf")
        assert up.status_code == 400


def test_upload_empty_rejected():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "empty.csv", b"", "text/csv")
        assert up.status_code == 400


def test_attach_foreign_file_id_is_404():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        other = _auth(client, OTHER)
        app.state.mcp = FakeMCP()
        app.state.ollama = FakeOllama(_tool_then_answer("read_excel", {"file_id": "x"}))

        data = _xlsx_bytes({"S": [["h"], ["1"]]})
        fid = _upload(client, owner, "mine.xlsx", data, XLSX_CT).json()["id"]

        # OTHER user attaches OWNER's file -> 404, no leak
        r = client.post(
            "/v1/chat", json={"message": "read it", "file_ids": [fid]}, headers=other
        )
        assert r.status_code == 404


def test_upload_over_size_cap_is_413():
    from app.config import get_settings

    settings = get_settings()
    original = settings.upload_max_bytes
    settings.upload_max_bytes = 1024  # 1 KB, temporarily
    try:
        with TestClient(app) as client:
            owner = _auth(client, OWNER)
            big = _xlsx_bytes({"S": [["h"]] + [[i] for i in range(500)]})
            assert len(big) > 1024
            up = _upload(client, owner, "big.xlsx", big, XLSX_CT)
            assert up.status_code == 413
            # rejected upload must not appear in the file list
            listed = client.get("/v1/files?source=uploaded", headers=owner).json()["files"]
            assert all(f["filename"] != "big.xlsx" for f in listed)
    finally:
        settings.upload_max_bytes = original


def test_attachment_survives_to_next_turn():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        app.state.mcp = FakeMCP()

        data = _xlsx_bytes({"S": [["h"], ["1"]]})
        fid = _upload(client, owner, "keep.xlsx", data, XLSX_CT).json()["id"]

        # turn 1: attach, model answers plainly (no tool)
        app.state.ollama = FakeOllama(
            [[{"type": "content", "text": "got it"}, {"type": "finish", "reason": "stop"}]]
        )
        r1 = client.post(
            "/v1/chat", json={"message": "here is a file", "file_ids": [fid]}, headers=owner
        )
        assert r1.status_code == 200
        sid = r1.json()["session_id"]

        # turn 2: NO file_ids resent; the model calls read_excel using the id it
        # only knows from the persisted attachment note.
        app.state.ollama = FakeOllama(
            _tool_then_answer("read_excel", {"file_id": fid}, answer="done")
        )
        r2 = client.post(
            "/v1/chat", json={"message": "now read it", "session_id": sid}, headers=owner
        )
        assert r2.status_code == 200, r2.text
        blob = _tool_results(r2.json().get("trace") or [])
        assert "Sheet 'S'" in blob, blob
