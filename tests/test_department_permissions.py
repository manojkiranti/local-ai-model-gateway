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


# --------------------------------------------------------------------------- #
# Self-targeting: an owner's own row is theirs. Raised independently by the
# /code-review pass and by the frontend author, who both noticed the docstring
# reasoning ("owner A evicting owner B") does not cover acting on yourself.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("requested", [None, "viewer", "editor", "owner"])
def test_an_owner_may_always_act_on_their_own_row(requested):
    """Stepping down or leaving must not need a global admin. The escalation
    rationale is about evicting a FELLOW owner; it never applied to yourself."""
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level=requested,
            existing_target_level=perms.LEVEL_OWNER,
            target_is_caller=True,
        )
        is None
    )


@pytest.mark.parametrize("caller", [None, "viewer", "editor"])
def test_self_targeting_is_not_an_escalation_route(caller):
    """The self branch sits BEHIND the owner gate, so a viewer or editor naming
    themselves gains nothing."""
    assert perms.grant_refusal(
        caller_level=caller,
        caller_is_global_admin=False,
        requested_level=perms.LEVEL_OWNER,
        existing_target_level=caller,
        target_is_caller=True,
    ) == perms.insufficient_level(perms.LEVEL_OWNER)


def test_targeting_someone_else_is_unchanged_by_the_self_rule():
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level="viewer",
            existing_target_level=perms.LEVEL_OWNER,
            target_is_caller=False,
        )
        == perms.OWNER_CANNOT_CHANGE_OWNER
    )


def test_target_is_caller_defaults_to_false():
    """The dangerous reading must be the one you have to ask for: a caller that
    forgets the argument gets the strict answer, not the permissive one."""
    assert (
        perms.grant_refusal(
            caller_level=perms.LEVEL_OWNER,
            caller_is_global_admin=False,
            requested_level=None,
            existing_target_level=perms.LEVEL_OWNER,
        )
        == perms.OWNER_CANNOT_CHANGE_OWNER
    )
