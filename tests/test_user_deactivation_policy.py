"""Who may be deactivated. A pure decision, exhaustively tested.

Deactivating a user is the offboarding switch: `get_current_user` re-reads the row
on every request, so `is_active=false` invalidates an already-issued 24h JWT on
the caller's next API call. That makes it the one lever with immediate effect —
and the one that can lock everybody out of the system if it is applied to the last
remaining admin.

The decision is a pure function precisely so both dangerous cases can be tested
without a database. The last-active-admin branch cannot be exercised over HTTP
against a shared development database (there are two real admins, and reducing
them to one to prove the guard risks the lockout the guard exists to prevent), so
it is proven here instead and the API test only asserts the guard does not
over-fire.
"""

import pytest

from app.users.models import ROLE_ADMIN, ROLE_MEMBER
from app.users.policy import LAST_ADMIN, SELF_DEACTIVATION, deactivation_refusal


def _refusal(**over):
    kwargs = dict(
        target_id=2,
        target_role=ROLE_MEMBER,
        target_is_active=True,
        caller_id=1,
        active_admin_count=2,
    )
    kwargs.update(over)
    return deactivation_refusal(**kwargs)


def test_deactivating_an_ordinary_member_is_allowed():
    assert _refusal() is None


def test_you_cannot_deactivate_yourself():
    """An admin locking themselves out by accident is the likeliest mistake here."""
    assert _refusal(target_id=1, caller_id=1) == SELF_DEACTIVATION


def test_self_deactivation_is_refused_even_when_other_admins_exist():
    assert (
        _refusal(target_id=7, caller_id=7, target_role=ROLE_ADMIN, active_admin_count=9)
        == SELF_DEACTIVATION
    )


def test_the_last_active_admin_cannot_be_deactivated():
    """Nobody could administer the system afterwards, including re-enabling them."""
    assert (
        _refusal(target_role=ROLE_ADMIN, active_admin_count=1) == LAST_ADMIN
    )


def test_an_admin_can_be_deactivated_while_another_admin_remains():
    assert _refusal(target_role=ROLE_ADMIN, active_admin_count=2) is None


def test_an_already_inactive_admin_does_not_count_as_the_last_one():
    """They are not currently holding the system up, so this is a no-op."""
    assert (
        _refusal(target_role=ROLE_ADMIN, target_is_active=False, active_admin_count=1)
        is None
    )


def test_a_member_is_never_the_last_admin():
    assert _refusal(target_role=ROLE_MEMBER, active_admin_count=0) is None


@pytest.mark.parametrize("count", [0, 1])
def test_a_zero_or_one_admin_count_still_protects_an_active_admin(count):
    """0 should be impossible, but refusing is the safe reading of a bad count."""
    assert _refusal(target_role=ROLE_ADMIN, active_admin_count=count) == LAST_ADMIN


def test_self_deactivation_is_checked_before_the_admin_count():
    """Both apply: the message should name the more specific mistake."""
    assert (
        _refusal(
            target_id=1, caller_id=1, target_role=ROLE_ADMIN, active_admin_count=1
        )
        == SELF_DEACTIVATION
    )
