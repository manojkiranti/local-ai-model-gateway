"""app/auth/login_name.py — what is typed in the sign-in box -> account email."""

import pytest

from app.auth.login_name import LoginNameError, canonical_login, directory_name


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("shristi.b", "shristi.b@nicasiabank.com"),
        ("  Shristi.B ", "shristi.b@nicasiabank.com"),
        ("NICASIA\\shristi.b", "shristi.b@nicasiabank.com"),
        ("user_01-x", "user_01-x@nicasiabank.com"),
        ("Workspace-Admin@Example.com", "workspace-admin@example.com"),
    ],
)
def test_a_username_or_email_becomes_the_account_email(typed, expected):
    assert canonical_login(typed, "nicasiabank.com") == expected


def test_the_domain_may_be_given_with_a_leading_at():
    assert canonical_login("shristi", "@NICASIABANK.com") == "shristi@nicasiabank.com"


def test_a_username_without_a_configured_domain_is_refused_not_guessed():
    with pytest.raises(LoginNameError, match="full email"):
        canonical_login("shristi.b", "")


@pytest.mark.parametrize("typed", ["", "   ", "a b", "shristi/b", "user@", "@host.com", "x" * 65])
def test_malformed_names_are_refused(typed):
    with pytest.raises(LoginNameError):
        canonical_login(typed, "nicasiabank.com")


def test_directory_name_is_the_upn_by_default_and_the_username_for_sam():
    assert directory_name("shristi.b@nicasiabank.com", "upn") == "shristi.b@nicasiabank.com"
    assert directory_name("shristi.b@nicasiabank.com", "sam") == "shristi.b"
