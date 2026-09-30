"""What a person types in the sign-in box -> the account identity (an email).

Accounts are keyed by email (`users.email`, `UserOut.email: EmailStr`), but
bank staff sign in to everything else with their bare network username. So a
username (`shristi.b`, or `NICASIA\\shristi.b`) is mapped to
`shristi.b@<LOGIN_EMAIL_DOMAIN>` and from there on is exactly the same account
as typing the full address — one identity, one throttle counter, one row.
With no domain configured a username is refused (422) rather than guessed.

Pure: no settings object, no I/O, so every rule is provable in isolation.
"""

from __future__ import annotations

import re

from pydantic import EmailStr, TypeAdapter, ValidationError

_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_EMAIL = TypeAdapter(EmailStr)


class LoginNameError(ValueError):
    """The sign-in name is neither a valid email nor a usable username."""


def canonical_login(raw: str, email_domain: str) -> str:
    """The lower-cased account email for what was typed.

    `user@domain` passes through (validated as an email). `DOMAIN\\user` drops
    the Windows domain prefix. A bare username gets `@email_domain` appended.
    """
    name = (raw or "").strip().lower()
    if "\\" in name:
        name = name.rsplit("\\", 1)[1]
    if not name:
        raise LoginNameError("Enter your username or email address.")
    if "@" in name:
        try:
            _EMAIL.validate_python(name)
        except ValidationError:
            raise LoginNameError("Enter a valid email address.") from None
        return name
    if not _USERNAME.fullmatch(name):
        raise LoginNameError("Enter a valid username or email address.")
    domain = (email_domain or "").strip().lower().lstrip("@")
    if not domain:
        raise LoginNameError("Sign in with your full email address.")
    return f"{name}@{domain}"


def directory_name(email: str, mode: str) -> str:
    """What Active Directory is asked about: the full address (`upn`, the
    original behaviour) or just the part before the @ (`sam`, the
    sAMAccountName) — which one the shim expects has not been verified live."""
    return email.split("@", 1)[0] if mode == "sam" else email
