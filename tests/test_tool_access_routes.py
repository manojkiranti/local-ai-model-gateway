"""HTTP tests for the per-user tool access admin routes.

Sign-in goes through the bank directory, which a dev machine often cannot
reach (every `/auth/login`-based route test then skips). So these tests create
their own disposable admin and member rows and stand in for `get_current_user`
with a dependency override that picks the caller by an `X-Test-User` header —
the routes, `require_admin`, the repository and Postgres all run for real.
Skips when Postgres is down.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import Depends, Header
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.config import get_settings
from app.db.session import get_session
from app.main import app
from app.tools.local import LOCAL_TOOLS
from app.users import repository as users_repo

ADMIN_EMAIL = "toolaccess-admin@example.test"
MEMBER_EMAIL = "toolaccess-member@example.test"


def _sql(statement, **params):
    async def run():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return result.scalar() if result.returns_rows else None
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _make_user(email, role):
    _sql("DELETE FROM users WHERE email = :email", email=email)
    return _sql(
        "INSERT INTO users (email, auth_provider, role, is_active) "
        "VALUES (:email, 'ad', :role, true) RETURNING id",
        email=email,
        role=role,
    )


@pytest.fixture
def ids():
    try:
        admin_id = _make_user(ADMIN_EMAIL, "admin")
        member_id = _make_user(MEMBER_EMAIL, "member")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")

    async def as_test_user(
        x_test_user: int = Header(...), session: AsyncSession = Depends(get_session)
    ):
        return await users_repo.get_by_id(session, x_test_user)

    app.dependency_overrides[get_current_user] = as_test_user
    try:
        yield admin_id, member_id
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        # CASCADE removes their overrides with them.
        _sql("DELETE FROM users WHERE email IN (:a, :m)", a=ADMIN_EMAIL, m=MEMBER_EMAIL)


def _as(user_id):
    return {"X-Test-User": str(user_id)}


def _item(body, name):
    return next(i for i in body["items"] if i["name"] == name)


def test_a_new_user_sees_the_starter_set(ids):
    admin_id, uid = ids
    admin = _as(admin_id)
    with TestClient(app) as client:
        body = client.get(f"/v1/users/{uid}/tool-access", headers=admin).json()
        assert {i["name"] for i in body["items"]} == {s.name for s in LOCAL_TOOLS}
        pptx = _item(body, "create_pptx")
        assert pptx["allowed"] and pptx["default_allowed"] and pptx["override"] is None
        fetch = _item(body, "fetch_url")
        assert not fetch["allowed"] and not fetch["default_allowed"]
        assert fetch["group"] == "external"
        groups = {g["id"]: g for g in body["groups"]}
        assert list(groups)[0] == "everyday"
        assert groups["external"]["allowed"] is False and groups["external"]["state"] == "default"
        assert set(groups["external"]["tools"]) == {"fetch_url", "get_nrb_forex"}
        assert groups["create"]["allowed"] is True and groups["create"]["label"] == "Create files"


def test_block_and_grant_then_reset_to_default(ids):
    admin_id, uid = ids
    admin = _as(admin_id)
    with TestClient(app) as client:
        blocked = client.put(
            f"/v1/users/{uid}/tool-access/create_pptx", json={"allowed": False}, headers=admin
        )
        assert blocked.status_code == 200, blocked.text
        row = _item(blocked.json(), "create_pptx")
        assert row["allowed"] is False and row["override"] is False
        assert row["updated_by"] == admin_id

        granted = client.put(
            f"/v1/users/{uid}/tool-access/fetch_url", json={"allowed": True}, headers=admin
        )
        assert _item(granted.json(), "fetch_url")["allowed"] is True

        reset = client.delete(f"/v1/users/{uid}/tool-access/create_pptx", headers=admin)
        assert reset.status_code == 204
        after = client.get(f"/v1/users/{uid}/tool-access", headers=admin).json()
        assert _item(after, "create_pptx")["override"] is None
        assert _item(after, "create_pptx")["allowed"] is True
        # The grant was untouched by resetting a different tool.
        assert _item(after, "fetch_url")["override"] is True


def test_the_member_tool_list_follows_their_access(ids):
    admin_id, uid = ids
    admin, member = _as(admin_id), _as(uid)
    with TestClient(app) as client:
        client.put(f"/v1/users/{uid}/tool-access/create_pptx", json={"allowed": False}, headers=admin)
        resp = client.get("/v1/tools", headers=member)
        if resp.status_code == 502:
            pytest.skip("MCP configured but unreachable")
        exposed = {t["name"] for t in resp.json()["exposed"] if t["backend"] == "local"}
        assert "create_pptx" not in exposed
        assert "fetch_url" not in exposed
        assert "calculator" in exposed
        reasons = {t["name"]: t["reason"] for t in resp.json()["filtered_out"]}
        assert reasons["create_pptx"] == "not permitted for this user"


def test_errors(ids):
    admin_id, uid = ids
    admin, member = _as(admin_id), _as(uid)
    with TestClient(app) as client:
        assert client.get(f"/v1/users/{uid}/tool-access", headers=member).status_code == 403
        assert client.get("/v1/users/999999999/tool-access", headers=admin).status_code == 404
        unknown = client.put(f"/v1/users/{uid}/tool-access/nope", json={"allowed": True}, headers=admin)
        assert unknown.status_code == 404 and "nope" in unknown.text
        extra = client.put(
            f"/v1/users/{uid}/tool-access/calculator", json={"allowed": True, "role": "admin"}, headers=admin
        )
        assert extra.status_code == 422


def _group(body, group_id):
    return next(g for g in body["groups"] if g["id"] == group_id)


def test_a_whole_group_is_switched_and_reset_at_once(ids):
    admin_id, uid = ids
    admin = _as(admin_id)
    with TestClient(app) as client:
        blocked = client.put(f"/v1/users/{uid}/tool-access/groups/create", json={"allowed": False}, headers=admin)
        assert blocked.status_code == 200, blocked.text
        create = _group(blocked.json(), "create")
        assert create["state"] == "blocked" and create["allowed"] is False
        assert create["updated_by"] == admin_id
        for tool in create["tools"]:
            assert _item(blocked.json(), tool)["allowed"] is False
        # Other groups are untouched.
        assert _group(blocked.json(), "everyday")["state"] == "default"

        granted = client.put(f"/v1/users/{uid}/tool-access/groups/external", json={"allowed": True}, headers=admin)
        assert _group(granted.json(), "external")["state"] == "granted"

        reset = client.delete(f"/v1/users/{uid}/tool-access/groups/create", headers=admin)
        assert reset.status_code == 200
        create = _group(reset.json(), "create")
        assert create["state"] == "default" and create["allowed"] is True
        assert create["updated_at"] is None


def test_a_group_with_a_per_tool_override_reads_as_mixed(ids):
    admin_id, uid = ids
    admin = _as(admin_id)
    with TestClient(app) as client:
        body = client.put(
            f"/v1/users/{uid}/tool-access/create_pptx", json={"allowed": False}, headers=admin
        ).json()
        create = _group(body, "create")
        assert create["state"] == "mixed" and create["allowed"] is None


def test_an_unknown_group_is_404(ids):
    admin_id, uid = ids
    with TestClient(app) as client:
        resp = client.put(f"/v1/users/{uid}/tool-access/groups/nope", json={"allowed": True}, headers=_as(admin_id))
        assert resp.status_code == 404 and "nope" in resp.text
        assert client.put(
            f"/v1/users/{uid}/tool-access/groups/create", json={"allowed": True}, headers=_as(uid)
        ).status_code == 403
