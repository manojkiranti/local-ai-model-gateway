"""GET /v1/departments/{code}/documents/{id}/download — what a citation links to.

Real Postgres + TestClient; skips if the DB is down. No Ollama, no Docling: the
route serves stored bytes and never parses.

Status codes are split on purpose and each split is asserted below:
department-level access is 403 (matching `GET /{code}/documents` beside it),
document-level anything is 404 (matching the archive route).

The fixture CLEANS UP the department it creates. Departments have no DELETE API
by design (ON DELETE RESTRICT protects audit history), so teardown goes straight
to SQL — otherwise every run leaves an orphan behind, which is exactly how this
database accumulated 134 stray test departments.
"""

import asyncio
import io
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app

PASSWORD = "supersecret123"
CSV = b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\n"


def _sql(fn):
    """Run `fn(conn)` on a THROWAWAY NullPool engine.

    The app's module-level engine pools connections bound to the first event
    loop, and each `asyncio.run` creates a new one — reusing it would fail the
    second call with "Event loop is closed".
    """

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


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


def _me(client, headers):
    return client.get("/users/me", headers=headers).json()


def _purge(code: str) -> None:
    """Remove a test department and everything referencing it, in FK order."""

    async def go(conn):
        row = (
            await conn.execute(
                text("SELECT id FROM departments WHERE code = :c"), {"c": code}
            )
        ).first()
        if row is None:
            return
        dept_id = row[0]
        for stmt in (
            "DELETE FROM ingest_jobs WHERE document_id IN "
            "(SELECT id FROM documents WHERE department_id = :d)",
            "DELETE FROM document_chunks WHERE department_id = :d",
            "DELETE FROM documents WHERE department_id = :d",
            "DELETE FROM chat_messages WHERE session_id IN "
            "(SELECT id FROM chat_sessions WHERE department_id = :d)",
            "DELETE FROM chat_sessions WHERE department_id = :d",
            "DELETE FROM user_departments WHERE department_id = :d",
            "DELETE FROM departments WHERE id = :d",
        ):
            await conn.execute(text(stmt), {"d": dept_id})

    try:
        _sql(go)
    except Exception:  # noqa: BLE001 — teardown must not mask a test failure
        pass


@pytest.fixture()
def env():
    code = f"dl{uuid.uuid4().hex[:6]}"
    try:
        with TestClient(app) as client:
            admin = _auth(client, "admin@example.com")
            if _me(client, admin).get("role") != "admin":
                pytest.skip("admin@example.com is not an admin in this database")
            member = _auth(client, f"dl-member-{uuid.uuid4().hex[:8]}@example.com")
            uid = _me(client, member)["id"]
            outsider = _auth(client, f"dl-out-{uuid.uuid4().hex[:8]}@example.com")

            client.post("/v1/departments", json={"code": code, "name": "Downloads"},
                        headers=admin)
            client.post(f"/v1/departments/{code}/members", json={"user_id": uid},
                        headers=admin)
            yield client, admin, member, outsider, code
    finally:
        _purge(code)


def _upload(client, headers, code, name=" leave.csv", data=CSV):
    return client.post(
        f"/v1/departments/{code}/documents",
        files={"file": (name.strip(), io.BytesIO(data), "text/csv")},
        data={"title": "Leave Policy"},
        headers=headers,
    )


def _url(code, doc_id):
    return f"/v1/departments/{code}/documents/{doc_id}/download"


def _mark_ready(doc_id: str) -> None:
    """Members may only download `ready` documents, but the API never sets that
    — the worker does. Flip it directly so the member path is testable without
    running Docling."""
    _sql(lambda c: c.execute(
        text("UPDATE documents SET status = 'ready' WHERE id = :i"), {"i": doc_id}
    ))


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_admin_downloads_original_bytes(env):
    client, admin, _member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]

    resp = client.get(_url(code, doc_id), headers=admin)
    assert resp.status_code == 200
    assert resp.content == CSV
    assert resp.headers["content-type"].startswith("text/csv")


def test_download_uses_the_original_filename(env):
    client, admin, _member, _outsider, code = env
    doc_id = _upload(client, admin, code, name="leave.csv").json()["document_id"]

    resp = client.get(_url(code, doc_id), headers=admin)
    assert "leave.csv" in resp.headers["content-disposition"]


def test_member_can_download_a_ready_document(env):
    client, admin, member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]
    _mark_ready(doc_id)

    resp = client.get(_url(code, doc_id), headers=member)
    assert resp.status_code == 200
    assert resp.content == CSV


# --------------------------------------------------------------------------- #
# Typed-in text documents
# --------------------------------------------------------------------------- #
def test_typed_text_document_downloads_with_a_title_derived_name(env):
    """`source='manual'` rows have file_name=None; the name comes from the title."""
    client, admin, _member, _outsider, code = env
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Dress Code", "content": "Business casual on Fridays."},
        headers=admin,
    )
    assert created.status_code == 202
    doc_id = created.json()["document_id"]

    resp = client.get(_url(code, doc_id), headers=admin)
    assert resp.status_code == 200
    assert b"Business casual" in resp.content
    # Starlette RFC 5987-encodes anything that is not header-safe, so the space
    # arrives percent-encoded via `filename*` rather than in a quoted string.
    disposition = resp.headers["content-disposition"]
    assert "Dress%20Code.txt" in disposition or 'Dress Code.txt"' in disposition
    assert resp.headers["content-type"].startswith("text/plain")


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_unauthenticated_download_is_rejected(env):
    client, admin, _member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]

    resp = client.get(_url(code, doc_id))
    assert resp.status_code in (401, 403)


def test_ungranted_department_is_403_matching_the_list_route(env):
    """Deliberately NOT 404: `GET /{code}/documents` answers 403 here, and the
    download route must not contradict the route beside it."""
    client, admin, _member, outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]
    _mark_ready(doc_id)

    listed = client.get(f"/v1/departments/{code}/documents", headers=outsider)
    resp = client.get(_url(code, doc_id), headers=outsider)
    assert listed.status_code == 403
    assert resp.status_code == 403


def test_member_cannot_download_a_pending_document(env):
    """Not yet ingested -> not part of the corpus a member can cite -> 404."""
    client, admin, member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]  # status=pending

    assert client.get(_url(code, doc_id), headers=member).status_code == 404


def test_unknown_document_id_is_404(env):
    client, admin, _member, _outsider, code = env
    assert client.get(_url(code, uuid.uuid4().hex), headers=admin).status_code == 404


def test_document_from_another_department_is_404(env):
    """The id exists, but not under this department's code."""
    client, admin, _member, _outsider, code = env
    other = f"dl{uuid.uuid4().hex[:6]}"
    try:
        client.post("/v1/departments", json={"code": other, "name": "Other"},
                    headers=admin)
        doc_id = _upload(client, admin, other).json()["document_id"]

        assert client.get(_url(code, doc_id), headers=admin).status_code == 404
    finally:
        _purge(other)


def test_unknown_department_is_404(env):
    client, admin, _member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]

    resp = client.get(_url(f"nope{uuid.uuid4().hex[:6]}", doc_id), headers=admin)
    assert resp.status_code == 404


def test_archived_document_is_404_for_a_member(env):
    client, admin, member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]
    _mark_ready(doc_id)
    client.delete(f"/v1/departments/{code}/documents/{doc_id}", headers=admin)

    assert client.get(_url(code, doc_id), headers=member).status_code == 404


def test_missing_file_on_disk_is_404_not_500(env):
    """The row survives; the bytes were removed out of band."""
    client, admin, _member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]
    _sql(lambda c: c.execute(
        text("UPDATE documents SET storage_key = :k WHERE id = :i"),
        {"k": f"{code}/does-not-exist.csv", "i": doc_id},
    ))

    assert client.get(_url(code, doc_id), headers=admin).status_code == 404


def test_traversal_storage_key_is_refused(env):
    """`storage_key` is minted by us but round-trips through the database, so it
    is treated as untrusted coming back. A key escaping RAG_DOCS_DIR must never
    serve a file."""
    client, admin, _member, _outsider, code = env
    doc_id = _upload(client, admin, code).json()["document_id"]
    _sql(lambda c: c.execute(
        text("UPDATE documents SET storage_key = :k WHERE id = :i"),
        {"k": "../../../../etc/passwd", "i": doc_id},
    ))

    resp = client.get(_url(code, doc_id), headers=admin)
    assert resp.status_code == 404
    assert b"root:" not in resp.content


# --------------------------------------------------------------------------- #
# NRB documents: the bytes are in the filestore, not RAG_DOCS_DIR (§28)
# --------------------------------------------------------------------------- #
def test_an_nrb_document_downloads_from_the_filestore(env, tmp_path, monkeypatch):
    """End-to-end over HTTP, because the unit test for `_document_path` cannot see
    the route's own 404 paths.

    An NRB document is an ordinary `documents` row with `metadata.origin='nrb'`
    whose bytes were never copied under RAG_DOCS_DIR — they live content-addressed
    in the NRB filestore. Before the origin branch existed, this request 404'd
    while the document was listed as ready.
    """
    import hashlib

    from app.nrb import filestore

    client, admin, _member, _outsider, code = env
    payload = b"%PDF-1.4 nrb circular bytes"
    digest = hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path / "nrb_files")
    blob = tmp_path / "nrb_files" / digest[:2] / f"{digest}.pdf"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)

    doc_id = _upload(client, admin, code).json()["document_id"]
    # Turn the uploaded row into what the NRB ingest driver mints: origin=nrb, the
    # blob's hash as content_hash, and a FILESTORE storage_key.
    _sql(lambda c: c.execute(
        text(
            "UPDATE documents SET metadata = jsonb_build_object("
            "  'origin', 'nrb', 'page_url', 'https://www.nrb.org.np/x/'),"
            " content_hash = :h, file_type = 'pdf', storage_key = :k"
            " WHERE id = :i"
        ),
        {"h": digest, "k": f"{digest[:2]}/{digest}.pdf", "i": doc_id},
    ))

    resp = client.get(_url(code, doc_id), headers=admin)
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-type"] == "application/pdf"
