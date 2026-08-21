"""Department admin API. Real Postgres + TestClient; skips if the DB is down.

Follows the auth/skip pattern in tests/test_files_integration.py.
"""

import uuid

import pytest
from starlette.testclient import TestClient

from app.main import app

PASSWORD = "supersecret123"


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

    # The grant survives -- and MEMBERSHIP stays manageable while the department
    # is disabled, because offboarding must not require reactivating a retired
    # department (which would re-expose it as a live tab to everyone left).
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert uid in [m["user_id"] for m in members]

    # Reactivating brings it straight back for the same member, no re-grant.
    assert client.patch(f"/v1/departments/{code}", json={"is_active": True},
                        headers=admin).status_code == 200
    assert code in [d["code"] for d in
                    client.get("/v1/departments", headers=member).json()]


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


@pytest.fixture()
def dept_with_levels(clients):
    """A fresh department plus one user at each level, granted by the admin."""
    client, admin, _member, _uid = clients
    code = f"lv{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/v1/departments", json={"code": code, "name": "Levels"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    people = {}
    for level in ("viewer", "editor", "owner"):
        email = f"rag-{level}-{uuid.uuid4().hex[:8]}@example.com"
        headers = _auth(client, email)
        uid = _me(client, headers)["id"]
        granted = client.post(
            f"/v1/departments/{code}/members",
            json={"user_id": uid, "role": level},
            headers=admin,
        )
        assert granted.status_code == 204, granted.text
        people[level] = (headers, uid)
    return client, admin, code, people


def test_a_global_admin_can_grant_owner(dept_with_levels):
    """The escalation guard must not apply to global admins, or nobody can ever
    create an owner and the feature is unusable."""
    client, admin, code, _people = dept_with_levels
    headers = _auth(client, f"rag-newowner-{uuid.uuid4().hex[:8]}@example.com")
    uid = _me(client, headers)["id"]
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": uid, "role": "owner"},
        headers=admin,
    )
    assert resp.status_code == 204, resp.text


def test_an_owner_can_grant_an_editor(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    headers = _auth(client, f"rag-delegated-{uuid.uuid4().hex[:8]}@example.com")
    uid = _me(client, headers)["id"]
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": uid, "role": "editor"},
        headers=owner,
    )
    assert resp.status_code == 204, resp.text


def test_an_owner_can_grant_by_email_without_reading_the_user_table(dept_with_levels):
    """GET /users is global-admin-only, so email-or-id is what makes delegation
    workable at all."""
    client, _admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    email = f"rag-byemail-{uuid.uuid4().hex[:8]}@example.com"
    _auth(client, email)
    assert client.get("/users", headers=owner).status_code == 403
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"email": email, "role": "viewer"},
        headers=owner,
    )
    assert resp.status_code == 204, resp.text


def test_an_owner_cannot_grant_owner(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    headers = _auth(client, f"rag-wannabe-{uuid.uuid4().hex[:8]}@example.com")
    uid = _me(client, headers)["id"]
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": uid, "role": "owner"},
        headers=owner,
    )
    assert resp.status_code == 403
    assert "global admin" in resp.json()["detail"]


def test_an_owner_cannot_revoke_another_owner(dept_with_levels):
    client, admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    other = _auth(client, f"rag-coowner-{uuid.uuid4().hex[:8]}@example.com")
    other_id = _me(client, other)["id"]
    assert client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": other_id, "role": "owner"},
        headers=admin,
    ).status_code == 204
    resp = client.delete(
        f"/v1/departments/{code}/members/{other_id}", headers=owner
    )
    assert resp.status_code == 403


def test_an_owner_cannot_demote_another_owner(dept_with_levels):
    """Demotion is the same escalation surface as promotion."""
    client, admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    other = _auth(client, f"rag-demote-{uuid.uuid4().hex[:8]}@example.com")
    other_id = _me(client, other)["id"]
    assert client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": other_id, "role": "owner"},
        headers=admin,
    ).status_code == 204
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": other_id, "role": "viewer"},
        headers=owner,
    )
    assert resp.status_code == 403


def test_an_owner_can_revoke_an_editor(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    _editor, editor_id = people["editor"]
    resp = client.delete(
        f"/v1/departments/{code}/members/{editor_id}", headers=owner
    )
    assert resp.status_code == 204


@pytest.mark.parametrize("level", ["viewer", "editor"])
def test_below_owner_cannot_manage_members(dept_with_levels, level):
    client, _admin, code, people = dept_with_levels
    headers, _ = people[level]
    assert client.get(
        f"/v1/departments/{code}/members", headers=headers
    ).status_code == 403
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"email": "nobody@example.com", "role": "viewer"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        "Owner access to this department is required"
    )


def test_an_outsider_cannot_manage_members(dept_with_levels):
    """No grant at all is a different refusal from a grant that is too weak."""
    client, _admin, code, _people = dept_with_levels
    outsider = _auth(client, f"rag-outsider-{uuid.uuid4().hex[:8]}@example.com")
    resp = client.get(f"/v1/departments/{code}/members", headers=outsider)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You do not have access to this department"


def test_the_members_list_carries_emails_and_levels(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    rows = client.get(f"/v1/departments/{code}/members", headers=owner).json()
    by_level = {r["role"]: r for r in rows}
    assert set(by_level) == {"viewer", "editor", "owner"}
    assert all("@" in r["email"] for r in rows)


def test_list_departments_reports_the_callers_level(dept_with_levels):
    client, admin, code, people = dept_with_levels
    for level in ("viewer", "editor", "owner"):
        headers, _ = people[level]
        mine = client.get("/v1/departments", headers=headers).json()
        row = next(d for d in mine if d["code"] == code)
        assert row["role"] == level
    # A global admin sees every department at the effective level owner.
    all_rows = client.get("/v1/departments", headers=admin).json()
    assert next(d for d in all_rows if d["code"] == code)["role"] == "owner"


def test_regranting_changes_the_level(dept_with_levels):
    client, admin, code, people = dept_with_levels
    viewer, viewer_id = people["viewer"]
    assert client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": viewer_id, "role": "editor"},
        headers=admin,
    ).status_code == 204
    mine = client.get("/v1/departments", headers=viewer).json()
    assert next(d for d in mine if d["code"] == code)["role"] == "editor"


def test_an_unknown_level_is_rejected_before_the_database(dept_with_levels):
    client, admin, code, people = dept_with_levels
    _viewer, viewer_id = people["viewer"]
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": viewer_id, "role": "editer"},
        headers=admin,
    )
    assert resp.status_code == 422


def test_granting_without_a_role_still_defaults_to_viewer(dept_with_levels):
    """An existing client that never sent `role` keeps granting what it granted
    before -- that is what makes this migration behaviour-neutral."""
    client, admin, code, _people = dept_with_levels
    headers = _auth(client, f"rag-default-{uuid.uuid4().hex[:8]}@example.com")
    uid = _me(client, headers)["id"]
    assert client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": uid},
        headers=admin,
    ).status_code == 204
    mine = client.get("/v1/departments", headers=headers).json()
    assert next(d for d in mine if d["code"] == code)["role"] == "viewer"


def test_omitting_role_on_a_RE_grant_does_not_demote(dept_with_levels):
    """The regression this closes: `role` defaulting to 'viewer' plus an upsert
    made "field absent" indistinguishable from "set to viewer", so re-adding an
    existing owner silently stripped them to viewer and answered 204. Demotion
    must always be something you asked for."""
    client, admin, code, people = dept_with_levels
    _owner, owner_id = people["owner"]
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": owner_id},
        headers=admin,
    )
    assert resp.status_code == 204, resp.text
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert next(m for m in members if m["user_id"] == owner_id)["role"] == "owner"


def test_omitting_role_on_a_NEW_grant_still_means_viewer(dept_with_levels):
    """Absence means 'do not change the level'; with no level to keep, that is
    least privilege."""
    client, admin, code, _people = dept_with_levels
    headers = _auth(client, f"rag-newabsent-{uuid.uuid4().hex[:8]}@example.com")
    uid = _me(client, headers)["id"]
    assert client.post(
        f"/v1/departments/{code}/members", json={"user_id": uid}, headers=admin
    ).status_code == 204
    mine = client.get("/v1/departments", headers=headers).json()
    assert next(d for d in mine if d["code"] == code)["role"] == "viewer"


def test_an_explicit_demotion_still_works(dept_with_levels):
    """Preserving on absence must not make deliberate demotion impossible."""
    client, admin, code, people = dept_with_levels
    _owner, owner_id = people["owner"]
    assert client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": owner_id, "role": "viewer"},
        headers=admin,
    ).status_code == 204
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert next(m for m in members if m["user_id"] == owner_id)["role"] == "viewer"


def test_an_owner_can_step_down_without_an_admin(dept_with_levels):
    """An owner's own row is theirs: leaving must not require a global admin."""
    client, admin, code, people = dept_with_levels
    owner, owner_id = people["owner"]
    assert client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": owner_id, "role": "editor"},
        headers=owner,
    ).status_code == 204
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert next(m for m in members if m["user_id"] == owner_id)["role"] == "editor"


def test_an_owner_can_revoke_their_own_grant(dept_with_levels):
    client, admin, code, people = dept_with_levels
    owner, owner_id = people["owner"]
    assert client.delete(
        f"/v1/departments/{code}/members/{owner_id}", headers=owner
    ).status_code == 204
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert all(m["user_id"] != owner_id for m in members)


def test_granting_a_deactivated_account_is_refused(dept_with_levels):
    """The old admin-only dropdown filtered deactivated users out; the email path
    cannot, so the refusal has to live on the server or an offboarded account
    reappears as a phantom member."""
    client, admin, code, _people = dept_with_levels
    email = f"rag-gone-{uuid.uuid4().hex[:8]}@example.com"
    headers = _auth(client, email)
    uid = _me(client, headers)["id"]
    assert client.patch(
        f"/users/{uid}", json={"is_active": False}, headers=admin
    ).status_code == 200
    for body in ({"user_id": uid}, {"email": email}):
        resp = client.post(
            f"/v1/departments/{code}/members", json=body, headers=admin
        )
        assert resp.status_code == 409, resp.text
        assert "deactivated" in resp.json()["detail"].lower()


def test_members_are_manageable_on_a_soft_disabled_department(dept_with_levels):
    """Offboarding must not require reactivating a retired department, which would
    re-expose it as a live tab to every remaining member. Grants deliberately
    survive soft-disable, so managing them has to survive it too."""
    client, admin, code, people = dept_with_levels
    _editor, editor_id = people["editor"]
    assert client.patch(
        f"/v1/departments/{code}", json={"is_active": False}, headers=admin
    ).status_code == 200
    listed = client.get(f"/v1/departments/{code}/members", headers=admin)
    assert listed.status_code == 200, listed.text
    assert client.delete(
        f"/v1/departments/{code}/members/{editor_id}", headers=admin
    ).status_code == 204


def test_the_corpus_routes_still_404_on_a_soft_disabled_department(dept_with_levels):
    """The membership exception must NOT leak into corpus operations: a
    soft-disabled department is still gone from the product for documents."""
    client, admin, code, _people = dept_with_levels
    assert client.patch(
        f"/v1/departments/{code}", json={"is_active": False}, headers=admin
    ).status_code == 200
    assert client.get(
        f"/v1/departments/{code}/documents", headers=admin
    ).status_code == 404
    assert client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "T", "content": "c"}, headers=admin,
    ).status_code == 404
