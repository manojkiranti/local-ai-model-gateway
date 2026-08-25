"""HTTP tests for the MCP grant admin routes.

A TestClient per test with a local `_auth()` that registers, logs in, and skips
when Postgres is down — the same shape as tests/test_document_upload.py.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.mcp import grants

MEMBER = "mcpgrant-member@example.com"
PASSWORD = "supersecret123"
SEEDED_ADMIN_EMAIL = "admin@example.com"
SEEDED_ADMIN_PASSWORD = "supersecret123"


def _ensure_user(client, email, password):
    """Create the user if absent. `POST /auth/register` is admin-only, so this
    borrows the seeded admin's token exactly as the other route tests do."""
    headers = {}
    if email != SEEDED_ADMIN_EMAIL:
        resp = client.post(
            "/auth/login",
            json={"email": SEEDED_ADMIN_EMAIL, "password": SEEDED_ADMIN_PASSWORD},
        )
        if resp.status_code == 200:
            headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    client.post(
        "/auth/register", json={"email": email, "password": password}, headers=headers
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


def _my_id(client, headers):
    resp = client.get("/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _clear(client, admin, user_id):
    """Leave no grant behind: this table is global with no fixture scope."""
    for key in sorted(grants.ALL_GRANTS):
        client.delete(f"/v1/users/{user_id}/mcp-grants/{key}", headers=admin)


def test_an_unknown_grant_key_is_422_not_a_500_from_the_check():
    """Both the route and the CHECK reject it; only one gives a usable error."""
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        uid = _my_id(client, admin)
        resp = client.post(
            f"/v1/users/{uid}/mcp-grants", json={"grant_key": "mcp-hmrs"}, headers=admin
        )
        assert resp.status_code == 422, resp.text
        assert "mcp-hmrs" in resp.text


def test_an_unexpected_field_is_refused_loudly():
    """extra="forbid", matching UserUpdate: a silently ignored field means the
    caller believes they set something they did not."""
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        uid = _my_id(client, admin)
        resp = client.post(
            f"/v1/users/{uid}/mcp-grants",
            json={"grant_key": grants.ROLE_HRMS, "role": "admin"},
            headers=admin,
        )
        assert resp.status_code == 422, resp.text


def test_granting_appears_in_the_list_and_re_granting_is_still_201():
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        member = _auth(client, MEMBER)
        uid = _my_id(client, member)
        _clear(client, admin, uid)
        try:
            first = client.post(
                f"/v1/users/{uid}/mcp-grants",
                json={"grant_key": grants.ROLE_HRMS},
                headers=admin,
            )
            assert first.status_code == 201, first.text
            keys = [item["grant_key"] for item in first.json()["items"]]
            assert keys == [grants.ROLE_HRMS]
            assert first.json()["items"][0]["granted_by"] == _my_id(client, admin)

            again = client.post(
                f"/v1/users/{uid}/mcp-grants",
                json={"grant_key": grants.ROLE_HRMS},
                headers=admin,
            )
            assert again.status_code == 201, again.text
            # Idempotent, and the audit timestamp is untouched.
            assert again.json()["items"][0]["granted_at"] == (
                first.json()["items"][0]["granted_at"]
            )

            listed = client.get(f"/v1/users/{uid}/mcp-grants", headers=admin)
            assert listed.status_code == 200
            assert [i["grant_key"] for i in listed.json()["items"]] == [grants.ROLE_HRMS]
        finally:
            _clear(client, admin, uid)


def test_revoking_is_idempotent_204():
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        member = _auth(client, MEMBER)
        uid = _my_id(client, member)
        client.post(
            f"/v1/users/{uid}/mcp-grants",
            json={"grant_key": grants.PERM_EMS_QUERY},
            headers=admin,
        )
        first = client.delete(
            f"/v1/users/{uid}/mcp-grants/{grants.PERM_EMS_QUERY}", headers=admin
        )
        second = client.delete(
            f"/v1/users/{uid}/mcp-grants/{grants.PERM_EMS_QUERY}", headers=admin
        )
        assert first.status_code == 204, first.text
        assert second.status_code == 204, second.text


def test_a_member_cannot_read_or_write_grants():
    with TestClient(app) as client:
        member = _auth(client, MEMBER)
        uid = _my_id(client, member)
        assert client.get(f"/v1/users/{uid}/mcp-grants", headers=member).status_code == 403
        assert (
            client.post(
                f"/v1/users/{uid}/mcp-grants",
                json={"grant_key": grants.ROLE_EMS},
                headers=member,
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/v1/users/{uid}/mcp-grants/{grants.ROLE_EMS}", headers=member
            ).status_code
            == 403
        )


def test_an_unauthenticated_caller_is_401():
    with TestClient(app) as client:
        assert client.get("/v1/users/1/mcp-grants").status_code == 401


def test_an_unknown_user_is_404():
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        resp = client.post(
            f"/v1/users/999999999/mcp-grants",
            json={"grant_key": grants.ROLE_HRMS},
            headers=admin,
        )
        assert resp.status_code == 404, resp.text


def test_a_fresh_admin_holds_no_grants():
    """Design §3.4 through the HTTP surface: admin confers the ability to
    grant, never the grants. The single most likely thing a later refactor
    "fixes" back into an implicit bypass."""
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        uid = _my_id(client, admin)
        _clear(client, admin, uid)
        listed = client.get(f"/v1/users/{uid}/mcp-grants", headers=admin)
        assert listed.status_code == 200
        assert listed.json()["items"] == []
