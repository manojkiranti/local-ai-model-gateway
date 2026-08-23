"""Minting and verifying an API key. PURE — no DB, no HTTP, no ORM.

Token format: `<label>_<prefix8>_<secret43>`, e.g.
`lgw_live_a1b2c3d4_x7Qk…`. The prefix is a NON-secret lookup handle stored in an
indexed UNIQUE column; the secret is never stored in recoverable form.

Two design points that a future reader will be tempted to "fix", and must not:

  * **SHA-256, not bcrypt.** The secret is 32 bytes of `secrets.token_urlsafe`
    — full entropy, no dictionary to attack — so bcrypt's work factor buys
    nothing while costing ~100 ms on EVERY request to `/v1/ocr`. Passwords need
    bcrypt because humans choose them; this is not that. (`app/auth/security.py`
    is right to use bcrypt: those are human passwords.)
  * **The prefix exists so verification is one indexed lookup.** Hashing with a
    per-row salt would force a scan over every key to find the matching row.
    A prefix plus an unsalted hash of a full-entropy secret has no rainbow-table
    exposure, because there is no low-entropy input to tabulate.

`verify` uses `hmac.compare_digest`. `==` on a hash is a timing oracle that
reads as correct code, which is exactly why a test asserts this by AST.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

__all__ = [
    "PREFIX_LEN",
    "SECRET_BYTES",
    "DEFAULT_LABEL",
    "MintedKey",
    "mint",
    "parse",
    "hash_secret",
    "verify",
]

PREFIX_LEN = 8
SECRET_BYTES = 32
DEFAULT_LABEL = "lgw_live"


@dataclass(frozen=True)
class MintedKey:
    """A freshly minted key. `token` is the ONLY time the plaintext exists."""

    token: str
    prefix: str
    key_hash: str


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint(label: str = DEFAULT_LABEL) -> MintedKey:
    prefix = secrets.token_hex(PREFIX_LEN // 2)          # 8 hex chars
    secret = secrets.token_urlsafe(SECRET_BYTES)         # 43 chars, url-safe
    return MintedKey(
        token=f"{label}_{prefix}_{secret}",
        prefix=prefix,
        key_hash=hash_secret(secret),
    )


def parse(token: str) -> tuple[str, str] | None:
    """`(prefix, secret)`, or None if this cannot be a key at all.

    Returns None rather than raising: a malformed header is an ordinary 401, not
    an exception path, and the caller must not be able to tell the two apart.
    """
    if not token:
        return None
    token = token.strip()
    if not token:
        return None
    parts = token.split("_")
    # label may itself contain underscores ("lgw_live"), so search from the END.
    # The prefix is exactly PREFIX_LEN (8) hex chars, secret may contain underscores.
    if len(parts) < 3:
        return None
    # Search backwards for an 8-character hex string (the prefix)
    for i in range(len(parts) - 2, -1, -1):
        potential_prefix = parts[i]
        if len(potential_prefix) == PREFIX_LEN:
            try:
                int(potential_prefix, 16)
                # Found a valid hex prefix! Everything after it is the secret.
                secret = "_".join(parts[i + 1:])
                if secret:
                    return potential_prefix, secret
            except ValueError:
                continue
    return None


def verify(token: str, key_hash: str) -> bool:
    parsed = parse(token)
    if parsed is None:
        return False
    _, secret = parsed
    return hmac.compare_digest(hash_secret(secret), key_hash)
