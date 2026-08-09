"""Department admin API. Real Postgres + TestClient; skips if the DB is down.

Follows the auth/skip pattern in tests/test_files_integration.py.
"""

import uuid

import pytest
from starlette.testclient import TestClient

from app.main import app

PASSWORD = "supersecret123"


def _auth(client, email):
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


def _me(client, headers):
    return client.get("/users/me", headers=headers).json()


@pytest.fixture()
def clients():
    """An admin (the project's seeded admin) and a fresh member."""
    with TestClient(app) as client:
        admin = _auth(client, "admin@example.com")
        if _me(client, admin).get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        member_email = f"rag-member-{uuid.uuid4().hex[:8]}@example.com"
        member = _auth(client, member_email)
        yield client, admin, member, _me(client, member)["id"]


def test_member_cannot_create_a_department(clients):
    client, _admin, member, _uid = clients
    resp = client.post("/v1/departments",
                       json={"code": f"x{uuid.uuid4().hex[:6]}", "name": "X"},
                       headers=member)
    assert resp.status_code == 403


def test_admin_creates_and_duplicate_code_is_409(clients):
    client, admin, _member, _uid = clients
    code = f"hr{uuid.uuid4().hex[:6]}"
    first = client.post("/v1/departments", json={"code": code, "name": "HR"},
                        headers=admin)
    assert first.status_code == 201
    assert first.json()["code"] == code and first.json()["is_active"] is True

    dupe = client.post("/v1/departments", json={"code": code, "name": "HR again"},
                       headers=admin)
    assert dupe.status_code == 409


def test_member_sees_only_granted_active_departments(clients):
    client, admin, member, uid = clients
    granted = f"g{uuid.uuid4().hex[:6]}"
    ungranted = f"u{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": granted, "name": "G"}, headers=admin)
    client.post("/v1/departments", json={"code": ungranted, "name": "U"}, headers=admin)

    # Before any grant: neither is visible.
    assert granted not in [d["code"] for d in
                           client.get("/v1/departments", headers=member).json()]

    assert client.post(f"/v1/departments/{granted}/members",
                       json={"user_id": uid}, headers=admin).status_code == 204

    visible = [d["code"] for d in client.get("/v1/departments", headers=member).json()]
    assert granted in visible
    assert ungranted not in visible


def test_soft_disable_hides_a_department_without_revoking_the_grant(clients):
    client, admin, member, uid = clients
    code = f"s{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "S"}, headers=admin)
    client.post(f"/v1/departments/{code}/members", json={"user_id": uid}, headers=admin)
    assert code in [d["code"] for d in
                    client.get("/v1/departments", headers=member).json()]

    patched = client.patch(f"/v1/departments/{code}", json={"is_active": False},
                           headers=admin)
    assert patched.status_code == 200 and patched.json()["is_active"] is False
    assert code not in [d["code"] for d in
                        client.get("/v1/departments", headers=member).json()]

    # The grant survives — the admin listing still shows the member.
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert uid in [m["user_id"] for m in members]


def test_granting_twice_is_idempotent(clients):
    client, admin, _member, uid = clients
    code = f"i{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "I"}, headers=admin)
    for _ in range(2):
        assert client.post(f"/v1/departments/{code}/members",
                           json={"user_id": uid}, headers=admin).status_code == 204
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert len([m for m in members if m["user_id"] == uid]) == 1


def test_revoke_removes_then_404s(clients):
    client, admin, _member, uid = clients
    code = f"r{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "R"}, headers=admin)
    client.post(f"/v1/departments/{code}/members", json={"user_id": uid}, headers=admin)

    assert client.delete(f"/v1/departments/{code}/members/{uid}",
                         headers=admin).status_code == 204
    assert client.delete(f"/v1/departments/{code}/members/{uid}",
                         headers=admin).status_code == 404


def test_unknown_department_is_404_on_grant_and_patch(clients):
    client, admin, _member, uid = clients
    assert client.post("/v1/departments/nope-xyz/members",
                       json={"user_id": uid}, headers=admin).status_code == 404
    assert client.patch("/v1/departments/nope-xyz", json={"is_active": False},
                        headers=admin).status_code == 404


def test_member_cannot_list_members(clients):
    client, admin, member, _uid = clients
    code = f"p{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "P"}, headers=admin)
    assert client.get(f"/v1/departments/{code}/members",
                      headers=member).status_code == 403


def test_departments_require_authentication(clients):
    client, _admin, _member, _uid = clients
    # (401, 403) matches tests/test_protected_endpoints.py — HTTPBearer's
    # no-credentials status has moved between FastAPI versions.
    assert client.get("/v1/departments").status_code in (401, 403)
