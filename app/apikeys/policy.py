"""Whether an API key may be used, and for what. PURE — no session, no ORM.

Kept pure for the `app/users/policy.py` reason: the branches that reject a
credential are the ones you must be able to prove exhaustively, and proving
"a revoked key is refused" should not require revoking a key in a real database.

Every gate FAILS CLOSED. `is_usable(None, …)` is False, and a scope string that
somehow escaped `ck_api_keys_scopes` satisfies nothing — the same rule as
`permissions.allows(None, …)`, where a level that escaped its CHECK must not
compare as rank 0 and pass the viewer test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

__all__ = [
    "SCOPE_OCR_READ",
    "SCOPE_DOCUMENT_READ",
    "ALL_SCOPES",
    "INVALID_KEY",
    "KeyFacts",
    "is_usable",
    "scope_refusal",
]

SCOPE_OCR_READ = "ocr:read"
SCOPE_DOCUMENT_READ = "document:read"

# Closed vocabulary, mirroring `ck_api_keys_scopes`. Adding a scope means
# editing BOTH — that duplication is deliberate: the CHECK stops a typo being
# stored, this set stops one being honoured.
ALL_SCOPES = frozenset({SCOPE_OCR_READ, SCOPE_DOCUMENT_READ})

# ONE message for all six 401 causes: header absent, malformed, unknown prefix,
# hash mismatch, revoked, expired. The log records which; the response never
# does, because distinguishing them tells an attacker which prefixes are real
# and whether a valid key ever existed.
INVALID_KEY = "Invalid API key"


@dataclass(frozen=True)
class KeyFacts:
    """The stored facts about one key, lifted out of the ORM row.

    A plain dataclass rather than the model so this module stays importable
    without SQLAlchemy and testable without a row.
    """

    is_active: bool
    expires_at: datetime | None
    scopes: tuple[str, ...]


def is_usable(facts: KeyFacts | None, *, now: datetime) -> bool:
    """Whether this key is a live credential at `now`.

    `None` means no row matched the presented prefix — refused, and
    indistinguishable to the caller from a wrong secret.
    """
    if facts is None:
        return False
    if not facts.is_active:
        return False
    if facts.expires_at is not None:
        expiry = facts.expires_at
        if expiry.tzinfo is None:
            # Postgres timestamptz round-trips aware, but a hand-built row or a
            # future driver change must not crash the auth path.
            expiry = expiry.replace(tzinfo=timezone.utc)
        # `<=`: expiry exactly at `now` is expired. An operator setting an
        # expiry expects it to have taken effect at that instant.
        if expiry <= now:
            return False
    return True


def scope_refusal(facts: KeyFacts, *, required: str) -> str | None:
    """Why this key may not use `required`, or None if it may."""
    if required not in facts.scopes:
        return f"This key lacks the {required} scope"
    return None
