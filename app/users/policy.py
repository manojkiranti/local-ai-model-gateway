"""Pure decisions about user administration.

`is_active` is the offboarding switch with immediate effect: `get_current_user`
re-reads the user row on every request, so clearing it invalidates an
already-issued JWT on the holder's next API call. Disabling someone in Active
Directory does NOT do that — an AD account can be disabled while a 24h token
issued minutes earlier keeps working until it expires, because the login boundary
is the only place AD is consulted. So this flag is the lever that actually cuts
access off now, which is exactly why it needs guarding.

Kept pure (no session, no ORM) so both dangerous cases are exhaustively testable
without a database — and in particular so the "last active admin" branch can be
proven without ever reducing a real deployment to zero admins to try it.
"""

from .models import ROLE_ADMIN

SELF_DEACTIVATION = "You cannot deactivate your own account"
LAST_ADMIN = (
    "This is the last active admin; promote or activate another admin first"
)


def deactivation_refusal(
    *,
    target_id: int,
    target_role: str,
    target_is_active: bool,
    caller_id: int,
    active_admin_count: int,
) -> str | None:
    """Why this user may not be deactivated, or None if they may.

    Only consulted when switching `is_active` OFF; turning it back on is always
    safe. `active_admin_count` counts users who are `role='admin'` AND currently
    active, including the target.
    """
    if target_id == caller_id:
        # Checked first: when both rules apply, this names the likelier mistake.
        return SELF_DEACTIVATION
    if target_role == ROLE_ADMIN and target_is_active and active_admin_count <= 1:
        # A count of 0 should be impossible while an active admin is being read,
        # but refusing is the safe reading of an impossible number.
        return LAST_ADMIN
    return None
