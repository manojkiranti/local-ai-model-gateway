"""Corpus document admin API. Real Postgres + TestClient; skips if the DB is down.

The API process never parses or embeds — it writes two rows and returns 202, so
these tests need no Ollama and no Docling.
"""

import io
import uuid

import pytest
from starlette.testclient import TestClient

from app.main import app

PASSWORD = "supersecret123"
CSV = b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\n"


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


@pytest.fixture()
def env():
    with TestClient(app) as client:
        admin = _auth(client, "admin@example.com")
        if _me(client, admin).get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        member = _auth(client, f"docs-member-{uuid.uuid4().hex[:8]}@example.com")
        uid = _me(client, member)["id"]
        code = f"docs{uuid.uuid4().hex[:6]}"
        client.post("/v1/departments", json={"code": code, "name": "Docs"},
                    headers=admin)
        client.post(f"/v1/departments/{code}/members", json={"user_id": uid},
                    headers=admin)
        yield client, admin, member, code


def _upload(client, headers, code, name, data, ctype="text/csv"):
    return client.post(
        f"/v1/departments/{code}/documents",
        files={"file": (name, io.BytesIO(data), ctype)},
        data={"title": "A Document"},
        headers=headers,
    )


def test_upload_returns_202_with_a_document_and_job_id(env):
    client, admin, _member, code = env
    resp = _upload(client, admin, code, "leave.csv", CSV)
    assert resp.status_code == 202
    body = resp.json()
    assert body["document_id"] and body["job_id"]
    assert body["status"] == "queued"


def test_uploaded_document_starts_pending_with_no_chunks(env):
    client, admin, _member, code = env
    _upload(client, admin, code, "leave.csv", CSV)
    listed = client.get(f"/v1/departments/{code}/documents", headers=admin).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "pending"
    assert listed[0]["chunk_count"] == 0


def test_member_cannot_upload(env):
    client, _admin, member, code = env
    assert _upload(client, member, code, "leave.csv", CSV).status_code == 403


def test_member_can_list_their_departments_documents(env):
    client, admin, member, code = env
    _upload(client, admin, code, "leave.csv", CSV)
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=member).status_code == 200


def test_a_non_member_cannot_list_documents(env):
    client, admin, _member, code = env
    outsider = _auth(client, f"outsider-{uuid.uuid4().hex[:8]}@example.com")
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=outsider).status_code == 403


def test_unsupported_extension_is_400(env):
    client, admin, _member, code = env
    resp = _upload(client, admin, code, "payload.exe", b"MZ", "application/octet-stream")
    assert resp.status_code == 400


def test_oversized_upload_is_413(env):
    client, admin, _member, code = env
    from app.config import get_settings
    big = b"x" * (get_settings().upload_max_bytes + 1)
    assert _upload(client, admin, code, "big.csv", big).status_code == 413


def test_duplicate_content_is_409(env):
    client, admin, _member, code = env
    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 202
    assert _upload(client, admin, code, "same.csv", CSV).status_code == 409


def test_typed_text_is_accepted_as_a_manual_document(env):
    client, admin, _member, code = env
    resp = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Verbal policy", "content": "Leave requests go to your lead."},
        headers=admin,
    )
    assert resp.status_code == 202
    listed = client.get(f"/v1/departments/{code}/documents", headers=admin).json()
    assert listed[0]["source"] == "manual"
    assert listed[0]["file_name"] is None


def test_empty_typed_text_is_400(env):
    client, admin, _member, code = env
    resp = client.post(f"/v1/departments/{code}/documents/text",
                       json={"title": "Empty", "content": "   "}, headers=admin)
    assert resp.status_code == 400


def test_archiving_hides_the_document_and_frees_the_hash(env):
    client, admin, _member, code = env
    doc_id = _upload(client, admin, code, "leave.csv", CSV).json()["document_id"]

    assert client.delete(f"/v1/departments/{code}/documents/{doc_id}",
                         headers=admin).status_code == 204
    assert client.get(f"/v1/departments/{code}/documents", headers=admin).json() == []
    # Same content can now be re-uploaded.
    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 202


def test_include_archived_is_admin_only(env):
    client, admin, member, code = env
    doc_id = _upload(client, admin, code, "leave.csv", CSV).json()["document_id"]
    client.delete(f"/v1/departments/{code}/documents/{doc_id}", headers=admin)

    seen = client.get(f"/v1/departments/{code}/documents?include_archived=true",
                      headers=admin).json()
    assert len(seen) == 1
    assert client.get(f"/v1/departments/{code}/documents?include_archived=true",
                      headers=member).status_code == 403


def test_unknown_department_is_404(env):
    client, admin, _member, _code = env
    assert _upload(client, admin, "nope-xyz", "a.csv", CSV).status_code == 404


def test_job_status_is_pollable(env):
    client, admin, _member, code = env
    job_id = _upload(client, admin, code, "leave.csv", CSV).json()["job_id"]
    resp = client.get(f"/v1/ingest-jobs/{job_id}", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["chunks_done"] == 0


def test_unknown_job_is_404(env):
    client, admin, _member, _code = env
    assert client.get(f"/v1/ingest-jobs/{uuid.uuid4().hex}",
                      headers=admin).status_code == 404


def test_members_see_only_ready_documents(env):
    """A pending or failed document is not part of the corpus a member's answers
    can cite, so surfacing it only invites 'why can't the assistant see this?'."""
    client, admin, member, code = env
    _upload(client, admin, code, "leave.csv", CSV)   # stays 'pending', no worker

    assert client.get(f"/v1/departments/{code}/documents",
                      headers=admin).json() != []
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=member).json() == []


def test_member_response_omits_embed_model(env):
    client, admin, member, code = env
    _upload(client, admin, code, "leave.csv", CSV)

    admin_row = client.get(f"/v1/departments/{code}/documents",
                           headers=admin).json()[0]
    assert "embed_model" in admin_row
    # Members get the leaner shape; no operational model inventory.
    body = client.get(f"/v1/departments/{code}/documents", headers=member).json()
    assert all("embed_model" not in row for row in body)


def test_corpus_operations_reject_an_inactive_department(env):
    """Soft-disabled means gone from the product — 404, for admins too, matching
    resolve_department in slice 1."""
    client, admin, _member, code = env
    assert client.patch(f"/v1/departments/{code}", json={"is_active": False},
                        headers=admin).status_code == 200

    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 404
    assert client.post(f"/v1/departments/{code}/documents/text",
                       json={"title": "T", "content": "body"},
                       headers=admin).status_code == 404
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=admin).status_code == 404


# Repeated rather than imported from tests/test_rag_departments_api.py: a shared
# fixture across test modules couples two suites that assert different things.
@pytest.fixture()
def levels():
    """A department plus one user at each level, granted by the admin."""
    with TestClient(app) as client:
        admin = _auth(client, "admin@example.com")
        if _me(client, admin).get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        code = f"dlv{uuid.uuid4().hex[:6]}"
        assert client.post(
            "/v1/departments", json={"code": code, "name": "Levels"},
            headers=admin,
        ).status_code == 201
        people = {}
        for level in ("viewer", "editor", "owner"):
            headers = _auth(client, f"docs-{level}-{uuid.uuid4().hex[:8]}@example.com")
            uid = _me(client, headers)["id"]
            assert client.post(
                f"/v1/departments/{code}/members",
                json={"user_id": uid, "role": level},
                headers=admin,
            ).status_code == 204
            people[level] = (headers, uid)
        yield client, admin, code, people


def _typed(client, headers, code, title):
    return client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": title, "content": "some corpus text"},
        headers=headers,
    )


def test_an_editor_can_upload(levels):
    """The whole point of the feature: curating a department no longer requires
    global admin over every other department, the user table and NRB."""
    client, _admin, code, people = levels
    editor, _ = people["editor"]
    resp = _upload(client, editor, code, "editor.csv", CSV)
    assert resp.status_code == 202, resp.text
    assert resp.json()["document_id"]


def test_an_owner_can_upload(levels):
    client, _admin, code, people = levels
    owner, _ = people["owner"]
    resp = _upload(client, owner, code, "owner.csv", CSV)
    assert resp.status_code == 202, resp.text


def test_a_viewer_cannot_upload(levels):
    client, _admin, code, people = levels
    viewer, _ = people["viewer"]
    resp = _upload(client, viewer, code, "viewer.csv", CSV)
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        "Editor access to this department is required"
    )


def test_the_uploader_is_recorded_as_the_editor_not_an_admin(levels):
    """`uploaded_by` used to be the admin's id by construction; it must now be
    whoever actually uploaded."""
    client, _admin, code, people = levels
    editor, editor_id = people["editor"]
    doc_id = _upload(client, editor, code, "who.csv", CSV).json()["document_id"]
    rows = client.get(f"/v1/departments/{code}/documents", headers=editor).json()
    assert any(r["id"] == doc_id for r in rows)


def test_a_viewer_cannot_add_typed_text(levels):
    client, _admin, code, people = levels
    viewer, _ = people["viewer"]
    assert _typed(client, viewer, code, "Nope").status_code == 403


def test_an_editor_can_add_typed_text(levels):
    client, _admin, code, people = levels
    editor, _ = people["editor"]
    assert _typed(client, editor, code, "Editor typed").status_code == 202


def test_a_viewer_cannot_archive(levels):
    client, admin, code, people = levels
    viewer, _ = people["viewer"]
    doc_id = _typed(client, admin, code, "Doc").json()["document_id"]
    assert client.delete(
        f"/v1/departments/{code}/documents/{doc_id}", headers=viewer
    ).status_code == 403


def test_an_editor_can_archive(levels):
    client, admin, code, people = levels
    editor, _ = people["editor"]
    doc_id = _typed(client, admin, code, "Doc2").json()["document_id"]
    assert client.delete(
        f"/v1/departments/{code}/documents/{doc_id}", headers=editor
    ).status_code == 204


def test_a_viewer_sees_only_ready_documents_and_no_admin_fields(levels):
    """A pending or failed document is not part of the corpus a viewer's answers
    can cite, and surfacing it invites "why can't the assistant see this?"."""
    client, admin, code, people = levels
    viewer, _ = people["viewer"]
    _typed(client, admin, code, "Fresh")
    rows = client.get(f"/v1/departments/{code}/documents", headers=viewer).json()
    assert all(r["status"] == "ready" for r in rows)
    assert all("embed_model" not in r for r in rows)


def test_an_editor_sees_non_ready_documents_and_admin_fields(levels):
    client, admin, code, people = levels
    editor, _ = people["editor"]
    doc_id = _typed(client, admin, code, "Fresh2").json()["document_id"]
    rows = client.get(f"/v1/departments/{code}/documents", headers=editor).json()
    assert any(r["id"] == doc_id for r in rows)
    assert all("embed_model" in r for r in rows)


def test_a_viewer_cannot_list_archived(levels):
    client, _admin, code, people = levels
    viewer, _ = people["viewer"]
    resp = client.get(
        f"/v1/departments/{code}/documents?include_archived=true", headers=viewer
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        "Editor access to this department is required"
    )


def test_an_editor_can_list_archived(levels):
    client, admin, code, people = levels
    editor, _ = people["editor"]
    doc_id = _typed(client, admin, code, "ToArchive").json()["document_id"]
    assert client.delete(
        f"/v1/departments/{code}/documents/{doc_id}", headers=admin
    ).status_code == 204
    seen = client.get(
        f"/v1/departments/{code}/documents?include_archived=true", headers=editor
    ).json()
    assert any(r["id"] == doc_id for r in seen)


def test_a_viewer_cannot_download_a_non_ready_document(levels):
    """404, not 403: at document granularity the answer must not reveal that an
    id exists inside a department you can otherwise see."""
    client, admin, code, people = levels
    viewer, _ = people["viewer"]
    doc_id = _typed(client, admin, code, "Fresh3").json()["document_id"]
    resp = client.get(
        f"/v1/departments/{code}/documents/{doc_id}/download", headers=viewer
    )
    assert resp.status_code == 404


def test_an_outsider_is_still_403_at_department_granularity(levels):
    """No grant at all remains a different refusal from a grant that is too
    weak, and it is 403 because the department is not the secret."""
    client, _admin, code, _people = levels
    outsider = _auth(client, f"docs-out-{uuid.uuid4().hex[:8]}@example.com")
    resp = client.get(f"/v1/departments/{code}/documents", headers=outsider)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You do not have access to this department"
