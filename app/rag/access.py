"""The department permission boundary.

The security invariant, stated precisely: **`department_id` is derived from the
authorized department context — it is not trusted directly from the request
body.** The tab code DOES originate in the request; it becomes trusted only
after this function validates it against `user_departments` and against the
session's own department.

Everything downstream (the contextvar, the retrieval SQL) may assume the
department it receives has already passed through here.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..history.models import ChatSession
from ..users.models import ROLE_ADMIN, User
from . import permissions
from . import repository as repo
from .context import DepartmentContext


async def effective_department_level(
    session: AsyncSession, user: User, dept
) -> str | None:
    """The caller's level in `dept`, or None if they have no access. Never raises.

    The ONE place a level is computed, so the chat boundary and every
    `/v1/departments/*` route agree by construction rather than by two functions
    happening to match.

    A global admin skips the lookup entirely, preserving the existing behaviour
    that admins never touch `user_departments`. `permissions.effective_level` then
    takes the MAXIMUM, so an admin who also holds a weak grant is still an owner.
    """
    is_global_admin = user.role == ROLE_ADMIN
    grant = None
    if not is_global_admin:
        grant = await repo.get_department_level(
            session, user_id=user.id, department_id=dept.id
        )
    return permissions.effective_level(grant, is_global_admin=is_global_admin)


async def _require_grant(session: AsyncSession, user: User, dept) -> str:
    """The caller's level here, or 403. Admins bypass the grant check ONLY.

    Re-checked on every turn, which is what makes revocation take effect on the
    next turn — Postgres stays the live authorization source.

    ANY level may chat: curation is what levels gate, while holding the grant at
    all is what "may ask a question here" means.
    """
    level = await effective_department_level(session, user, dept)
    if level is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this department",
        )
    return level


async def resolve_department(
    session: AsyncSession,
    user: User,
    code: str | None,
    chat_session: ChatSession | None,
) -> DepartmentContext | None:
    """Validate a request-supplied department code for this user and session.

    Returns None for general chat (no department, no RAG). Raises HTTPException
    on every rejection so callers never have to interpret a falsy result.
    """
    # Callers are expected to have loaded the session through an owner-scoped
    # lookup. Re-check anyway — a boundary that assumes every caller did the
    # right thing upstream is one refactor away from a hole. 404 rather than
    # 403, matching GET /v1/files/{id}: don't confirm that a foreign id exists.
    if chat_session is not None and chat_session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    # A brand-new session (None) is NOT the same as an existing session whose
    # department is NULL. Both have no department id, but only the first may be
    # given one — see `is_existing` below.
    is_existing = chat_session is not None
    bound_id = chat_session.department_id if is_existing else None

    if code is None:
        # A bound session continues in ITS OWN department. `department` in the
        # body is required only to OPEN a department chat; on an existing session
        # it is an optional consistency check, never the source of truth — that
        # is `chat_sessions.department_id`, read server-side.
        if bound_id is not None:
            dept = await repo.get_department_by_id(session, bound_id)
            if dept is None or not dept.is_active:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
                )
            await _require_grant(session, user, dept)
            return DepartmentContext(id=dept.id, code=dept.code)
        return None

    dept = await repo.get_department_by_code(session, code)
    # Inactive is 404, not 403, and for admins too: soft-disable means the
    # department is gone from the product, and 403 would confirm it still exists.
    if dept is None or not dept.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown department",
        )

    # Admins bypass the grant check ONLY. They do not bypass 404, the ownership
    # check above, or the session-binding checks below.
    await _require_grant(session, user, dept)

    if is_existing and bound_id is None:
        # An existing GENERAL conversation cannot be adopted into a department:
        # every prior turn was answered without departmental grounding, and
        # relabelling the thread would misrepresent all of them.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This conversation is not a department conversation; "
                   "start a new chat in the department tab.",
        )

    if bound_id is not None and bound_id != dept.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This conversation belongs to a different department",
        )

    return DepartmentContext(id=dept.id, code=dept.code)
