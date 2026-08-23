"""Minting and verifying an API key. PURE — no DB, no HTTP, no ORM.

Token format: `<label>_<prefix8>_<secret64>`, e.g.
`lgw_live_a1b2c3d4_x7Qk…`. The prefix is a NON-secret lookup handle stored in an
indexed UNIQUE column; the secret is never stored in recoverable form.

Three design points that a future reader will be tempted to "fix", and must not:

  * **Secret is hex, not url-safe base64.** The token is delimited by "_", and
    base64url includes "_" as an encoding character — measured, 48.6% of
    `token_urlsafe(32)` secrets contain one, which would put the delimiter
    inside the secret and break the split. Hex cannot. Entropy is identical
    (32 bytes = 256 bits); the secret is 64 chars instead of 43. This removes
    ambiguity so the split is exact and no scanning is needed.
  * **SHA-256, not bcrypt.** The secret is 32 bytes of full entropy, no
    dictionary to attack — so bcrypt's work factor buys nothing while costing
    ~100 ms on EVERY request to `/v1/ocr`. Passwords need bcrypt because
    humans choose them; this is not that. (`app/auth/security.py` is right to
    use bcrypt: those are human passwords.)
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
    # token_hex, NOT token_urlsafe: the token is delimited by "_", and
    # base64url includes "_" — measured, 48.6% of token_urlsafe(32) secrets
    # contain one, which would put the delimiter inside the secret and make
    # the split ambiguous. Hex cannot. Entropy is identical (32 bytes = 256
    # bits); the secret is just 64 chars instead of 43.
    secret = secrets.token_hex(SECRET_BYTES)
    return MintedKey(
        token=f"{label}_{prefix}_{secret}",
        prefix=prefix,
        key_hash=hash_secret(secret),
    )


def parse(token: str) -> tuple[str, str] | None:
    """`(prefix, secret)`, or None if this cannot be a key at all.

    The secret is hex (see `mint`), so it never contains the "_" delimiter and
    this split is exact. Returns None rather than raising: a malformed header is
    an ordinary 401, not an exception path, and the caller must not be able to
    tell the two apart.
    """
    if not token:
        return None
    parts = token.strip().split("_")
    # label may itself contain underscores ("lgw_live"), so take from the END.
    if len(parts) < 3:
        return None
    prefix, secret = parts[-2], parts[-1]
    if len(prefix) != PREFIX_LEN or not secret:
        return None
    return prefix, secret


def verify(token: str, key_hash: str) -> bool:
    parsed = parse(token)
    if parsed is None:
        return False
    _, secret = parsed
    return hmac.compare_digest(hash_secret(secret), key_hash)
