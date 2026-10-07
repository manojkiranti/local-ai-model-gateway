"""HTTP tests for the per-user MCP tool access admin routes.

Same harness as test_tool_access_routes.py: disposable admin/member rows and a
`get_current_user` override keyed on an `X-Test-User` header, so the routes,
`require_admin`, the repository and Postgres all run for real. The MCP
server's tool list is stubbed (`mcp_router._live_tools`) so the tests do not
depend on a live MCP server. Skips when Postgres is down.
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
from app.mcp.client import ExcludedTool, ExposedTool
from app.toolaccess import mcp_router
from app.users import repository as users_repo

ADMIN_EMAIL = "mcpaccess-admin@example.test"
MEMBER_EMAIL = "mcpaccess-member@example.test"

LIVE = (
    [
        ExposedTool(name=n, description=f"{n} does a thing. More text.", ollama_schema={})
        for n in ("list_hrms_employees", "get_hrms_employee_remaining_leave", "search_ems_records", "get_echo")
    ],
    [ExcludedTool(name="create_ems_expense", reason="write-like token 'create'")],
)


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
def ids(monkeypatch):
    try:
        admin_id = _make_user(ADMIN_EMAIL, "admin")
        member_id = _make_user(MEMBER_EMAIL, "member")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")

    async def as_test_user(
        x_test_user: int = Header(...), session: AsyncSession = Depends(get_session)
    ):
        return await users_repo.get_by_id(session, x_test_user)

    async def live(_mcp):
        return LIVE

    monkeypatch.setattr(mcp_router, "_live_tools", live)
    app.dependency_overrides[get_current_user] = as_test_user
    try:
        yield admin_id, member_id
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        # CASCADE removes their levels and overrides with them.
        _sql("DELETE FROM users WHERE email IN (:a, :m)", a=ADMIN_EMAIL, m=MEMBER_EMAIL)


def _as(user_id):
    return {"X-Test-User": str(user_id)}


def _tool(body, name):
    return next(t for t in body["tools"] if t["name"] == name)


def _system(body, sid):
    return next(s for s in body["systems"] if s["id"] == sid)


def test_a_new_user_gets_the_read_default(ids):
    admin_id, uid = ids
    with TestClient(app) as client:
        body = client.get(f"/v1/users/{uid}/mcp-access", headers=_as(admin_id)).json()
    assert body["mcp_reachable"] is True
    assert [s["id"] for s in body["systems"]] == ["hrms", "izone", "ems", "other"]
    assert all(s["level"] == "read" and not s["overridden"] for s in body["systems"])
    leave = _tool(body, "get_hrms_employee_remaining_leave")
    assert leave["system"] == "hrms" and leave["kind"] == "read" and leave["allowed"] is True
    assert leave["summary"] == "get_hrms_employee_remaining_leave does a thing."
    # Excluded server-wide: listed so the admin can see it, but nobody gets it.
    create = _tool(body, "create_ems_expense")
    assert create["server_enabled"] is False and create["kind"] == "write" and create["allowed"] is False


def test_a_level_hides_a_system_and_an_override_beats_it(ids):
    admin_id, uid = ids
    admin = _as(admin_id)
    with TestClient(app) as client:
        none = client.put(f"/v1/users/{uid}/mcp-access/systems/hrms", json={"level": "none"}, headers=admin)
        assert none.status_code == 200, none.text
        hrms = _system(none.json(), "hrms")
        assert hrms["level"] == "none" and hrms["overridden"] and hrms["updated_by"] == admin_id
        assert _tool(none.json(), "list_hrms_employees")["allowed"] is False

        granted = client.put(
            f"/v1/users/{uid}/mcp-access/tools/get_hrms_employee_remaining_leave",
            json={"allowed": True},
            headers=admin,
        )
        assert granted.status_code == 200, granted.text
        assert _tool(granted.json(), "get_hrms_employee_remaining_leave")["allowed"] is True
        assert _tool(granted.json(), "list_hrms_employees")["allowed"] is False

        reset = client.delete(f"/v1/users/{uid}/mcp-access/systems/hrms", headers=admin).json()
        assert _system(reset, "hrms")["level"] == "read" and not _system(reset, "hrms")["overridden"]
        cleared = client.delete(
            f"/v1/users/{uid}/mcp-access/tools/get_hrms_employee_remaining_leave", headers=admin
        ).json()
        assert _tool(cleared, "get_hrms_employee_remaining_leave")["override"] is None


def test_full_cannot_exceed_the_server_wide_ceiling(ids):
    admin_id, uid = ids
    with TestClient(app) as client:
        body = client.put(
            f"/v1/users/{uid}/mcp-access/systems/ems", json={"level": "full"}, headers=_as(admin_id)
        ).json()
    assert _tool(body, "create_ems_expense")["allowed"] is False


def test_the_members_tool_list_follows_their_mcp_access(ids):
    admin_id, uid = ids
    _sql(
        "INSERT INTO user_mcp_levels (user_id, system, level) VALUES (:u, 'hrms', 'none')", u=uid
    )
    with TestClient(app) as client:
        state = client.get(f"/v1/users/{uid}/mcp-access", headers=_as(admin_id)).json()
    assert _tool(state, "search_ems_records")["allowed"] is True
    assert not any(t["allowed"] for t in state["tools"] if t["system"] == "hrms")


@pytest.mark.parametrize(
    "method,path,body,expected",
    [
        ("put", "systems/payroll", {"level": "read"}, 404),
        ("put", "systems/hrms", {"level": "admin"}, 422),
        ("put", "systems/hrms", {"level": "read", "extra": 1}, 422),
        ("put", "tools/no_such_tool", {"allowed": True}, 404),
    ],
)
def test_bad_requests_are_refused(ids, method, path, body, expected):
    admin_id, uid = ids
    with TestClient(app) as client:
        response = getattr(client, method)(
            f"/v1/users/{uid}/mcp-access/{path}", json=body, headers=_as(admin_id)
        )
    assert response.status_code == expected, response.text


def test_a_tool_override_is_refused_while_the_mcp_server_is_down(ids, monkeypatch):
    admin_id, uid = ids

    async def down(_mcp):
        return None

    monkeypatch.setattr(mcp_router, "_live_tools", down)
    with TestClient(app) as client:
        refused = client.put(
            f"/v1/users/{uid}/mcp-access/tools/list_hrms_employees",
            json={"allowed": True},
            headers=_as(admin_id),
        )
        assert refused.status_code == 503
        # Levels stay manageable, and the list says why tools are missing.
        body = client.get(f"/v1/users/{uid}/mcp-access", headers=_as(admin_id)).json()
        assert body["mcp_reachable"] is False and body["tools"] == []


def test_a_member_cannot_read_or_change_access(ids):
    _admin_id, uid = ids
    with TestClient(app) as client:
        assert client.get(f"/v1/users/{uid}/mcp-access", headers=_as(uid)).status_code == 403
        assert (
            client.put(
                f"/v1/users/{uid}/mcp-access/systems/hrms", json={"level": "full"}, headers=_as(uid)
            ).status_code
            == 403
        )


def test_unknown_user_is_404(ids):
    admin_id, _uid = ids
    with TestClient(app) as client:
        assert client.get("/v1/users/99999999/mcp-access", headers=_as(admin_id)).status_code == 404
