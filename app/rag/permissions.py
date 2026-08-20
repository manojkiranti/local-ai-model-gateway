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
    existence IS the secret — a document or an ingest job — the answer is 404
    instead.
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
