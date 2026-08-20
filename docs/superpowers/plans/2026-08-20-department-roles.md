# Department-Scoped Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each department grant an ordered level — `viewer` < `editor` <
`owner` — so a department can be curated by its own people without anyone being
made a global admin over every department, the user table and the NRB pipeline.

**Architecture:** `user_departments` gains a CHECK-closed `role` column. All
decisions live in one pure module, `app/rag/permissions.py`, which never touches a
session. `repository.has_department_access` becomes `get_department_level`, so the
access check and the level are the same single primary-key lookup; the chat boundary
(`access.resolve_department`) admits any level, while each `/v1/departments/*` route
declares a minimum through one helper, `router._require_level`. The model-facing
`DepartmentContext` contextvar is deliberately untouched.

**Tech Stack:** FastAPI/Starlette, Pydantic v2, SQLAlchemy 2 async + asyncpg,
Alembic, Postgres, pytest. Python 3.10, this repo's `.venv` only.

**Spec:** `docs/superpowers/specs/2026-08-20-department-roles-design.md`

## Global Constraints

- Use **this** checkout's `.venv` for everything (`.venv/bin/pytest`,
  `.venv/bin/alembic`). Never a sibling project's environment.
- Branch is `feat/role`. Do not commit to `main`.
- **Never apply, revert or stamp a migration without the user's explicit
  go-ahead.** Task 2 is gated on it and says so.
- Levels are exactly `viewer`, `editor`, `owner`, ordered weakest to strongest.
  `owner` implies `editor` implies `viewer`.
- **A global admin is `owner`-equivalent for capabilities but is NEVER run through
  the owner escalation rule.** `allows()` takes a level; `grant_refusal()` takes
  `caller_is_global_admin` as its own separate argument. Collapsing them makes
  global admins unable to create owners.
- **Every gate fails closed.** `permissions.allows(None, ...)` and
  `allows("<unknown>", ...)` are both `False`.
- Deploy must be behaviour-neutral: existing grants become `viewer`, which is
  byte-for-byte today's member powers.
- Status codes: no grant → 403; grant too weak → 403 naming the level; escalation
  attempt → 403 with the pure function's `detail`; unknown/inactive department →
  404 (admins included); document or job not visible at your level → **404**, never
  403, so the corpus's shape does not leak.
- `DepartmentContext`, `rag_context` and `search_department_docs` must not change.
  The department is never a tool argument.
- Current Alembic head: `b7e3d95a41c8`. New revision id: `c2f8b1d47e93`.
  `alembic heads` must stay **one** — `tests/test_alembic_lineage.py` fails
  otherwise.
- The `_auth` helpers in the integration tests **skip on auth failure**, so a break
  turns ~86 tests into silent skips a green run hides. Record the skip count before
  Task 1 and compare it at the end of every task.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/rag/permissions.py` | **Create.** Every level decision, pure. Ordering, `allows`, `effective_level`, `grant_refusal`, the refusal message builder. No session, no ORM, no HTTP — unit-testable without Postgres. |
| `alembic/versions/c2f8b1d47e93_department_roles.py` | **Create.** `user_departments.role` + `ck_user_departments_role` + `ck_users_role`. |
| `app/rag/models.py` | `UserDepartment.role` and its CHECK. |
| `app/users/models.py` | `ck_users_role` — closes a vocabulary that was open. |
| `app/rag/repository.py` | `get_department_level` replaces `has_department_access`; `grant_department` gains `role` and upserts; `list_department_members` and `list_departments_for_user` carry the level (and the member's email). |
| `app/rag/access.py` | `effective_department_level` — the one place a level is computed. `_require_grant` becomes a thin raiser over it. |
| `app/rag/router.py` | `_require_level` replaces `_require_department_access`; every route declares its minimum. |
| `app/rag/schemas.py` | `DepartmentOut.role`, `MemberOut.email`/`role`, `GrantCreate.role`. |
| `app/rag/jobs_router.py` | Editor-of-the-job's-department instead of global admin. |
| `tests/test_department_permissions.py` | **Create.** The security proof, no database. |

**Dependency order:** Task 1 (pure) → Task 2 (schema) → Task 3 (repository) →
Task 4 (boundary) → Tasks 5, 6, 7 (routes, independent of each other) → Task 8
(docs).

---

### Task 1: The pure permission policy

Mirrors `app/users/policy.py`: the guard *is* the security property, and the
escalation branch cannot be safely rehearsed against a real database.

**Files:**
- Create: `app/rag/permissions.py`
- Test: `tests/test_department_permissions.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `LEVEL_VIEWER = "viewer"`, `LEVEL_EDITOR = "editor"`, `LEVEL_OWNER = "owner"`
  - `LEVELS: tuple[str, ...]` — ordered weakest to strongest
  - `allows(level: str | None, required: str) -> bool`
  - `effective_level(grant: str | None, *, is_global_admin: bool) -> str | None`
  - `insufficient_level(required: str) -> str`
  - `grant_refusal(*, caller_level: str | None, caller_is_global_admin: bool, requested_level: str | None, existing_target_level: str | None) -> str | None`
  - `OWNER_CANNOT_SET_OWNER: str`, `OWNER_CANNOT_CHANGE_OWNER: str`

- [ ] **Step 1: Record the baseline skip count**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Write the passed/skipped/failed numbers into the commit message of Step 6. Every
later task compares against them.

- [ ] **Step 2: Write the failing test**

Create `tests/test_department_permissions.py`:

```python
"""Department level policy — pure, no database.

These are the security properties, so they are proven here rather than through the
API: `grant_refusal`'s escalation branch decides who can mint owners, and asserting
it against a real database would mean arranging a department with a hostile owner
in it. The pure function is exhaustible instead.
"""

import itertools

import pytest

from app.rag import permissions as perms

ALL_LEVELS = (perms.LEVEL_VIEWER, perms.LEVEL_EDITOR, perms.LEVEL_OWNER)
# `None` = no grant at all, which is a distinct case from every level.
LEVELS_AND_NONE = (None,) + ALL_LEVELS


def test_levels_are_ordered_weakest_to_strongest():
    assert perms.LEVELS == (
        perms.LEVEL_VIEWER,
        perms.LEVEL_EDITOR,
        perms.LEVEL_OWNER,
    )


@pytest.mark.parametrize(
    "level,required,expected",
    [
        ("viewer", "viewer", True),
        ("viewer", "editor", False),
        ("viewer", "owner", False),
        ("editor", "viewer", True),
        ("editor", "editor", True),
        ("editor", "owner", False),
        ("owner", "viewer", True),
        ("owner", "editor", True),
        ("owner", "owner", True),
    ],
)
def test_allows_is_inclusive_of_stronger_levels(level, required, expected):
    assert perms.allows(level, required) is expected


def test_no_grant_allows_nothing():
    for required in ALL_LEVELS:
        assert perms.allows(None, required) is False


def test_an_unrecognised_level_fails_closed():
    """ck_user_departments_role should make this unreachable. If a value escapes
    the constraint it must allow NOTHING, never compare as rank 0 and pass the
    viewer check."""
    for required in ALL_LEVELS:
        assert perms.allows("superuser", required) is False
        assert perms.allows("", required) is False
    assert perms.allows("owner", "administrator") is False


def test_a_global_admin_is_owner_equivalent():
    assert perms.effective_level(None, is_global_admin=True) == perms.LEVEL_OWNER


def test_a_global_admin_keeps_owner_powers_despite_a_weaker_grant():
    """Being granted a department must never COST an admin a capability, so the
    effective level is the maximum of the two, not the grant."""
    assert (
        perms.effective_level(perms.LEVEL_VIEWER, is_global_admin=True)
        == perms.LEVEL_OWNER
    )


def test_a_member_without_a_grant_has_no_level():
    assert perms.effective_level(None, is_global_admin=False) is None


def test_a_members_effective_level_is_their_grant():
    for level in ALL_LEVELS:
        assert perms.effective_level(level, is_global_admin=False) == level


def test_an_unrecognised_grant_is_no_access():
    assert perms.effective_level("superuser", is_global_admin=False) is None


def test_a_global_admin_is_never_refused_a_membership_change():
    """THE regression this locks: if `grant_refusal` ever derives the caller's
    ownership from `effective_level`, "an owner cannot grant owner" starts applying
    to global admins too and nobody can create an owner at all."""
    for requested, existing in itertools.product(LEVELS_AND_NONE, LEVELS_AND_NONE):
        assert (
            perms.grant_refusal(
                caller_level=None,
                caller_is_global_admin=True,
                requested_level=requested,
                existing_target_level=existing,
            )
            is None
        )


@pytest.mark.parametrize("requested", ["viewer", "editor"])
def test_an_owner_may_grant_viewer_and_editor(requested):
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level=requested,
            existing_target_level=None,
        )
        is None
    )


def test_an_owner_may_not_grant_owner():
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level=perms.LEVEL_OWNER,
            existing_target_level=None,
        )
        == perms.OWNER_CANNOT_SET_OWNER
    )


@pytest.mark.parametrize("requested", [None, "viewer", "editor"])
def test_an_owner_may_neither_demote_nor_revoke_another_owner(requested):
    """Demotion is the same escalation surface as promotion, and revocation
    (requested_level=None) is the lateral case: owner A evicting owner B."""
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level=requested,
            existing_target_level=perms.LEVEL_OWNER,
        )
        == perms.OWNER_CANNOT_CHANGE_OWNER
    )


@pytest.mark.parametrize("existing", [None, "viewer", "editor"])
def test_an_owner_may_revoke_a_viewer_or_editor(existing):
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level=None,
            existing_target_level=existing,
        )
        is None
    )


@pytest.mark.parametrize("caller", [None, "viewer", "editor"])
def test_below_owner_may_not_manage_members_at_all(caller):
    for requested in LEVELS_AND_NONE:
        assert perms.grant_refusal(
            caller_level=caller,
            caller_is_global_admin=False,
            requested_level=requested,
            existing_target_level=None,
        ) == perms.insufficient_level(perms.LEVEL_OWNER)


def test_the_refusal_message_names_the_level_required():
    assert perms.insufficient_level(perms.LEVEL_EDITOR) == (
        "Editor access to this department is required"
    )
    assert perms.insufficient_level(perms.LEVEL_OWNER) == (
        "Owner access to this department is required"
    )
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_department_permissions.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.rag.permissions'`

- [ ] **Step 4: Write the implementation**

Create `app/rag/permissions.py`:

```python
"""Pure decisions about what a granted user may DO inside a department.

`user_departments` says WHO may reach a department; this module says at what level.
Kept pure (no session, no ORM, no HTTP) for the same reason as
`app/users/policy.py`: the escalation rule below is the security property, and
proving it through the API would mean arranging a department with a hostile owner
in it. Here it is exhaustible.

Everything fails closed. `None` (no grant) and any unrecognised string allow
nothing — `ck_user_departments_role` should make the latter unreachable, but a
value that escaped the constraint must not compare as rank 0 and pass the viewer
check.
"""

from __future__ import annotations

LEVEL_VIEWER = "viewer"
LEVEL_EDITOR = "editor"
LEVEL_OWNER = "owner"

# Ordered weakest -> strongest. The index IS the rank, which is what makes
# "at least editor" one comparison at every call site instead of set arithmetic.
LEVELS: tuple[str, ...] = (LEVEL_VIEWER, LEVEL_EDITOR, LEVEL_OWNER)
_RANK = {level: rank for rank, level in enumerate(LEVELS)}

# Refusal messages. Returned as an HTTP 403 `detail` and rendered verbatim by the
# frontend — the convention `PATCH /users/{id}`'s 409 already sets.
OWNER_CANNOT_SET_OWNER = (
    "Only a global admin can grant owner access to a department"
)
OWNER_CANNOT_CHANGE_OWNER = (
    "Only a global admin can change or revoke another owner's access"
)


def insufficient_level(required: str) -> str:
    """The 403 detail for a caller whose grant is too weak.

    Naming the level is safe here and unsafe nowhere: the caller already holds a
    grant, so the department's existence is not a secret being leaked. Where
    existence IS the secret — a document or a job — the answer is 404 instead.
    """
    return f"{required.capitalize()} access to this department is required"


def allows(level: str | None, required: str) -> bool:
    """Does `level` meet or exceed `required`?"""
    if level not in _RANK or required not in _RANK:
        return False
    return _RANK[level] >= _RANK[required]


def effective_level(grant: str | None, *, is_global_admin: bool) -> str | None:
    """The level in force for this caller here, or None for no access at all.

    A global admin is owner-equivalent for CAPABILITIES. This takes the maximum
    rather than overwriting, so an admin who also holds a `viewer` grant is still
    an owner — being granted a department must never cost an admin a capability.

    Deliberately NOT an input to `grant_refusal`; see the warning there.
    """
    if is_global_admin:
        return LEVEL_OWNER
    return grant if grant in _RANK else None


def grant_refusal(
    *,
    caller_level: str | None,
    caller_is_global_admin: bool,
    requested_level: str | None,
    existing_target_level: str | None,
) -> str | None:
    """Why this membership change is refused, or None if it may proceed.

    `requested_level=None` means revocation.

    THE TRAP THIS FUNCTION EXISTS FOR: a global admin is owner-equivalent for
    capabilities (`effective_level`) but must NOT be run through the owner
    escalation rule. Collapse the two and "an owner cannot grant owner" also stops
    global admins from creating owners — the feature becomes unusable, and whoever
    debugs it deletes the guard. So `caller_is_global_admin` is its own argument,
    checked FIRST, and callers pass both facts rather than deriving one from the
    other. `test_a_global_admin_is_never_refused_a_membership_change` locks it.
    """
    if caller_is_global_admin:
        return None

    # Re-checked here even though `router._require_level` already gated the route:
    # a pure policy that depends on its caller having been careful is not a policy.
    if not allows(caller_level, LEVEL_OWNER):
        return insufficient_level(LEVEL_OWNER)

    # An owner delegates viewer and editor only. Bounding the chain at depth 1
    # keeps "who can mint peers" with global admin, mirroring the existing rule
    # that `role` is not patchable through `PATCH /users/{id}`.
    if requested_level == LEVEL_OWNER:
        return OWNER_CANNOT_SET_OWNER

    # Demotion is the same escalation surface as promotion, and revocation is the
    # lateral case — owner A evicting owner B — so an existing owner row is
    # untouchable either way. No "last owner" guard is needed: global admin is
    # always the backstop, unlike `LAST_ADMIN` in app/users/policy.py.
    if existing_target_level == LEVEL_OWNER:
        return OWNER_CANNOT_CHANGE_OWNER

    return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_department_permissions.py -q`
Expected: PASS, no skips (this file never touches Postgres).

- [ ] **Step 6: Commit**

```bash
git add app/rag/permissions.py tests/test_department_permissions.py
git commit -m "feat(roles): the pure department level policy

viewer < editor < owner, with the ordering as the rank so 'at least
editor' is one comparison at every call site.

The trap the module is shaped around: a global admin is owner-equivalent
for capabilities but must not be run through the owner escalation rule.
Collapse them and 'an owner cannot grant owner' stops global admins from
creating owners too, at which point the feature is unusable and whoever
debugs it deletes the guard. caller_is_global_admin is therefore its own
argument, checked first.

Everything fails closed: no grant and any unrecognised level allow
nothing, so a value that escaped ck_user_departments_role cannot compare
as rank 0 and pass the viewer check."
```

---

### Task 2: Schema — the level column and two closed vocabularies

**Files:**
- Create: `alembic/versions/c2f8b1d47e93_department_roles.py`
- Modify: `app/rag/models.py` (the `UserDepartment` class)
- Modify: `app/users/models.py` (`User.__table_args__`)
- Test: `tests/test_rag_schema_integration.py`

**Interfaces:**
- Consumes: `app.rag.permissions.LEVEL_VIEWER` (Task 1) — for the test only; the
  model and migration use SQL literals so the DDL never depends on Python.
- Produces: `user_departments.role` (`varchar(16) NOT NULL DEFAULT 'viewer'`),
  constraints `ck_user_departments_role` and `ck_users_role`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_schema_integration.py`:

```python
def test_user_departments_role_defaults_to_the_weakest_level():
    """Least privilege on omission: an insert that forgets the level must not
    quietly create an editor. This default is also what makes the migration
    backfill-free."""

    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept_id = await _make_department(s)
                user_id = await _make_user(s)
                await s.execute(
                    text(
                        "INSERT INTO user_departments (user_id, department_id) "
                        "VALUES (:u, :d)"
                    ),
                    {"u": user_id, "d": dept_id},
                )
                got = (
                    await s.execute(
                        text(
                            "SELECT role FROM user_departments "
                            "WHERE user_id = :u AND department_id = :d"
                        ),
                        {"u": user_id, "d": dept_id},
                    )
                ).scalar_one()
                assert got == "viewer"
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_ck_user_departments_role_rejects_a_typo():
    """The vocabulary is closed because every gate compares this exact string. A
    typo'd level would be a level that allows nothing, silently — the same reason
    ck_documents_status exists."""

    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept_id = await _make_department(s)
                user_id = await _make_user(s)
                with pytest.raises(IntegrityError):
                    await s.execute(
                        text(
                            "INSERT INTO user_departments "
                            "(user_id, department_id, role) VALUES (:u, :d, 'editer')"
                        ),
                        {"u": user_id, "d": dept_id},
                    )
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_ck_users_role_rejects_a_typo():
    """users.role had NO check constraint, unlike auth_provider and
    documents.status. An unrecognised role is silently a non-admin."""

    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                with pytest.raises(IntegrityError):
                    await s.execute(
                        text(
                            "INSERT INTO users "
                            "(email, auth_provider, password_hash, role, is_active)"
                            " VALUES (:e, 'local', 'x', 'administrator', true)"
                        ),
                        {"e": f"ckrole-{uuid.uuid4().hex[:8]}@example.com"},
                    )
        finally:
            await engine.dispose()

    asyncio.run(go())
```

Read the top of `tests/test_rag_schema_integration.py` first and reuse its existing
`_engine`, `_make_department` and `_make_user` helpers verbatim. If a helper does
not exist under that name, use whatever the file already uses to create a
department and a user — do not add a second helper doing the same job. Add
`import uuid`, `import pytest`, `from sqlalchemy import text` and
`from sqlalchemy.exc import IntegrityError` only if they are not already imported.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_schema_integration.py -q -k "role"`
Expected: FAIL — `column "role" of relation "user_departments" does not exist`
for the first two, and no `IntegrityError` raised for the third.

If instead every test SKIPS, Postgres is not reachable. Fix that before continuing
— this task cannot be verified without it.

- [ ] **Step 3: Add the column and both constraints to the models**

In `app/rag/models.py`, give `UserDepartment` a `__table_args__` (it currently has
none) and the new column. Insert `__table_args__` immediately after the docstring,
and the column after `department_id`:

```python
    __tablename__ = "user_departments"
    __table_args__ = (
        # Closed vocabulary, same rule as ck_documents_status: every gate compares
        # this exact string, so an unrecognised value is not cosmetic — it is a
        # level that allows nothing (permissions.allows fails closed). Adding a
        # level means editing this CHECK.
        CheckConstraint(
            "role IN ('viewer', 'editor', 'owner')",
            name="ck_user_departments_role",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True
    )
    # What the holder may DO here: viewer < editor < owner (app/rag/permissions.py).
    # Defaulting to the weakest level is least privilege on omission, and it is what
    # let the migration backfill every pre-existing grant without a data step.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'viewer'")
    )
```

`CheckConstraint`, `String` and `text` are already imported in this module; confirm
rather than assuming.

In `app/users/models.py`, add a third constraint to `User.__table_args__`, after
`ck_users_credential`:

```python
        # users.role had no CHECK while auth_provider and documents.status both
        # did. `require_admin` compares this exact string, so an unrecognised value
        # is silently a non-admin — a privilege bug that reads as a typo.
        CheckConstraint(
            "role IN ('admin', 'member')",
            name="ck_users_role",
        ),
```

- [ ] **Step 4: Write the migration by hand**

Autogenerate would miss the `server_default` intent and propose dropping the
hand-written HNSW/GIN indexes. Create
`alembic/versions/c2f8b1d47e93_department_roles.py`:

```python
"""department-scoped roles: a level on every grant, and two closed vocabularies

`user_departments.role` says what a granted user may DO in a department, ordered
viewer < editor < owner (app/rag/permissions.py). Before this, a grant was binary
and the only way to let someone curate their own department's corpus was to make
them a GLOBAL admin — over every other department, the user table and the NRB
pipeline.

NOT NULL DEFAULT 'viewer' backfills every existing grant in the ALTER itself, and
'viewer' is exactly what a granted member could already do (curation was
admin-only), so this migration is behaviour-neutral: nobody gains or loses a
capability until an admin sets a level.

The default STAYS after the backfill. Least privilege is the right failure mode for
an insert that forgets the level.

`ck_users_role` closes a vocabulary that was open: `require_admin` compares
`users.role` to the exact string 'admin', so an unrecognised value is silently a
non-admin. Same rule as ck_users_auth_provider and ck_documents_status. It is not
backfillable, and should not be — if the ALTER fails, a row already violates the
rule and wants a human. Check before upgrading:

    SELECT id, email, role FROM users WHERE role NOT IN ('admin', 'member');

Every row was created by `POST /auth/register` or directory provisioning, both of
which write 'admin' or 'member', so the expected result is zero rows.

Revision ID: c2f8b1d47e93
Revises: b7e3d95a41c8
Create Date: 2026-08-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2f8b1d47e93"
down_revision: Union[str, None] = "b7e3d95a41c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_departments",
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'viewer'"),
        ),
    )
    op.create_check_constraint(
        "ck_user_departments_role",
        "user_departments",
        "role IN ('viewer', 'editor', 'owner')",
    )
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_constraint(
        "ck_user_departments_role", "user_departments", type_="check"
    )
    op.drop_column("user_departments", "role")
```

- [ ] **Step 5: Verify the lineage stays linear BEFORE applying anything**

```bash
.venv/bin/alembic heads
.venv/bin/pytest tests/test_alembic_lineage.py -q
```

Expected: exactly one head, `c2f8b1d47e93`, and the lineage test passes. If two
heads appear, the new revision was written beside the head instead of on it — fix
`down_revision`, do not merge.

- [ ] **Step 6: STOP and ask the user before applying the migration**

Do not run `alembic upgrade`. Report the pre-flight query from the migration
docstring and ask for an explicit go-ahead. On approval:

```bash
psql "$DATABASE_URL" -c "SELECT id, email, role FROM users WHERE role NOT IN ('admin','member');"
.venv/bin/alembic upgrade head
DATABASE_URL=<...>/local_ai_gateway_p4 .venv/bin/alembic upgrade head
.venv/bin/alembic current
```

Both databases must end at `c2f8b1d47e93`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_schema_integration.py tests/test_alembic_lineage.py -q`
Expected: PASS. Then the full suite, comparing against Task 1's baseline:
`.venv/bin/pytest -q 2>&1 | tail -3` — the **skip count must not have risen**.

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/c2f8b1d47e93_department_roles.py app/rag/models.py app/users/models.py tests/test_rag_schema_integration.py
git commit -m "feat(roles): user_departments.role, plus two closed vocabularies

The level column backfills to 'viewer' inside the ALTER, and 'viewer' is
exactly what a granted member could already do, so the migration is
behaviour-neutral. The DEFAULT stays: least privilege is the right
failure mode for an insert that forgets the level.

ck_users_role closes a vocabulary that was open while auth_provider and
documents.status were both closed. require_admin compares users.role to
the exact string 'admin', so an unrecognised value is silently a
non-admin -- a privilege bug that reads as a typo."
```

---

### Task 3: Repository — the level is the access check

**Files:**
- Modify: `app/rag/repository.py`
- Test: `tests/test_rag_repository_integration.py`

**Interfaces:**
- Consumes: `permissions.LEVEL_VIEWER` (Task 1); `user_departments.role` (Task 2).
- Produces:
  - `get_department_level(session, *, user_id: int, department_id: int) -> str | None`
    — **replaces** `has_department_access`, which is deleted.
  - `grant_department(session, *, user_id, department_id, granted_by, role: str = LEVEL_VIEWER) -> None`
  - `list_department_members(session, department_id) -> list[Row]` with attributes
    `user_id, department_id, role, granted_by, granted_at, email`
  - `list_departments_for_user(session, user_id) -> list[Row]` with attributes
    `Department, role` (unpack as `for dept, role in rows`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_repository_integration.py`, reusing that file's existing
engine/session helper rather than adding another:

```python
def test_get_department_level_returns_the_grant_level_or_none():
    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                user_id = await _make_user(s)
                assert (
                    await repo.get_department_level(
                        s, user_id=user_id, department_id=dept.id
                    )
                    is None
                )
                await repo.grant_department(
                    s, user_id=user_id, department_id=dept.id,
                    granted_by=None, role="editor",
                )
                await s.commit()
                assert (
                    await repo.get_department_level(
                        s, user_id=user_id, department_id=dept.id
                    )
                    == "editor"
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_grant_department_defaults_to_viewer():
    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                user_id = await _make_user(s)
                await repo.grant_department(
                    s, user_id=user_id, department_id=dept.id, granted_by=None
                )
                await s.commit()
                assert (
                    await repo.get_department_level(
                        s, user_id=user_id, department_id=dept.id
                    )
                    == "viewer"
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_regranting_CHANGES_the_level_rather_than_doing_nothing():
    """This route is also promote/demote. on_conflict_do_nothing would answer 204
    while silently leaving the old level in place -- the worst possible outcome for
    a permission change."""

    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                user_id = await _make_user(s)
                await repo.grant_department(
                    s, user_id=user_id, department_id=dept.id,
                    granted_by=None, role="viewer",
                )
                await s.commit()
                await repo.grant_department(
                    s, user_id=user_id, department_id=dept.id,
                    granted_by=None, role="owner",
                )
                await s.commit()
                assert (
                    await repo.get_department_level(
                        s, user_id=user_id, department_id=dept.id
                    )
                    == "owner"
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_list_department_members_carries_the_email_and_level():
    """An owner managing members sees emails, not bare integers -- GET /users is
    global-admin-only, so without this the members screen is unusable for them."""

    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                user_id = await _make_user(s)
                await repo.grant_department(
                    s, user_id=user_id, department_id=dept.id,
                    granted_by=None, role="editor",
                )
                await s.commit()
                rows = await repo.list_department_members(s, dept.id)
                assert len(rows) == 1
                assert rows[0].user_id == user_id
                assert rows[0].role == "editor"
                assert "@" in rows[0].email
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_list_departments_for_user_carries_the_level():
    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                user_id = await _make_user(s)
                await repo.grant_department(
                    s, user_id=user_id, department_id=dept.id,
                    granted_by=None, role="owner",
                )
                await s.commit()
                rows = await repo.list_departments_for_user(s, user_id)
                assert [(d.code, level) for d, level in rows] == [
                    (dept.code, "owner")
                ]
        finally:
            await engine.dispose()

    asyncio.run(go())
```

`_code()` should produce a fresh unique department code (`f"t{uuid.uuid4().hex[:8]}"`)
— reuse the file's existing helper if it has one.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_repository_integration.py -q -k "level or member or regranting"`
Expected: FAIL — `AttributeError: module 'app.rag.repository' has no attribute 'get_department_level'`

- [ ] **Step 3: Replace `has_department_access` with `get_department_level`**

In `app/rag/repository.py`, delete `has_department_access` entirely and add:

```python
async def get_department_level(
    session: AsyncSession, *, user_id: int, department_id: int
) -> str | None:
    """This user's level in this department, or None if they hold no grant.

    Replaces `has_department_access`: a non-None answer IS the access check, so the
    boundary and the level cost the same single primary-key lookup that slice 3
    measured at 0.518 ms. `access.resolve_department` calls this on EVERY chat
    turn — never widen it into a join.
    """
    found = (
        await session.execute(
            select(UserDepartment.role).where(
                UserDepartment.user_id == user_id,
                UserDepartment.department_id == department_id,
            )
        )
    ).first()
    return None if found is None else found[0]
```

- [ ] **Step 4: Make `grant_department` an upsert that carries the level**

Replace the body of `grant_department`:

```python
async def grant_department(
    session: AsyncSession,
    *,
    user_id: int,
    department_id: int,
    granted_by: int | None,
    role: str = LEVEL_VIEWER,
) -> None:
    """Grant access at `role`, or change an existing grant's level.

    `on_conflict_do_UPDATE`, not `do_nothing`: this is also the promote/demote
    route, and a no-op would answer 204 while leaving the old level in place —
    the worst possible outcome for a permission change. Still idempotent, so an
    admin clicking twice does not produce a 500.

    `granted_by`/`granted_at` are refreshed with the level, so the row answers
    "who put them at THIS level, and when" rather than "who first let them in".
    That is the fact an audit of a privilege change actually wants.
    """
    stmt = pg_insert(UserDepartment).values(
        user_id=user_id,
        department_id=department_id,
        granted_by=granted_by,
        role=role,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "department_id"],
        set_={
            "role": stmt.excluded.role,
            "granted_by": stmt.excluded.granted_by,
            "granted_at": func.now(),
        },
    )
    await session.execute(stmt)
```

Add the imports this needs at the top of the file: `func` to the existing
`from sqlalchemy import ...` line, and

```python
from ..users.models import User
from .permissions import LEVEL_VIEWER
```

`app/rag/models.py` already imports `..users.models`, so this introduces no new
cycle.

- [ ] **Step 5: Carry the email and the level on the two list queries**

Replace `list_department_members`:

```python
async def list_department_members(
    session: AsyncSession, department_id: int
) -> list[Row]:
    """Members with their level and email.

    The email is here because `GET /users` is global-admin-only: a department owner
    managing members would otherwise see bare integers and have no way to resolve
    them. Rows carry attribute access, so the router can validate them straight
    into `MemberOut`.
    """
    result = await session.execute(
        select(
            UserDepartment.user_id,
            UserDepartment.department_id,
            UserDepartment.role,
            UserDepartment.granted_by,
            UserDepartment.granted_at,
            User.email,
        )
        .join(User, User.id == UserDepartment.user_id)
        .where(UserDepartment.department_id == department_id)
        .order_by(UserDepartment.user_id)
    )
    return list(result.all())
```

and the query inside `list_departments_for_user` (keep its docstring, add a line):

```python
    result = await session.execute(
        select(Department, UserDepartment.role)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id, Department.is_active.is_(True))
        .order_by(Department.code)
    )
    return list(result.all())
```

Change its return annotation to `list[Row]` and note in the docstring that each row
is `(Department, role)`. Add `from sqlalchemy import Row` to the imports (or
`from sqlalchemy.engine import Row`, whichever this SQLAlchemy version exposes —
check with `.venv/bin/python -c "from sqlalchemy import Row; print(Row)"`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_repository_integration.py -q`
Expected: PASS.

Then find every remaining caller of the deleted function — there must be none:

```bash
grep -rn "has_department_access" app/ tests/
```

Expected: no output. `app/rag/access.py` and `app/rag/router.py` still reference it
at this point, so this grep is what tells you Task 4 is required before the suite
is green. Run `.venv/bin/pytest -q 2>&1 | tail -3` and expect failures in the
department tests — that is correct mid-sequence.

- [ ] **Step 7: Commit**

```bash
git add app/rag/repository.py tests/test_rag_repository_integration.py
git commit -m "feat(roles): the level IS the access check

get_department_level replaces has_department_access: a non-None answer is
the boundary, so the check and the level cost the one primary-key lookup
slice 3 measured at 0.518 ms. resolve_department calls it every turn.

grant_department upserts rather than do_nothing, because this is also the
promote/demote route -- a no-op would answer 204 while leaving the old
level in place. Member rows carry email because GET /users is
global-admin-only, so an owner managing members would otherwise see bare
integers with no way to resolve them.

Leaves app/rag/{access,router}.py referencing the deleted function; the
next commit is what makes the suite green."
```

---

### Task 4: The boundary — one place computes a level

**Files:**
- Modify: `app/rag/access.py`
- Test: `tests/test_rag_access_integration.py`

**Interfaces:**
- Consumes: `repo.get_department_level` (Task 3); `permissions.effective_level`
  (Task 1).
- Produces:
  `effective_department_level(session, user: User, dept) -> str | None` — the ONLY
  place a level is computed. Used by `access._require_grant` and, in Task 5, by
  `router._require_level`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_access_integration.py`, reusing its existing helpers:

```python
def test_every_level_can_chat_in_its_department():
    """The chat boundary admits ANY level. Curation is what levels gate; being
    able to ask a question is what the grant itself means."""

    async def go():
        engine, Session = _engine()
        try:
            for level in ("viewer", "editor", "owner"):
                async with Session() as s:
                    dept = await repo.create_department(s, code=_code(), name="Ops")
                    user = await _make_user_row(s)
                    await repo.grant_department(
                        s, user_id=user.id, department_id=dept.id,
                        granted_by=None, role=level,
                    )
                    await s.commit()
                    ctx = await access.resolve_department(
                        s, user, dept.code, None
                    )
                    assert ctx is not None and ctx.code == dept.code
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_effective_level_reports_owner_for_a_global_admin_without_a_grant():
    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                admin = await _make_user_row(s, role="admin")
                assert (
                    await access.effective_department_level(s, admin, dept)
                    == "owner"
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_effective_level_is_none_without_a_grant():
    async def go():
        engine, Session = _engine()
        try:
            async with Session() as s:
                dept = await repo.create_department(s, code=_code(), name="Ops")
                user = await _make_user_row(s)
                assert (
                    await access.effective_department_level(s, user, dept) is None
                )
        finally:
            await engine.dispose()

    asyncio.run(go())
```

`_make_user_row(s, role="member")` must return a `User` ORM object (the existing
file may already have an equivalent — reuse it). Remember
`ck_users_credential`: a `local` user MUST be inserted with a non-NULL
`password_hash`, or the insert violates the constraint.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_access_integration.py -q`
Expected: FAIL — `AttributeError: module 'app.rag.access' has no attribute
'effective_department_level'`, plus errors from `repo.has_department_access` no
longer existing.

- [ ] **Step 3: Add the one level-computing function and rewire `_require_grant`**

In `app/rag/access.py`, replace `_require_grant` with:

```python
async def effective_department_level(
    session: AsyncSession, user: User, dept
) -> str | None:
    """The caller's level in `dept`, or None if they have no access. Never raises.

    The ONE place a level is computed, so the chat boundary and every
    `/v1/departments/*` route agree by construction rather than by two functions
    happening to match.

    A global admin skips the lookup entirely, preserving today's behaviour that
    admins never touch `user_departments`. `permissions.effective_level` then takes
    the MAXIMUM, so an admin who also holds a weak grant is still an owner.
    """
    is_global_admin = user.role == ROLE_ADMIN
    grant = None
    if not is_global_admin:
        grant = await repo.get_department_level(
            session, user_id=user.id, department_id=dept.id
        )
    return permissions.effective_level(grant, is_global_admin=is_global_admin)


async def _require_grant(session: AsyncSession, user: User, dept) -> str:
    """The caller's level here, or 403. Re-checked on every turn, which is what
    makes revocation take effect on the next turn — Postgres stays the live
    authorization source.

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
```

Add `from . import permissions` to the imports. The two existing
`await _require_grant(session, user, dept)` call sites need no change — they
already ignore the return value, and now it is a level rather than `None`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_access_integration.py tests/test_rag_chat_department_integration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/rag/access.py tests/test_rag_access_integration.py
git commit -m "feat(roles): one function computes a department level

effective_department_level is the only place a level is derived, so the
chat boundary and every /v1/departments route agree by construction
rather than by two functions happening to match. A global admin still
skips the user_departments lookup entirely.

The chat boundary admits ANY level: curation is what levels gate, while
holding the grant at all is what 'may ask a question here' means."
```

---

### Task 5: Department and member routes

**Files:**
- Modify: `app/rag/router.py` (`_require_department_access` → `_require_level`;
  `list_departments`, `list_members`, `grant_member`, `revoke_member`)
- Modify: `app/rag/schemas.py` (`DepartmentOut`, `MemberOut`, `GrantCreate`)
- Test: `tests/test_rag_departments_api.py`

**Interfaces:**
- Consumes: `access.effective_department_level` (Task 4); `permissions.allows`,
  `permissions.grant_refusal`, `permissions.insufficient_level` (Task 1);
  `repo.get_department_level`, `repo.list_department_members`,
  `repo.list_departments_for_user`, `repo.grant_department` (Task 3).
- Produces:
  - `_require_level(session, user, code, minimum) -> tuple[Department, str]`
    — used by Task 6's document routes.
  - `_department_out(dept, role: str | None) -> DepartmentOut`
  - `DepartmentOut.role: str | None`, `MemberOut.email: str`, `MemberOut.role: str`,
    `GrantCreate.role: Literal["viewer","editor","owner"]` (default `"viewer"`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_departments_api.py`. Add a fixture that mints an owner, an
editor and a viewer in one fresh department:

```python
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
    email = f"rag-newowner-{uuid.uuid4().hex[:8]}@example.com"
    headers = _auth(client, email)
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
    email = f"rag-delegated-{uuid.uuid4().hex[:8]}@example.com"
    headers = _auth(client, email)
    uid = _me(client, headers)["id"]
    resp = client.post(
        f"/v1/departments/{code}/members",
        json={"user_id": uid, "role": "editor"},
        headers=owner,
    )
    assert resp.status_code == 204, resp.text


def test_an_owner_cannot_grant_owner(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    owner, _ = people["owner"]
    email = f"rag-wannabe-{uuid.uuid4().hex[:8]}@example.com"
    headers = _auth(client, email)
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
    other_email = f"rag-coowner-{uuid.uuid4().hex[:8]}@example.com"
    other = _auth(client, other_email)
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_departments_api.py -q`
Expected: FAIL — `GrantCreate` rejects the unexpected `role` key (`extra` is
forbidden on these models), and `DepartmentOut` has no `role`.

- [ ] **Step 3: Extend the schemas**

In `app/rag/schemas.py`, add `from typing import Literal` to the imports and:

```python
# The level vocabulary as the API accepts it. `Literal` rejects a typo with 422
# before the request ever reaches ck_user_departments_role, so a client gets a
# field error rather than a 500 from an IntegrityError.
DepartmentRole = Literal["viewer", "editor", "owner"]
```

`DepartmentOut` gains, after `created_at`:

```python
    # The CALLER's effective level in this department ('owner' for a global admin
    # with no grant row). Server-side so the frontend has one rule -- role >=
    # editor means show the upload button -- instead of reimplementing the policy
    # against /users/me.
    role: str | None = None
```

`MemberOut` gains, after `department_id`:

```python
    role: str
    # GET /users is global-admin-only, so without the email a department owner
    # managing members sees bare integers they cannot resolve.
    email: str
```

`GrantCreate` gains:

```python
    role: DepartmentRole = "viewer"
```

- [ ] **Step 4: Add `_require_level` and delete `_require_department_access`**

In `app/rag/router.py`, replace `_require_department_access` with:

```python
async def _require_level(
    session: AsyncSession, user: User, code: str, minimum: str
) -> tuple[Department, str]:
    """The active department plus the caller's level, or the right refusal.

    Order matters and is the same order slice 1 established: 404 for unknown or
    inactive FIRST (admins included — a soft-disabled department is gone from the
    product, and 403 would confirm it still exists), then 403 for no grant, then
    403 naming the level required. Naming it is safe: the caller holds a grant, so
    the department's existence is not the secret.
    """
    dept = await _require_active_department(session, code)
    level = await access.effective_department_level(session, user, dept)
    if level is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this department",
        )
    if not permissions.allows(level, minimum):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=permissions.insufficient_level(minimum),
        )
    return dept, level


def _department_out(dept, role: str | None) -> DepartmentOut:
    """`role` is the caller's effective level, which is not on the ORM row."""
    return DepartmentOut(
        id=dept.id,
        code=dept.code,
        name=dept.name,
        is_active=dept.is_active,
        created_at=dept.created_at,
        role=role,
    )
```

Add to the imports: `from . import access, permissions`, `from .models import Department`,
and `from .permissions import LEVEL_EDITOR, LEVEL_OWNER, LEVEL_VIEWER`. Drop
`require_admin` from the `..auth.dependencies` import only once Task 6 has removed
its last use — `POST /v1/departments` and `PATCH /v1/departments/{code}` keep it.

- [ ] **Step 5: Rewrite the four routes**

`create_department` and `update_department`: keep `Depends(require_admin)`, and
change their `return` to `_department_out(dept, LEVEL_OWNER)` — the caller is a
global admin, so that is their effective level.

`list_departments`:

```python
@router.get("", response_model=list[DepartmentOut])
async def list_departments(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DepartmentOut]:
    """Admins see every department; everyone else sees only their own tabs.

    Each row carries the caller's own level, so the frontend decides what to draw
    from one field instead of reimplementing the policy.
    """
    if user.role == ROLE_ADMIN:
        rows = await repo.list_departments(session)
        return [_department_out(d, LEVEL_OWNER) for d in rows]
    granted = await repo.list_departments_for_user(session, user.id)
    return [_department_out(d, level) for d, level in granted]
```

`list_members`:

```python
@router.get("/{code}/members", response_model=list[MemberOut])
async def list_members(
    code: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    """Owner-visible, INCLUDING other owners: `grant_refusal` restricts writes, not
    reads, and hiding owners would leave an owner unable to see why a revoke was
    refused."""
    dept, _level = await _require_level(session, user, code, LEVEL_OWNER)
    rows = await repo.list_department_members(session, dept.id)
    return [MemberOut.model_validate(m) for m in rows]
```

`grant_member` — keep the existing email-resolution block verbatim, and wrap it:

```python
@router.post("/{code}/members", status_code=status.HTTP_204_NO_CONTENT)
async def grant_member(
    code: str,
    body: GrantCreate,
    caller: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Grant access at a level, or change an existing member's level.

    An owner delegates viewer and editor; only a global admin mints owners. The
    guard is `permissions.grant_refusal`, which takes `caller_is_global_admin`
    SEPARATELY from the level for the reason documented there.
    """
    dept, caller_level = await _require_level(session, caller, code, LEVEL_OWNER)

    user_id = body.user_id
    if user_id is None:
        # Granting by email: resolve it here rather than trusting the client to
        # have looked the id up correctly. Same 404 as an unknown id, so the two
        # spellings of "that user does not exist" read identically. This path is
        # what lets an OWNER grant at all -- GET /users is global-admin-only.
        target = await users_repo.get_by_email(session, body.email.lower())
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
            )
        user_id = target.id

    existing = await repo.get_department_level(
        session, user_id=user_id, department_id=dept.id
    )
    refusal = permissions.grant_refusal(
        caller_level=caller_level,
        caller_is_global_admin=caller.role == ROLE_ADMIN,
        requested_level=body.role,
        existing_target_level=existing,
    )
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

    try:
        await repo.grant_department(
            session, user_id=user_id, department_id=dept.id,
            granted_by=caller.id, role=body.role,
        )
        await session.commit()
    except IntegrityError:
        # Unknown user_id -> FK violation.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

`revoke_member`:

```python
@router.delete("/{code}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_member(
    code: str,
    user_id: int,
    caller: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept, caller_level = await _require_level(session, caller, code, LEVEL_OWNER)
    existing = await repo.get_department_level(
        session, user_id=user_id, department_id=dept.id
    )
    # requested_level=None is revocation. An owner may not evict another owner:
    # that is the lateral case, and no last-owner guard is needed because global
    # admin is always the backstop.
    refusal = permissions.grant_refusal(
        caller_level=caller_level,
        caller_is_global_admin=caller.role == ROLE_ADMIN,
        requested_level=None,
        existing_target_level=existing,
    )
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

    removed = await repo.revoke_department(
        session, user_id=user_id, department_id=dept.id
    )
    await session.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such grant"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

Note the deliberate behaviour change to record in Task 8: the three member routes
now require an **active** department (they previously accepted an inactive one for
admins). This follows `_require_active_department`'s own stated reasoning — a
soft-disabled department is gone from the product, for admins too — and the
reactivate-then-grant order works fine.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_departments_api.py -q`
Expected: PASS. Then `.venv/bin/pytest -q 2>&1 | tail -3` and compare the skip
count to Task 1's baseline — it must not have risen.

- [ ] **Step 7: Commit**

```bash
git add app/rag/router.py app/rag/schemas.py tests/test_rag_departments_api.py
git commit -m "feat(roles): levels on the department and member routes

_require_level is the one gate: 404 unknown/inactive first (admins
included), then 403 for no grant, then 403 naming the level required --
naming it is safe because the caller holds a grant, so the department's
existence is not the secret.

Members are owner-managed, and the list shows other owners: grant_refusal
restricts writes, not reads, and hiding them would leave an owner unable
to see why a revoke was refused. GET /v1/departments now carries the
caller's effective level so the frontend has one rule instead of
reimplementing the policy against /users/me.

Behaviour change worth noting: the member routes now require an ACTIVE
department, matching _require_active_department's own reasoning."
```

---

### Task 6: Document routes move from global admin to editor

**Files:**
- Modify: `app/rag/router.py` (`upload_document`, `create_text_document`,
  `list_department_documents`, `download_department_document`,
  `archive_department_document`)
- Test: `tests/test_rag_documents_api.py`

**Interfaces:**
- Consumes: `_require_level`, `LEVEL_EDITOR`, `LEVEL_VIEWER` (Task 5);
  `permissions.allows` (Task 1).
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_documents_api.py`. Reuse that file's existing fixtures for
building a department and uploading; if it has no per-level fixture, copy
`dept_with_levels` from `tests/test_rag_departments_api.py` (repeat it — do not
import across test modules).

```python
def test_an_editor_can_upload(dept_with_levels):
    """The whole point: curating a department no longer requires global admin."""
    client, _admin, code, people = dept_with_levels
    editor, _ = people["editor"]
    resp = client.post(
        f"/v1/departments/{code}/documents",
        data={"title": "Editor upload"},
        files={"file": ("policy.txt", b"leave policy text", "text/plain")},
        headers=editor,
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["document_id"]


def test_a_viewer_cannot_upload(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    resp = client.post(
        f"/v1/departments/{code}/documents",
        data={"title": "Nope"},
        files={"file": ("x.txt", b"x", "text/plain")},
        headers=viewer,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        "Editor access to this department is required"
    )


def test_a_viewer_cannot_add_typed_text(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    resp = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Nope", "content": "text"},
        headers=viewer,
    )
    assert resp.status_code == 403


def test_a_viewer_cannot_archive(dept_with_levels):
    client, admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Doc", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    doc_id = created.json()["document_id"]
    resp = client.delete(
        f"/v1/departments/{code}/documents/{doc_id}", headers=viewer
    )
    assert resp.status_code == 403


def test_an_editor_can_archive(dept_with_levels):
    client, admin, code, people = dept_with_levels
    editor, _ = people["editor"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Doc2", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    doc_id = created.json()["document_id"]
    resp = client.delete(
        f"/v1/departments/{code}/documents/{doc_id}", headers=editor
    )
    assert resp.status_code == 204


def test_a_viewer_sees_only_ready_documents_and_no_admin_fields(dept_with_levels):
    """A pending or failed document is not part of the corpus a viewer's answers
    can cite, and surfacing it just invites 'why can't the assistant see this?'."""
    client, admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Fresh", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    rows = client.get(f"/v1/departments/{code}/documents", headers=viewer).json()
    assert all(r["status"] == "ready" for r in rows)
    assert all("embed_model" not in r for r in rows)


def test_an_editor_sees_non_ready_documents_and_admin_fields(dept_with_levels):
    client, admin, code, people = dept_with_levels
    editor, _ = people["editor"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Fresh2", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    doc_id = created.json()["document_id"]
    rows = client.get(f"/v1/departments/{code}/documents", headers=editor).json()
    assert any(r["id"] == doc_id for r in rows)
    assert all("embed_model" in r for r in rows)


def test_a_viewer_cannot_list_archived(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    resp = client.get(
        f"/v1/departments/{code}/documents?include_archived=true", headers=viewer
    )
    assert resp.status_code == 403


def test_a_viewer_cannot_download_a_non_ready_document(dept_with_levels):
    """404, not 403: at document granularity the answer must not reveal that an id
    exists in a department you can see."""
    client, admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Fresh3", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    doc_id = created.json()["document_id"]
    resp = client.get(
        f"/v1/departments/{code}/documents/{doc_id}/download", headers=viewer
    )
    assert resp.status_code == 404
```

These tests assume a freshly created document is not yet `ready` — true whenever the
ingest worker is not running against the test database, which is the normal test
condition. If a worker IS running locally, stop it before this task.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_documents_api.py -q -k "editor or viewer"`
Expected: FAIL — the editor upload returns **403** ("Admin privileges required")
because the route still depends on `require_admin`.

- [ ] **Step 3: Move the five gates**

In `upload_document`, replace the dependency and the department lookup:

```python
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    settings = get_settings()
    dept, _level = await _require_level(session, user, code, LEVEL_EDITOR)
```

and further down change `uploaded_by=admin.id` to `uploaded_by=user.id`.

In `create_text_document`, the identical two changes: the dependency, the
`_require_active_department` call, and `uploaded_by=admin.id` → `uploaded_by=user.id`.

In `archive_department_document`:

```python
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """..."""
    dept, _level = await _require_level(session, user, code, LEVEL_EDITOR)
```

In `list_department_documents`, replace the `_require_department_access` call and the
two `user.role != ROLE_ADMIN` branches:

```python
    dept, level = await _require_level(session, user, code, LEVEL_VIEWER)

    if not permissions.allows(level, LEVEL_EDITOR):
        if include_archived:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=permissions.insufficient_level(LEVEL_EDITOR),
            )
        rows = await docs_repo.list_documents(session, dept.id, ready_only=True)
        return [DocumentOut.model_validate(d) for d in rows]

    rows = await docs_repo.list_documents(
        session, dept.id, include_archived=include_archived
    )
    return [DocumentAdminOut.model_validate(d) for d in rows]
```

Update its docstring's first line from "Admins manage; members browse." to
"Editors manage; viewers browse." and keep the rest.

In `download_department_document`:

```python
    dept, level = await _require_level(session, user, code, LEVEL_VIEWER)
    doc = await docs_repo.get_document(session, document_id)
    if doc is None or doc.department_id != dept.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        )
    if not permissions.allows(level, LEVEL_EDITOR) and doc.status != STATUS_READY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_documents_api.py tests/test_rag_document_download.py tests/test_rag_document_download_nrb.py -q`
Expected: PASS.

- [ ] **Step 5: Confirm no route still over-gates**

```bash
grep -n "require_admin" app/rag/router.py
```

Expected: exactly two hits, both in the import line and on `create_department` /
`update_department`. Anything else is a curation route that was missed.

- [ ] **Step 6: Commit**

```bash
git add app/rag/router.py tests/test_rag_documents_api.py
git commit -m "feat(roles): corpus curation is editor-level, not global admin

Upload, typed text and archive move from require_admin to editor on the
department. This is the change the whole feature exists for: an HR
manager curating the HR corpus no longer needs admin over Finance, the
user table and the NRB pipeline.

Listing and download split on allows(level, editor) rather than on
role == admin, keeping the two established refusal shapes -- 403 naming
the level for the list, 404 at document granularity for the download, so
an id's existence in a department you can see does not leak."
```

---

### Task 7: Ingest job progress follows the document's department

Without this the feature ships broken: an editor uploads, receives
`{document_id, job_id}`, and 403s polling their own job.

**Files:**
- Modify: `app/rag/jobs_router.py`
- Test: `tests/test_rag_jobs_integration.py`

**Interfaces:**
- Consumes: `repo.get_department_level` (Task 3); `permissions.allows`,
  `LEVEL_EDITOR` (Task 1); `documents.get_document`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_jobs_integration.py` (copy `dept_with_levels` in as above
if the file has no equivalent fixture):

```python
def test_an_editor_can_poll_the_job_for_their_own_upload(dept_with_levels):
    client, _admin, code, people = dept_with_levels
    editor, _ = people["editor"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Editor doc", "content": "text"},
        headers=editor,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    resp = client.get(f"/v1/ingest-jobs/{job_id}", headers=editor)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == job_id


def test_a_viewer_cannot_poll_a_job_in_their_own_department(dept_with_levels):
    """404 rather than 403: a job id maps to a document, and confirming that this
    one exists would leak the corpus's shape to someone who cannot see it."""
    client, admin, code, people = dept_with_levels
    viewer, _ = people["viewer"]
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Admin doc", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    resp = client.get(f"/v1/ingest-jobs/{job_id}", headers=viewer)
    assert resp.status_code == 404


def test_an_outsider_cannot_poll_a_job(dept_with_levels):
    client, admin, code, _people = dept_with_levels
    outsider = _auth(client, f"rag-outsider-{uuid.uuid4().hex[:8]}@example.com")
    created = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Admin doc 2", "content": "text"},
        headers=admin,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    assert client.get(
        f"/v1/ingest-jobs/{job_id}", headers=outsider
    ).status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_jobs_integration.py -q -k "poll"`
Expected: FAIL — the editor gets 403 "Admin privileges required".

- [ ] **Step 3: Rewrite the route**

Replace the whole of `app/rag/jobs_router.py`:

```python
"""Ingest job progress. Separate router because the path is not under
/v1/departments — a job id is enough to identify the work."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..users.models import ROLE_ADMIN, User
from . import documents as docs_repo
from . import jobs as jobs_repo
from . import repository as repo
from .permissions import LEVEL_EDITOR, allows
from .schemas import IngestJobOut

router = APIRouter(prefix="/v1/ingest-jobs", tags=["departments"])


@router.get("/{job_id}", response_model=IngestJobOut)
async def get_ingest_job(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IngestJobOut:
    """Editor of the job's department, or a global admin.

    This is not a nicety: `POST .../documents` hands the uploader a `job_id`, so
    gating this on global admin would let an editor upload and then be refused
    progress on their own upload.

    Every refusal is **404**, never 403. A job id maps to a document, so
    confirming that this one exists would leak the corpus's shape to someone who
    cannot see it — the same rule as the download route.
    """
    job = await jobs_repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown ingest job"
        )

    if user.role != ROLE_ADMIN:
        doc = await docs_repo.get_document(session, job.document_id)
        level = None
        if doc is not None:
            # Not gated on the department being active: the job is a record of
            # work on a document this editor owns, and a department disabled
            # mid-ingest should not turn their progress view into a 404.
            level = await repo.get_department_level(
                session, user_id=user.id, department_id=doc.department_id
            )
        if not allows(level, LEVEL_EDITOR):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown ingest job"
            )

    return IngestJobOut.model_validate(job)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_jobs_integration.py -q`
Expected: PASS. Then the whole suite:
`.venv/bin/pytest -q 2>&1 | tail -3` — all green, and the **skip count equal to
Task 1's baseline**. If skips rose, an `_auth` helper broke and tests are silently
not running.

- [ ] **Step 5: Commit**

```bash
git add app/rag/jobs_router.py tests/test_rag_jobs_integration.py
git commit -m "fix(roles): an editor can poll the job for their own upload

POST .../documents hands the uploader a job_id, so leaving this route on
require_admin shipped the feature broken: upload succeeds, then 403 on
the progress call.

Every refusal is 404, never 403 -- a job id maps to a document, so
confirming this one exists would leak the corpus's shape. Same rule as
the download route."
```

---

### Task 8: Documentation

The frontend is a separate repo (`local-ai-model-frontend`) and cannot draw an
upload button without `DepartmentOut.role`, so the contract document is part of
shipping this.

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `STATUS.md`, `docs/frontend-sync-prompt.md`

- [ ] **Step 1: `CLAUDE.md` — the endpoint list**

Under "Endpoints", change the department lines to state the new gates: members
routes are `owner` (admin for the `owner` level itself), `documents` POST/DELETE are
`editor`, `GET .../documents` is "viewers see `ready` only; editors see
non-archived, `?include_archived=` editor-only", and
`GET /v1/ingest-jobs/{id}` is "editor of the job's department, 404 otherwise".

- [ ] **Step 2: `CLAUDE.md` — a gotcha bullet**

Add to "Conventions / gotchas", next to the department-access bullets:

```markdown
- **A department grant carries a LEVEL, and a global admin is not an owner.**
  `user_departments.role` is `viewer` < `editor` < `owner`
  (`ck_user_departments_role` closes it, same rule as `ck_documents_status`), and
  every decision lives in the pure `app/rag/permissions.py`. Two things a rewrite
  must not lose: (1) **`allows()` takes a level while `grant_refusal()` takes
  `caller_is_global_admin` SEPARATELY** — a global admin is owner-equivalent for
  capabilities, but run them through the owner escalation rule and "an owner
  cannot grant owner" stops global admins creating owners too, at which point the
  feature is unusable and whoever debugs it deletes the guard
  (`test_a_global_admin_is_never_refused_a_membership_change`); (2) **every gate
  fails closed** — `allows(None, …)` and `allows("<unknown>", …)` are both False,
  so a level that escaped the CHECK cannot compare as rank 0 and pass the viewer
  test. `effective_level` takes the MAXIMUM of grant and admin-ness, so being
  granted a department never costs an admin a capability. An owner delegates
  viewer/editor but never `owner`, and may not demote or evict an existing owner
  — that bounds the escalation chain at depth 1, and needs no last-owner guard
  because global admin is the backstop (unlike `LAST_ADMIN`, which has none).
  `access.effective_department_level` is the ONE place a level is computed;
  `router._require_level` is the ONE gate, ordering its refusals 404 unknown /
  inactive → 403 no grant → 403 naming the level. Naming the level is safe only
  there: at document and job granularity the answer is **404**, because existence
  is the secret.
```

- [ ] **Step 3: `README.md` — extend §"Auth model"**

After the `auth_provider` table, add a subsection stating that `users.role`
(`admin`|`member`) gates global routes while a department grant carries its own
level, with the §3 permission matrix from the spec, and the sentence: *"A global
admin is the backstop for every department; only a global admin can create a
department owner."*

- [ ] **Step 4: `STATUS.md`**

In the auth bullet, note the department level and that `POST
/v1/departments/{code}/members` takes `role` and doubles as promote/demote. Remove
any claim that document management is admin-only.

- [ ] **Step 5: `docs/frontend-sync-prompt.md`**

Under `DEPARTMENTS`, document: `DepartmentOut.role` is the caller's effective level
and the **only** field the UI needs to decide what to draw (`role === "editor" ||
role === "owner"` → upload/archive; `role === "owner"` → the members screen); do NOT
recombine it with `/users/me`'s global role. Under members, document `MemberOut`'s
`email`/`role`, `GrantCreate`'s optional `role`, that POST is also promote/demote,
and the two new 403s whose `detail` must be **rendered verbatim** (an owner
attempting to grant or change an `owner`). Note that the new 403 detail
`"Editor access to this department is required"` is a level problem, not a login
problem — never trigger a re-login on it.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
git add CLAUDE.md README.md STATUS.md docs/frontend-sync-prompt.md
git commit -m "docs(roles): department levels across the four contract documents

The frontend lives in a separate repo and cannot draw an upload button
without DepartmentOut.role, so the sync prompt is part of shipping this.

Records the global-admin-versus-owner trap as a gotcha: allows() takes a
level, grant_refusal() takes caller_is_global_admin separately, and
collapsing them makes global admins unable to create owners."
```

---

## Self-Review

**Spec coverage.** §3 matrix → Tasks 5, 6, 7 (one test per row). §4 schema →
Task 2. §5.1 pure policy → Task 1. §5.2 wiring → Tasks 3, 4. §6 flow bugs →
bug 1 Task 7, bug 2 Task 3 Step 5 + Task 5, bug 3 covered by `GrantCreate.email`
already existing and asserted in Task 5's owner-grant test. §7 API contract →
Tasks 5, 6, 7. §8 error semantics → asserted in Tasks 5, 6, 7. §9 testing → each
task's own tests. §10 rollout → Task 2 Step 6 (both databases) and Task 8. §11
evaluation → the matrix suite is the labelled set; Task 8 Step 2 keeps the matrix
and the tests in step. §12 out-of-scope → nothing in this plan touches those.

**Type consistency.** `get_department_level` returns `str | None` in Tasks 3, 4, 5,
7. `_require_level` returns `tuple[Department, str]` and every caller unpacks two
values (Tasks 5, 6). `list_departments_for_user` returns rows unpacked as
`(dept, level)` in Task 5. `list_department_members` rows are validated by
`MemberOut` whose fields exactly match the six selected columns. `effective_level`
and `allows` keep one signature throughout. `LEVEL_VIEWER`/`LEVEL_EDITOR`/
`LEVEL_OWNER` are the only level spellings used in code; SQL literals appear only
in the migration and the two CHECK constraints.

**Known gap, deliberate.** `has_department_access` is deleted in Task 3 while
`app/rag/access.py` and `app/rag/router.py` still call it, so the suite is red
between Tasks 3 and 4. Task 3 Step 6 says so explicitly rather than pretending
otherwise; do not "fix" it by keeping a shim.
