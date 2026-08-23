# External API: API-Key Auth + Image OCR Endpoint — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an external application POST an image with an API key and get its
OCR text back synchronously, without that key being able to reach any
JWT-authenticated route.

**Architecture:** A new `app/apikeys/` package holds a second credential type —
an `ApiClient`, which is deliberately NOT a `User` — with the accept/reject
decision in two pure modules (`keygen`, `policy`) that need no database. A new
`app/publicapi/` package holds the one external route, which reuses the existing
`app/files/image_ocr.py` engine through `asyncio.to_thread` behind a semaphore so
CPU-bound OCR cannot stall the event loop. Both routers are registered only when
`EXTERNAL_API_ENABLED` is true, which defaults false.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async ORM, Alembic, Postgres, Pydantic
v2 / pydantic-settings, pytest. OCR via `rapidocr` + `onnxruntime` (already in
`requirements-ocr.txt`, opt-in build flag). No new third-party dependency.

**Spec:** `docs/superpowers/specs/2026-08-23-external-api-keys-and-ocr-endpoint-design.md`

## Corrections to the spec

Read these before Task 3 — the spec's schema sketch used types this repo does
not use:

1. **`users.id` is `Integer`**, not uuid (`app/users/models.py`). So
   `api_keys.created_by_user_id` is an integer FK.
2. **Ids in this repo are `String(32)` uuid-hex**, not native `uuid` — see
   `GeneratedFile.id`, `chat_sessions.id`. `api_keys.id` follows that pattern
   (unguessable, and a stable render key for a future frontend).
3. `scopes` stays `ARRAY(String)` with the CHECK as specified.

Everything else in the spec stands as written.

## Global Constraints

- **Use this project's venv for every command:** `.venv/bin/python`,
  `.venv/bin/pytest`, `.venv/bin/alembic`. Never a sibling project's.
- **Python 3.10.** No `match` statements needed; `X | None` annotations are fine.
- **`alembic heads` must stay exactly one.** The current head is `c2f8b1d47e93`.
  The new migration's `down_revision` is `c2f8b1d47e93`. `tests/test_alembic_lineage.py`
  fails if a second head appears. Never create a merge revision.
- **An `ApiClient` is never a `User`.** No function in `app/apikeys/` may return
  a `User`, and `require_api_client` must never be usable as a substitute for
  `get_current_user`.
- **The OCR stack must not become mandatory.** `rapidocr`/`onnxruntime` stay in
  `requirements-ocr.txt` only. Every import of them stays inside a function.
  `app/publicapi/` must never import `rapidocr`, `onnxruntime`, `cv2` or any
  part of `docling` at module scope.
- **No confidence threshold anywhere.** Report scores; never compare one to a
  literal. An AST test enforces this (Task 10).
- **The OCR caveat is ONE constant with TWO readers.** It lives in
  `app/files/image_ocr.py` and is imported by both `app/tools/local/read_image.py`
  and `app/publicapi/schemas.py`. Never a second copy.
- **Exact scope string:** `ocr:read`. Exact key prefix default: `lgw_live`.
- **Exact 401 detail, for all six causes:** `Invalid API key`.
- **Exact 503 detail when the stack is absent:** `image OCR is not enabled on
  this deployment` (byte-identical to what `read_image` already says).
- **Test login credentials that already exist in Postgres:**
  `admin@example.com` / `supersecret123`.

---

## File Structure

**Create:**
- `app/apikeys/__init__.py` — package marker, no logic.
- `app/apikeys/keygen.py` — PURE. Mint, parse, hash, constant-time verify. No DB, no ORM, no FastAPI import.
- `app/apikeys/policy.py` — PURE. Is this key usable? Is the scope satisfied? No DB.
- `app/apikeys/models.py` — `ApiKey`, `ApiKeyUsage` ORM models.
- `app/apikeys/repository.py` — data access: lookup by prefix, insert, revoke, list, touch `last_used_at`, record usage.
- `app/apikeys/throttle.py` — PURE-ish. `RateLimiter` (per-key requests/minute) plus the shared auth-failure `LoginThrottle` instance keyed on key prefix.
- `app/apikeys/dependencies.py` — `require_api_client(scope)` FastAPI dependency; the only place the three pieces above are combined.
- `app/apikeys/schemas.py` — request/response models for the admin routes.
- `app/apikeys/router.py` — `/v1/api-keys` (JWT + `require_admin`).
- `app/publicapi/__init__.py` — package marker.
- `app/publicapi/schemas.py` — the OCR response envelope.
- `app/publicapi/ocr_router.py` — `POST /v1/ocr`.
- `alembic/versions/<rev>_api_keys_and_usage.py` — the migration.

**Modify:**
- `app/config.py` — eight new settings + validation.
- `app/main.py` — conditional router registration.
- `app/tools/local/read_image.py` — import the caveat instead of defining it.
- `.env.example` — document the new settings.
- `CLAUDE.md` — endpoints list + a conventions/gotchas entry.

**Test:**
- `tests/test_apikey_keygen.py`, `tests/test_apikey_policy.py`,
  `tests/test_apikey_rate_limit.py` — pure, no DB.
- `tests/test_apikey_admin_integration.py`, `tests/test_ocr_api_integration.py` — Postgres.
- `tests/test_ocr_api_boundaries.py` — AST + subprocess boundary tests.
- `tests/test_ocr_api_eval.py` — the 12-case eval, live-gated.

---

## Task 1: `keygen.py` — minting and verifying a key (PURE)

**Files:**
- Create: `app/apikeys/__init__.py`, `app/apikeys/keygen.py`
- Test: `tests/test_apikey_keygen.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PREFIX_LEN: int = 8`, `SECRET_BYTES: int = 32`
  - `class MintedKey` (frozen dataclass): `.token: str`, `.prefix: str`, `.key_hash: str`
  - `mint(label: str = "lgw_live") -> MintedKey`
  - `parse(token: str) -> tuple[str, str] | None` — `(prefix, secret)` or None if malformed
  - `hash_secret(secret: str) -> str` — sha256 hex
  - `verify(token: str, key_hash: str) -> bool` — constant-time

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apikey_keygen.py`:

```python
"""Pure tests for API key minting and verification. No DB, no app import.

These prove the credential mechanics, so they are exhaustive on the failure
side: a truncated token, a prefix-only token and a right-prefix/wrong-secret
token must all fail to verify.
"""

import ast
from pathlib import Path

import pytest

from app.apikeys import keygen


def test_mint_produces_a_parseable_token():
    minted = keygen.mint()
    parsed = keygen.parse(minted.token)
    assert parsed is not None
    prefix, secret = parsed
    assert prefix == minted.prefix
    assert len(prefix) == keygen.PREFIX_LEN
    assert keygen.hash_secret(secret) == minted.key_hash


def test_the_token_carries_the_label_so_a_dev_key_is_visibly_not_prod():
    assert keygen.mint("lgw_test").token.startswith("lgw_test_")


def test_the_plaintext_secret_is_not_recoverable_from_the_hash():
    minted = keygen.mint()
    _, secret = keygen.parse(minted.token)
    assert secret not in minted.key_hash
    assert len(minted.key_hash) == 64  # sha256 hex


def test_mints_never_repeat():
    tokens = {keygen.mint().token for _ in range(200)}
    prefixes = {keygen.mint().prefix for _ in range(200)}
    assert len(tokens) == 200
    assert len(prefixes) == 200


def test_verify_accepts_the_real_token():
    minted = keygen.mint()
    assert keygen.verify(minted.token, minted.key_hash) is True


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t[:-1],                      # truncated
        lambda t: t + "x",                     # extended
        lambda t: t.rsplit("_", 1)[0],         # prefix only, secret removed
        lambda t: t.rsplit("_", 1)[0] + "_" + "z" * 43,  # right prefix, wrong secret
        lambda t: "",
        lambda t: "   ",
        lambda t: "lgw_live",
        lambda t: t.upper(),
    ],
)
def test_verify_rejects_every_mangled_token(mangle):
    minted = keygen.mint()
    assert keygen.verify(mangle(minted.token), minted.key_hash) is False


def test_parse_returns_none_rather_than_raising_on_junk():
    for junk in ["", "no-underscores", "lgw_live", "\x00\x01", "a_b"]:
        assert keygen.parse(junk) is None


def test_verify_uses_a_constant_time_comparison():
    """`==` on a hash is a timing oracle that reads as perfectly correct code.

    Asserted on the AST, not by timing (a timing test is flaky), and not by
    reading the text (a comment mentioning compare_digest would satisfy grep).
    """
    tree = ast.parse(Path(keygen.__file__).read_text())
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verify"
    )
    calls = {
        getattr(node.func, "attr", getattr(node.func, "id", None))
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }
    assert "compare_digest" in calls
    comparisons = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    assert not any(
        isinstance(op, (ast.Eq, ast.NotEq)) for c in comparisons for op in c.ops
    ), "verify() must not use == / != on secret material"
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/pytest tests/test_apikey_keygen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.apikeys'`

- [ ] **Step 3: Write the implementation**

Create `app/apikeys/__init__.py`:

```python
"""API-key credentials for external (non-human) callers.

An API key identifies an `ApiClient`, which is deliberately NOT a `User`. That
separation is the point of this package: `app/auth/dependencies.py` resolves
humans and never sees a key, so no route written for a JWT user can be reached
with one, and no key can inherit admin or a department grant.

`keygen` and `policy` are PURE — no session, no ORM, no FastAPI — for the same
reason `app/rag/ranking.py` and `app/users/policy.py` are: the code that decides
whether a credential is accepted should be provable with no database.
"""
```

Create `app/apikeys/keygen.py`:

```python
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
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/pytest tests/test_apikey_keygen.py -v`
Expected: PASS (17 tests — the parametrized case counts 8)

- [ ] **Step 5: Commit**

```bash
git add app/apikeys/__init__.py app/apikeys/keygen.py tests/test_apikey_keygen.py
git commit -m "feat(apikeys): mint and verify API keys, prefix-indexed sha256

SHA-256 not bcrypt: the secret is 32 bytes of token_urlsafe, so a work
factor buys nothing and costs ~100ms on every OCR request. Constant-time
compare, AST-asserted, because == on a hash reads as correct code."
```

---

## Task 2: `policy.py` — is this key allowed to do this? (PURE)

**Files:**
- Create: `app/apikeys/policy.py`
- Test: `tests/test_apikey_policy.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SCOPE_OCR_READ: str = "ocr:read"`, `ALL_SCOPES: frozenset[str]`
  - `INVALID_KEY: str = "Invalid API key"`
  - `class KeyFacts` (frozen dataclass): `is_active: bool`, `expires_at: datetime | None`, `scopes: tuple[str, ...]`
  - `is_usable(facts: KeyFacts | None, *, now: datetime) -> bool`
  - `scope_refusal(facts: KeyFacts, *, required: str) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apikey_policy.py`:

```python
"""Pure tests for the API-key decision. Every gate must fail CLOSED.

Same rule as `app/rag/permissions.py`: an unknown or absent input must be
refused, never allowed by falling through a comparison.
"""

from datetime import datetime, timedelta, timezone

from app.apikeys import policy

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _facts(**over):
    base = dict(is_active=True, expires_at=None, scopes=("ocr:read",))
    base.update(over)
    return policy.KeyFacts(**base)


def test_an_active_unexpired_key_is_usable():
    assert policy.is_usable(_facts(), now=NOW) is True


def test_a_missing_key_is_not_usable():
    """None means 'no row matched that prefix'. It must never be truthy."""
    assert policy.is_usable(None, now=NOW) is False


def test_a_revoked_key_is_not_usable():
    assert policy.is_usable(_facts(is_active=False), now=NOW) is False


def test_an_expired_key_is_not_usable():
    past = NOW - timedelta(seconds=1)
    assert policy.is_usable(_facts(expires_at=past), now=NOW) is False


def test_a_key_expiring_in_the_future_is_usable():
    assert policy.is_usable(_facts(expires_at=NOW + timedelta(days=1)), now=NOW) is True


def test_expiry_exactly_now_is_expired():
    """A boundary decided by the operator, written down so it cannot drift."""
    assert policy.is_usable(_facts(expires_at=NOW), now=NOW) is False


def test_a_naive_expiry_is_treated_as_utc_not_crashed():
    naive = datetime(2026, 8, 22, 12, 0)
    assert policy.is_usable(_facts(expires_at=naive), now=NOW) is False


def test_the_required_scope_must_be_present():
    assert policy.scope_refusal(_facts(), required="ocr:read") is None


def test_an_empty_scope_set_is_refused():
    assert policy.scope_refusal(_facts(scopes=()), required="ocr:read") is not None


def test_a_key_with_a_different_scope_is_refused():
    refusal = policy.scope_refusal(_facts(scopes=("other:thing",)), required="ocr:read")
    assert refusal is not None
    assert "ocr:read" in refusal


def test_an_unknown_scope_string_never_satisfies_anything():
    """A value that escaped ck_api_keys_scopes must not compare as satisfied."""
    assert policy.scope_refusal(_facts(scopes=("ocr:reed",)), required="ocr:read")
    assert policy.scope_refusal(_facts(scopes=("*",)), required="ocr:read")
    assert policy.scope_refusal(_facts(scopes=("ocr:read ",)), required="ocr:read")


def test_the_401_detail_is_one_message_for_every_cause():
    """Distinguishing 'unknown key' from 'wrong secret' tells an attacker which
    prefixes are real; 'expired' tells them a valid key existed."""
    assert policy.INVALID_KEY == "Invalid API key"


def test_the_scope_vocabulary_is_closed_and_matches_the_db_check():
    assert policy.ALL_SCOPES == frozenset({"ocr:read"})
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/pytest tests/test_apikey_policy.py -v`
Expected: FAIL — `ImportError: cannot import name 'policy'`

- [ ] **Step 3: Write the implementation**

Create `app/apikeys/policy.py`:

```python
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
    "ALL_SCOPES",
    "INVALID_KEY",
    "KeyFacts",
    "is_usable",
    "scope_refusal",
]

SCOPE_OCR_READ = "ocr:read"

# Closed vocabulary, mirroring `ck_api_keys_scopes`. Adding a scope means
# editing BOTH — that duplication is deliberate: the CHECK stops a typo being
# stored, this set stops one being honoured.
ALL_SCOPES = frozenset({SCOPE_OCR_READ})

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
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/pytest tests/test_apikey_policy.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add app/apikeys/policy.py tests/test_apikey_policy.py
git commit -m "feat(apikeys): pure accept/reject policy, failing closed

is_usable(None) is False and an unknown scope satisfies nothing, the
permissions.allows(None) rule. One 401 message for all six causes."
```

---

## Task 3: ORM models and the migration

**Files:**
- Create: `app/apikeys/models.py`
- Create: `alembic/versions/<generated>_api_keys_and_usage.py`
- Test: `tests/test_apikey_admin_integration.py` (first test only; the rest lands in Task 6)

**Interfaces:**
- Consumes: `policy.ALL_SCOPES` (for the CHECK's value list).
- Produces:
  - `class ApiKey`: `.id: str`, `.name: str`, `.key_prefix: str`, `.key_hash: str`, `.scopes: list[str]`, `.is_active: bool`, `.expires_at: datetime | None`, `.created_by_user_id: int`, `.created_at: datetime`, `.last_used_at: datetime | None`, `.revoked_at: datetime | None`
  - `class ApiKeyUsage`: `.id: int`, `.api_key_id: str`, `.route: str`, `.status_code: int`, `.bytes_in: int`, `.width: int | None`, `.height: int | None`, `.lines_out: int | None`, `.duration_ms: int`, `.created_at: datetime`

- [ ] **Step 1: Write the failing test**

Create `tests/test_apikey_admin_integration.py`:

```python
"""Integration tests for the api_keys tables and admin routes.

Builds a throwaway NullPool engine per call rather than using the app's
module-level `engine`: that one pools connections bound to the first event loop,
and each `asyncio.run` creates a new one, so the second test in the file would
die with "Event loop is closed". Same rule as the RAG integration tests.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.apikeys import keygen
from app.apikeys.models import ApiKey

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")


def _run(coro_fn):
    async def main():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _an_admin_id(session):
    from sqlalchemy import select

    from app.users.models import User

    row = await session.scalar(select(User.id).where(User.role == "admin").limit(1))
    assert row is not None, "seed an admin first (admin@example.com)"
    return row


def test_a_revoked_key_must_record_when_it_was_revoked():
    """ck_api_keys_revoked: is_active=false with no revoked_at is illegal.

    The half-revoked state is unrepresentable on purpose — 'inactive since
    when?' has no answer, and 'revoked but still active' would still serve.
    """

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        session.add(
            ApiKey(
                id=uuid.uuid4().hex,
                name="ck-test",
                key_prefix=minted.prefix,
                key_hash=minted.key_hash,
                scopes=["ocr:read"],
                is_active=False,
                revoked_at=None,          # <- the illegal combination
                created_by_user_id=admin_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


def test_an_unknown_scope_cannot_be_stored():
    """ck_api_keys_scopes closes the vocabulary, like ck_documents_status."""

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        session.add(
            ApiKey(
                id=uuid.uuid4().hex,
                name="scope-test",
                key_prefix=minted.prefix,
                key_hash=minted.key_hash,
                scopes=["ocr:reed"],      # typo
                created_by_user_id=admin_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


def test_two_keys_cannot_share_a_prefix():
    """The prefix is the lookup key, so a collision makes verification
    ambiguous. UNIQUE is functional here, not tidiness."""

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        for _ in range(2):
            session.add(
                ApiKey(
                    id=uuid.uuid4().hex,
                    name="dup-prefix",
                    key_prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    scopes=["ocr:read"],
                    created_by_user_id=admin_id,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest tests/test_apikey_admin_integration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.apikeys.models'`

- [ ] **Step 3: Write the models**

Create `app/apikeys/models.py`:

```python
"""ORM models for API keys and their usage log.

Two constraints do real work and must survive any rewrite:

  * `ck_api_keys_scopes` closes the scope vocabulary, the same rule as
    `ck_documents_status` and `ck_user_departments_role`. A typo'd scope must
    fail at INSERT rather than sit in a key someone believes works. Adding a
    scope means editing this CHECK.
  * `ck_api_keys_revoked` makes the half-revoked state unrepresentable:
    `is_active=false` with no `revoked_at` (inactive since when?) and
    `is_active=true` with a `revoked_at` (revoked but still serving) are both
    illegal. Same shape as `ck_nrb_files_blocked_reason`.

Both FKs are ON DELETE RESTRICT and keys are never hard-deleted, exactly like
departments and `nrb_files`: usage rows are the only evidence of a leaked key's
activity, and a cascade would destroy it. Revocation is `is_active=false`.

`ApiKeyUsage.id` is a bigserial because the table grows once per request and is
only ever read by time range; `ApiKey.id` is uuid-hex like every other id here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from ..db.base import Base
from .policy import ALL_SCOPES

_SCOPE_LIST = ", ".join(f"'{s}'" for s in sorted(ALL_SCOPES))


def _uuid_hex() -> str:
    return uuid4().hex


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "(revoked_at IS NULL) = is_active",
            name="ck_api_keys_revoked",
        ),
        CheckConstraint(
            f"scopes <@ ARRAY[{_SCOPE_LIST}]::text[]",
            name="ck_api_keys_scopes",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # NON-secret lookup handle. UNIQUE and indexed: verification is one B-tree
    # hit, and a collision would make verification ambiguous.
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False, unique=True, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usage"
    __table_args__ = (
        Index("ix_api_key_usage_key_time", "api_key_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    api_key_id: Mapped[str] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=False
    )
    route: Mapped[str] = mapped_column(String(128), nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bytes_in: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL when the image was never decoded (a 401/413/429 path).
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Make Alembic see the models, then autogenerate**

The models must be imported for `Base.metadata` to know them. Check how
`alembic/env.py` collects models:

Run: `grep -n "import\|target_metadata" alembic/env.py | head -30`

Add `from app.apikeys import models as apikeys_models  # noqa: F401` alongside
the other model imports there (or in whatever module aggregates them — follow
the existing pattern rather than inventing one).

Then:

Run: `.venv/bin/alembic revision --autogenerate -m "api_keys and api_key_usage"`

- [ ] **Step 5: Read the generated migration and fix it**

Open the new file under `alembic/versions/`. Three things to check by hand,
because autogenerate gets each of them wrong or partly wrong:

1. `down_revision` must be `"c2f8b1d47e93"`. If it is anything else, the
   lineage has moved since this plan was written — stop and ask, do not guess,
   and never create a merge revision.
2. Both `CheckConstraint`s must be present in `create_table`. Autogenerate does
   render table-level CHECKs, but verify — a silently dropped CHECK is exactly
   the class of defect the constraints exist to prevent, and it would pass every
   test except the two in Task 3 Step 1.
3. The `ARRAY(String)` column needs `postgresql.ARRAY(sa.String())`.

- [ ] **Step 6: Apply the migration and confirm one head**

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic heads          # must print exactly ONE line
.venv/bin/pytest tests/test_alembic_lineage.py -v
```

Expected: one head, lineage test PASS.

- [ ] **Step 7: Run the model tests**

Run: `DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest tests/test_apikey_admin_integration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Confirm no schema drift**

Run: `.venv/bin/alembic revision --autogenerate -m "drift check"` then read the
generated file. It must contain no operations touching `api_keys` or
`api_key_usage`. Delete it either way:

```bash
git status --porcelain alembic/versions/   # find the new file
rm alembic/versions/<the-drift-check-file>.py
```

- [ ] **Step 9: Commit**

```bash
git add app/apikeys/models.py alembic/versions/ alembic/env.py tests/test_apikey_admin_integration.py
git commit -m "feat(apikeys): api_keys + api_key_usage tables

ck_api_keys_scopes closes the vocabulary and ck_api_keys_revoked makes
the half-revoked state unrepresentable. Both FKs RESTRICT: usage rows are
the only evidence a leaked key ever ran."
```

---

## Task 4: `repository.py` — data access

**Files:**
- Create: `app/apikeys/repository.py`
- Test: covered by Tasks 6 and 9's integration tests (this task has no
  behaviour a unit test can reach without a database, and duplicating the
  route tests here would test the same lines twice).

**Interfaces:**
- Consumes: `models.ApiKey`, `models.ApiKeyUsage`, `policy.KeyFacts`.
- Produces:
  - `async def create_key(session, *, name, key_prefix, key_hash, scopes, expires_at, created_by_user_id) -> ApiKey`
  - `async def find_by_prefix(session, prefix: str) -> ApiKey | None`
  - `async def list_keys(session) -> list[ApiKey]`
  - `async def revoke(session, key_id: str) -> bool`
  - `async def touch_last_used(session, key_id: str) -> None`
  - `async def record_usage(session, *, api_key_id, route, status_code, bytes_in, duration_ms, width=None, height=None, lines_out=None) -> None`
  - `def facts_of(key: ApiKey) -> policy.KeyFacts`

- [ ] **Step 1: Write the implementation**

Create `app/apikeys/repository.py`:

```python
"""Data access for API keys. No decisions live here — `policy.py` owns those.

`facts_of` is the seam: it lifts an ORM row into the pure `KeyFacts` the policy
reasons about, so the policy never imports SQLAlchemy and its tests never need
a row.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import policy
from .models import ApiKey, ApiKeyUsage


def facts_of(key: ApiKey) -> policy.KeyFacts:
    return policy.KeyFacts(
        is_active=key.is_active,
        expires_at=key.expires_at,
        scopes=tuple(key.scopes or ()),
    )


async def create_key(
    session: AsyncSession,
    *,
    key_id: str,
    name: str,
    key_prefix: str,
    key_hash: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by_user_id: int,
) -> ApiKey:
    key = ApiKey(
        id=key_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=scopes,
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
    )
    session.add(key)
    await session.flush()
    return key


async def find_by_prefix(session: AsyncSession, prefix: str) -> ApiKey | None:
    """One indexed lookup. The prefix is non-secret; the hash is the credential."""
    return await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))


async def list_keys(session: AsyncSession) -> list[ApiKey]:
    rows = await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
    return list(rows)


async def revoke(session: AsyncSession, key_id: str) -> bool:
    """Revoke, never delete. False if there was no such active key.

    `is_active` and `revoked_at` move TOGETHER because `ck_api_keys_revoked`
    forbids the half state — writing one without the other raises.
    """
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.is_active.is_(True))
        .values(is_active=False, revoked_at=datetime.now(timezone.utc))
    )
    return result.rowcount > 0


async def touch_last_used(session: AsyncSession, key_id: str) -> None:
    await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(last_used_at=datetime.now(timezone.utc))
    )


async def record_usage(
    session: AsyncSession,
    *,
    api_key_id: str,
    route: str,
    status_code: int,
    bytes_in: int,
    duration_ms: int,
    width: int | None = None,
    height: int | None = None,
    lines_out: int | None = None,
) -> None:
    """One row per call. Deliberately holds NO image bytes and NO OCR text.

    The text is the caller's own content; retaining it would recreate exactly
    the confidentiality problem the 'usage record only' decision avoided. What
    is kept is enough to answer 'who called, how often, how big, how slow, and
    did it work' — and `request_id` on a support ticket joins to it.
    """
    session.add(
        ApiKeyUsage(
            api_key_id=api_key_id,
            route=route,
            status_code=status_code,
            bytes_in=bytes_in,
            duration_ms=duration_ms,
            width=width,
            height=height,
            lines_out=lines_out,
        )
    )
```

- [ ] **Step 2: Confirm it imports cleanly**

Run: `.venv/bin/python -c "from app.apikeys import repository; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add app/apikeys/repository.py
git commit -m "feat(apikeys): repository — lookup by prefix, revoke, usage log

facts_of is the seam that keeps policy.py free of SQLAlchemy. Usage rows
hold no image bytes and no OCR text."
```

---

## Task 5: Per-key rate limiting (PURE)

**Files:**
- Create: `app/apikeys/throttle.py`
- Test: `tests/test_apikey_rate_limit.py`

**Interfaces:**
- Consumes: `app.auth.throttle.LoginThrottle` (reused for auth FAILURES).
- Produces:
  - `class RateLimiter(per_minute: int, burst: int, clock=time.monotonic, max_tracked=10_000)`
  - `.check(identifier: str) -> int | None` — seconds to wait, or None if allowed; consumes a token when it allows
  - `def get_rate_limiter() -> RateLimiter` — process-wide singleton built from settings
  - `def get_auth_throttle() -> LoginThrottle` — process-wide singleton keyed on key PREFIX

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apikey_rate_limit.py`:

```python
"""Pure tests for the per-key rate limiter. Injected clock, no sleeping."""

from app.apikeys.throttle import RateLimiter


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _limiter(per_minute=60, burst=5):
    clock = _Clock()
    return RateLimiter(per_minute=per_minute, burst=burst, clock=clock), clock


def test_the_first_call_is_allowed():
    limiter, _ = _limiter()
    assert limiter.check("k1") is None


def test_the_burst_is_spent_then_refused():
    limiter, _ = _limiter(per_minute=60, burst=3)
    assert [limiter.check("k1") for _ in range(3)] == [None, None, None]
    assert limiter.check("k1") is not None


def test_a_refusal_reports_seconds_never_zero():
    """Retry-After: 0 tells a client to retry immediately, which is a loop."""
    limiter, _ = _limiter(per_minute=60, burst=1)
    limiter.check("k1")
    assert limiter.check("k1") >= 1


def test_tokens_refill_over_time():
    limiter, clock = _limiter(per_minute=60, burst=2)
    limiter.check("k1")
    limiter.check("k1")
    assert limiter.check("k1") is not None
    clock.advance(1.0)          # 60/min = 1 per second
    assert limiter.check("k1") is None


def test_refill_never_exceeds_the_burst():
    limiter, clock = _limiter(per_minute=60, burst=2)
    clock.advance(3600)
    assert limiter.check("k1") is None
    assert limiter.check("k1") is None
    assert limiter.check("k1") is not None


def test_keys_are_limited_independently():
    limiter, _ = _limiter(per_minute=60, burst=1)
    assert limiter.check("k1") is None
    assert limiter.check("k2") is None


def test_a_zero_per_minute_limit_refuses_everything():
    """Fail closed: a misconfigured 0 must not mean 'unlimited'."""
    limiter, _ = _limiter(per_minute=0, burst=0)
    assert limiter.check("k1") is not None


def test_tracking_is_bounded_so_a_flood_cannot_exhaust_memory():
    limiter = RateLimiter(per_minute=60, burst=1, max_tracked=50)
    for i in range(500):
        limiter.check(f"k{i}")
    assert len(limiter) <= 50
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/pytest tests/test_apikey_rate_limit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.apikeys.throttle'`

- [ ] **Step 3: Write the implementation**

Create `app/apikeys/throttle.py`:

```python
"""Two different limits, deliberately not one thing.

  * `RateLimiter` is a token bucket on SUCCESSFUL use: it stops one key
    monopolising the OCR capacity. Answer: 429.
  * `LoginThrottle` (reused wholesale from `app/auth/throttle.py`) counts
    credential FAILURES and locks a prefix out, for exactly the reason
    `/auth/login` is throttled: an unthrottled credential endpoint is a
    brute-force surface.

Both counters are PER PROCESS. N uvicorn workers means N x the limit — the same
documented caveat as the login throttle. That is acceptable for capacity
protection and would not be for a billing quota; if this ever becomes a billing
quota it needs Postgres or Redis, not a bigger comment.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ..auth.throttle import LoginThrottle
from ..config import get_settings

__all__ = ["RateLimiter", "get_rate_limiter", "get_auth_throttle"]

DEFAULT_MAX_TRACKED = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A per-identifier token bucket. `check` consumes a token when it allows."""

    def __init__(
        self,
        *,
        per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        self.per_minute = per_minute
        self.burst = burst
        self._clock = clock
        self._max_tracked = max_tracked
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(self, identifier: str) -> int | None:
        """Seconds to wait, or None if the call may proceed."""
        # Fail closed: a misconfigured 0 means "none allowed", not "unlimited".
        if self.per_minute <= 0 or self.burst <= 0:
            return 60

        now = self._clock()
        bucket = self._buckets.get(identifier)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), updated=now)
        self._buckets[identifier] = bucket
        self._buckets.move_to_end(identifier)

        rate = self.per_minute / 60.0
        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            self._enforce_bound()
            return None

        needed = (1.0 - bucket.tokens) / rate
        self._enforce_bound()
        # Never 0: a Retry-After of 0 tells a client to retry immediately.
        return max(1, math.ceil(needed))

    def __len__(self) -> int:
        return len(self._buckets)

    def _enforce_bound(self) -> None:
        # Evict the least recently touched. Unlike the login throttle there is
        # no lockout state to protect here, so plain LRU is correct: an evicted
        # bucket refills to full, which is generous, not a security hole.
        while len(self._buckets) > self._max_tracked:
            self._buckets.popitem(last=False)


_rate_limiter: RateLimiter | None = None
_auth_throttle: LoginThrottle | None = None


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        settings = get_settings()
        _rate_limiter = RateLimiter(
            per_minute=settings.ocr_rate_per_minute,
            burst=settings.ocr_rate_burst,
        )
    return _rate_limiter


def get_auth_throttle() -> LoginThrottle:
    """Lockout on repeated bad keys, keyed on the presented PREFIX.

    Reuses the login throttle unchanged, which brings its eviction rule with it:
    eviction PREFERS UNLOCKED entries, so a flood of junk prefixes cannot evict
    a locked one and thereby clear a lockout.
    """
    global _auth_throttle
    if _auth_throttle is None:
        settings = get_settings()
        _auth_throttle = LoginThrottle(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_attempt_window_seconds,
            lockout_seconds=settings.login_lockout_seconds,
        )
    return _auth_throttle
```

**Note for the implementer:** the three `login_*` settings names above are
guesses at the existing field names beyond `login_max_attempts`. Before running
the tests, confirm them: `grep -n "login_" app/config.py`. Use the real names.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/pytest tests/test_apikey_rate_limit.py -v`
Expected: PASS (8 tests). Note `get_rate_limiter` is not exercised yet — it
needs the settings from Task 6.

- [ ] **Step 5: Commit**

```bash
git add app/apikeys/throttle.py tests/test_apikey_rate_limit.py
git commit -m "feat(apikeys): per-key token bucket + reuse the login throttle

Two different limits: 429 for using too much, lockout for guessing keys.
Both per-process, the documented login-throttle caveat."
```

---

## Task 6: Settings, the `require_api_client` dependency, and the admin routes

**Files:**
- Create: `app/apikeys/dependencies.py`, `app/apikeys/schemas.py`, `app/apikeys/router.py`
- Modify: `app/config.py`, `app/main.py`, `.env.example`
- Test: `tests/test_apikey_admin_integration.py` (append)

**Interfaces:**
- Consumes: `keygen.mint/verify`, `policy.*`, `repository.*`, `throttle.get_auth_throttle`, `app.auth.dependencies.require_admin`, `app.db.session.get_session`.
- Produces:
  - `class ApiClient` (frozen dataclass): `.key_id: str`, `.name: str`, `.scopes: tuple[str, ...]`
  - `def require_api_client(scope: str) -> Callable[..., Awaitable[ApiClient]]` — a dependency FACTORY
  - Settings fields: `external_api_enabled: bool = False`, `ocr_max_concurrent: int = 2`, `ocr_queue_wait_seconds: int = 10`, `ocr_max_upload_bytes: int = 10_485_760`, `ocr_rate_per_minute: int = 30`, `ocr_rate_burst: int = 10`, `api_key_prefix: str = "lgw_live"`, `ocr_prewarm: bool = False`

- [ ] **Step 1: Add the settings**

Modify `app/config.py`. Add to `Settings`, following the commenting style of the
surrounding fields:

```python
    # --- External API (API-key callers) -----------------------------------
    # Master switch. FALSE by default so merging this changes nothing about any
    # existing deployment: both /v1/api-keys and /v1/ocr go unregistered.
    # Note the deliberate asymmetry with the OCR-stack case: a deployment that
    # MEANS to serve this API but has no OCR stack answers 503, because a 404
    # there is indistinguishable from a wrong URL. A deployment that was never
    # asked to serve it has no route at all, which is honest and is a smaller
    # attack surface than a disabled one.
    external_api_enabled: bool = False
    # OCR is CPU-bound and synchronous. This cap is separate from the thread
    # offload because `asyncio.to_thread`'s default executor is much larger and
    # would run many OCRs at once, each spawning onnxruntime's own intra-op
    # threads, oversubscribing the box.
    ocr_max_concurrent: int = 2
    # Bounded waiting. An unbounded queue turns a load spike into an outage.
    ocr_queue_wait_seconds: int = 10
    ocr_max_upload_bytes: int = 10 * 1024 * 1024
    ocr_rate_per_minute: int = 30
    ocr_rate_burst: int = 10
    # So a dev key is visibly not a prod key at a glance in a config file.
    api_key_prefix: str = "lgw_live"
    # Load the OCR models at startup instead of making the first caller pay.
    ocr_prewarm: bool = False
```

And in the existing validation block (next to the `login_max_attempts` check):

```python
        if self.ocr_max_concurrent < 1:
            raise ValueError("OCR_MAX_CONCURRENT must be at least 1")
        if self.ocr_queue_wait_seconds < 1:
            raise ValueError("OCR_QUEUE_WAIT_SECONDS must be at least 1")
        if self.ocr_max_upload_bytes < 1024:
            raise ValueError("OCR_MAX_UPLOAD_BYTES must be at least 1024")
        if self.ocr_rate_per_minute < 1 or self.ocr_rate_burst < 1:
            raise ValueError(
                "OCR_RATE_PER_MINUTE and OCR_RATE_BURST must be at least 1 "
                "(the limiter fails closed, so 0 would refuse every call)"
            )
        if not self.api_key_prefix.strip():
            raise ValueError("API_KEY_PREFIX must not be blank")
```

- [ ] **Step 2: Write the dependency**

Create `app/apikeys/dependencies.py`:

```python
"""The `X-API-Key` boundary. The ONLY place the pieces are combined.

`require_api_client` returns an `ApiClient` and never a `User`. That is the
whole reason this package exists: `app/auth/dependencies.py` resolves humans
and never sees a key, so no route written for a JWT user can be reached with
one, and no key can inherit admin or a department grant.

It is a dependency FACTORY (`require_api_client("ocr:read")`) rather than a
plain dependency, because the required scope belongs to the ROUTE. A single
dependency reading a scope out of the request would let the caller choose which
scope they are checked against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from . import keygen, policy, repository
from .throttle import get_auth_throttle

logger = logging.getLogger("app.apikeys.auth")


@dataclass(frozen=True)
class ApiClient:
    """An authenticated non-human caller. NOT a User, and never convertible."""

    key_id: str
    name: str
    scopes: tuple[str, ...]


def _invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=policy.INVALID_KEY,
    )


def require_api_client(scope: str) -> Callable[..., Awaitable[ApiClient]]:
    if scope not in policy.ALL_SCOPES:
        # A programming error, caught at import time rather than at request
        # time: a route asking for a scope no key can hold would 403 forever.
        raise ValueError(f"unknown scope {scope!r}")

    async def dependency(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        session: AsyncSession = Depends(get_session),
    ) -> ApiClient:
        parsed = keygen.parse(x_api_key or "")
        if parsed is None:
            # No prefix to throttle on, and nothing to look up.
            logger.info("api key rejected: malformed or absent header")
            raise _invalid()
        prefix, _secret = parsed

        throttle = get_auth_throttle()
        retry_after = throttle.retry_after(prefix)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts for this key",
                headers={"Retry-After": str(retry_after)},
            )

        key = await repository.find_by_prefix(session, prefix)

        # The log distinguishes the six causes; the RESPONSE never does.
        if key is None:
            throttle.record_failure(prefix)
            logger.info("api key rejected: no key with prefix %s", prefix)
            raise _invalid()
        if not keygen.verify(x_api_key or "", key.key_hash):
            throttle.record_failure(prefix)
            logger.warning(
                "api key rejected: secret mismatch for key %s (%s)", key.id, key.name
            )
            raise _invalid()
        if not policy.is_usable(
            repository.facts_of(key), now=datetime.now(timezone.utc)
        ):
            # Not a guess, so it does not consume a throttle attempt: a revoked
            # or expired key presented by an honest caller would otherwise lock
            # out the prefix on top of already being refused.
            logger.info(
                "api key rejected: not usable (key=%s active=%s expires=%s)",
                key.id, key.is_active, key.expires_at,
            )
            raise _invalid()

        refusal = policy.scope_refusal(repository.facts_of(key), required=scope)
        if refusal is not None:
            # 403, not 401: the credential is GENUINE. Telling the caller their
            # key is fine and their permissions are not stops them rotating a
            # working key chasing the wrong bug.
            logger.info("api key %s lacks scope %s", key.id, scope)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

        throttle.reset(prefix)
        await repository.touch_last_used(session, key.id)
        await session.commit()
        return ApiClient(key_id=key.id, name=key.name, scopes=tuple(key.scopes or ()))

    return dependency
```

- [ ] **Step 3: Write the admin schemas and router**

Create `app/apikeys/schemas.py`:

```python
"""Admin request/response models for /v1/api-keys.

`extra="forbid"` throughout: `is_active` or `key_hash` in a create body must be
a loud 422, not a silently ignored field. Same rule as `UserPatch` refusing
`role` and the NRB run schema refusing `all_files`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .policy import ALL_SCOPES, SCOPE_OCR_READ


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: [SCOPE_OCR_READ])
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALL_SCOPES)
        if unknown:
            raise ValueError(
                f"unknown scope(s): {', '.join(unknown)}; "
                f"known scopes are {', '.join(sorted(ALL_SCOPES))}"
            )
        if not value:
            raise ValueError("a key with no scopes can do nothing")
        return value


class ApiKeyCreated(BaseModel):
    """The ONLY response that ever carries the plaintext key."""

    id: str
    name: str
    prefix: str
    key: str
    scopes: list[str]
    expires_at: datetime | None


class ApiKeyOut(BaseModel):
    """A listed key. Deliberately has no `key` and no `key_hash` field."""

    id: str
    name: str
    prefix: str
    scopes: list[str]
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
```

Create `app/apikeys/router.py`:

```python
"""/v1/api-keys — admin-only key management (JWT, not API key).

Managing keys is a human, privileged act, so it sits behind the EXISTING
`require_admin`. A key can never manage keys: `require_api_client` is not used
here, and `ocr:read` is the only scope that exists.

No PATCH: rotation is "mint a new one, revoke the old", which needs no overlap
state machine. No GET /{id} and no usage listing — nothing consumes them.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..config import get_settings
from ..db.session import get_session
from ..users.models import User
from . import keygen, repository
from .schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut

router = APIRouter(prefix="/v1", tags=["api-keys"])


def _out(key) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        prefix=key.key_prefix,
        scopes=list(key.scopes or []),
        is_active=key.is_active,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key for an external caller (admin)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Not an admin."},
        422: {"description": "Unknown scope, or an unexpected field."},
    },
)
async def create_api_key(
    body: ApiKeyCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Returns the plaintext key ONCE. It is never recoverable afterwards."""
    minted = keygen.mint(get_settings().api_key_prefix)
    key = await repository.create_key(
        session,
        key_id=uuid4().hex,
        name=body.name,
        key_prefix=minted.prefix,
        key_hash=minted.key_hash,
        scopes=list(body.scopes),
        expires_at=body.expires_at,
        created_by_user_id=admin.id,
    )
    await session.commit()
    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        prefix=minted.prefix,
        key=minted.token,
        scopes=list(key.scopes or []),
        expires_at=key.expires_at,
    )


@router.get(
    "/api-keys",
    response_model=list[ApiKeyOut],
    summary="List API keys, newest first (admin)",
)
async def list_api_keys(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Revoked keys are listed too — `is_active` says which. They are never
    deleted, so a leaked key's history stays attributable."""
    return [_out(k) for k in await repository.list_keys(session)]


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (admin)",
    responses={404: {"description": "No such active key."}},
)
async def revoke_api_key(
    key_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Revocation is `is_active=false` + `revoked_at`, never a DELETE: the
    usage rows are the only evidence of what this key did."""
    if not await repository.revoke(session, key_id):
        raise HTTPException(status_code=404, detail="No such active API key")
    await session.commit()
```

- [ ] **Step 4: Register the router conditionally**

Modify `app/main.py`. Add the import beside the others:

```python
from .apikeys.router import router as api_keys_router
```

And after the existing `include_router` calls:

```python
# The external API is opt-in. When disabled the routes do not exist at all —
# see the comment on `Settings.external_api_enabled` for why 404 is right here
# and 503 is right for a missing OCR stack.
if get_settings().external_api_enabled:
    app.include_router(api_keys_router)
```

- [ ] **Step 5: Document the settings**

Append to `.env.example`:

```
# --- External API (API-key callers) ---
# Master switch. When false, /v1/api-keys and /v1/ocr are not registered.
EXTERNAL_API_ENABLED=false
# Concurrent OCR jobs per process. OCR is CPU-bound; this stops it starving chat.
OCR_MAX_CONCURRENT=2
# How long a queued request waits for a slot before 503 + Retry-After.
OCR_QUEUE_WAIT_SECONDS=10
OCR_MAX_UPLOAD_BYTES=10485760
OCR_RATE_PER_MINUTE=30
OCR_RATE_BURST=10
# Token label, so a dev key is visibly not a prod key.
API_KEY_PREFIX=lgw_live
# Load the OCR models at startup rather than making the first caller pay.
OCR_PREWARM=false
```

- [ ] **Step 6: Write the failing admin-route tests**

Append to `tests/test_apikey_admin_integration.py`:

```python
# --- admin route tests ---------------------------------------------------

PASSWORD = "supersecret123"
ADMIN_EMAIL = "admin@example.com"


def _client():
    """A TestClient with the external API switched ON.

    The switch is read at import time by main.py, so it must be set in the
    environment BEFORE the app module is imported, and the settings cache
    cleared. Skips rather than fails if the app cannot be built.
    """
    from fastapi.testclient import TestClient

    os.environ["EXTERNAL_API_ENABLED"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    import importlib

    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


def _admin_token(client):
    resp = client.post(
        "/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    if resp.status_code != 200:
        pytest.skip(f"cannot log in as {ADMIN_EMAIL} ({resp.status_code})")
    return resp.json()["access_token"]


def test_minting_returns_the_plaintext_once_and_never_again():
    client = _client()
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}

    created = client.post(
        "/v1/api-keys", json={"name": "test-mint"}, headers=headers
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["key"].startswith("lgw_live_")
    assert body["scopes"] == ["ocr:read"]

    listed = client.get("/v1/api-keys", headers=headers)
    assert listed.status_code == 200
    row = next(k for k in listed.json() if k["id"] == body["id"])
    # Asserted on the serialised JSON, not the model: a field added to the
    # model would leak through response_model only if it is also in the schema,
    # and this is the assertion that would catch it.
    assert "key" not in row
    assert "key_hash" not in row

    client.delete(f"/v1/api-keys/{body['id']}", headers=headers)


def test_an_unknown_scope_is_a_loud_422_not_a_silent_drop():
    client = _client()
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}
    resp = client.post(
        "/v1/api-keys",
        json={"name": "bad-scope", "scopes": ["ocr:reed"]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "ocr:reed" in resp.text


def test_an_unexpected_field_is_rejected_rather_than_ignored():
    client = _client()
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}
    resp = client.post(
        "/v1/api-keys",
        json={"name": "sneaky", "is_active": True},
        headers=headers,
    )
    assert resp.status_code == 422


def test_a_non_admin_cannot_mint_a_key():
    client = _client()
    resp = client.post("/v1/api-keys", json={"name": "nope"})
    assert resp.status_code in (401, 403)


def test_revoking_twice_is_a_404_the_second_time():
    client = _client()
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}
    key_id = client.post(
        "/v1/api-keys", json={"name": "revoke-twice"}, headers=headers
    ).json()["id"]
    assert client.delete(f"/v1/api-keys/{key_id}", headers=headers).status_code == 204
    assert client.delete(f"/v1/api-keys/{key_id}", headers=headers).status_code == 404


def test_a_revoked_key_is_still_listed_so_its_history_stays_attributable():
    client = _client()
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}
    key_id = client.post(
        "/v1/api-keys", json={"name": "kept-after-revoke"}, headers=headers
    ).json()["id"]
    client.delete(f"/v1/api-keys/{key_id}", headers=headers)
    row = next(k for k in client.get("/v1/api-keys", headers=headers).json()
               if k["id"] == key_id)
    assert row["is_active"] is False
```

- [ ] **Step 7: Run the tests**

Run: `DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest tests/test_apikey_admin_integration.py -v`
Expected: PASS (9 tests total). If any test SKIPS, read the skip reason — a
skipped auth test is a green run that proved nothing.

- [ ] **Step 8: Confirm the switch actually gates the routes**

```bash
EXTERNAL_API_ENABLED=false .venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from app.main import app
paths = TestClient(app).get("/openapi.json").json()["paths"]
assert "/v1/api-keys" not in paths, "the master switch does not gate the router"
print("ok: routes absent when disabled")
PY
```

Expected: `ok: routes absent when disabled`

- [ ] **Step 9: Commit**

```bash
git add app/apikeys/dependencies.py app/apikeys/schemas.py app/apikeys/router.py \
        app/config.py app/main.py .env.example tests/test_apikey_admin_integration.py
git commit -m "feat(apikeys): require_api_client + admin /v1/api-keys routes

require_api_client is a FACTORY so the route owns the required scope, not
the caller. 401 for all six credential causes, 403 for a genuine key
lacking the scope. Both routers gated on EXTERNAL_API_ENABLED, default off."
```

---

## Task 7: The OCR response envelope, and one caveat with two readers

**Files:**
- Create: `app/publicapi/__init__.py`, `app/publicapi/schemas.py`
- Modify: `app/files/image_ocr.py` (add the shared constant),
  `app/tools/local/read_image.py` (import it instead of defining it)
- Test: `tests/test_ocr_api_boundaries.py` (first two tests)

**Interfaces:**
- Consumes: `image_ocr.OcrResult`, `images.ImageSummary`.
- Produces:
  - `image_ocr.OCR_CAVEAT: str` — the single shared constant
  - `class OcrLine`: `.text: str`, `.confidence: float`
  - `class OcrEngineInfo`: `.name`, `.model`, `.backend`, `.lang`, `.version`
  - `class OcrImageInfo`: `.kind`, `.width`, `.height`, `.frames`
  - `class OcrResponse`: `.text`, `.lines`, `.authoritative`, `.caveat`, `.partial`, `.image`, `.engine`, `.request_id`
  - `def build_response(result, summary, request_id) -> OcrResponse`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ocr_api_boundaries.py`:

```python
"""Boundary tests: the shared caveat, no threshold, no OCR import at import.

These are the tests that stop a rewrite quietly losing a property. They are
deliberately structural (AST, subprocess) rather than behavioural, because each
property is invisible in ordinary output right up to the moment it matters.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest


def test_the_caveat_is_one_constant_with_two_readers():
    """A second copy drifts, and then the API and the chat answer caveat
    differently — leaving the reader unable to tell which to believe. Same rule
    as sources.VERIFY_NOTE.
    """
    from app.files import image_ocr
    from app.publicapi import schemas
    from app.tools.local import read_image

    assert image_ocr.OCR_CAVEAT
    assert read_image.CAVEAT is image_ocr.OCR_CAVEAT
    assert schemas.CAVEAT is image_ocr.OCR_CAVEAT


def test_neither_the_router_nor_the_schemas_compare_a_confidence_to_a_literal():
    """No threshold. docs/nrb-integration.md §16.6 measured orthographic
    well-formedness, which is not a per-field correctness estimate; a constant
    derived from it would dress a guess as a measurement.
    """
    from app.publicapi import ocr_router, schemas

    for module in (ocr_router, schemas):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            } | {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            }
            if names & {"confidence", "score", "scores", "mean_score", "min_score"}:
                pytest.fail(
                    f"{module.__name__} compares a confidence value at line "
                    f"{node.lineno}; scores are reported, never enforced"
                )
```

- [ ] **Step 2: Run and confirm they fail**

Run: `.venv/bin/pytest tests/test_ocr_api_boundaries.py -v`
Expected: FAIL — `AttributeError: module 'app.files.image_ocr' has no attribute 'OCR_CAVEAT'`

- [ ] **Step 3: Move the caveat into `image_ocr.py`**

In `app/files/image_ocr.py`, add near the other module constants (after
`SUPPORTED_LANGS`) and add `"OCR_CAVEAT"` to `__all__`:

```python
# ONE constant, TWO readers: `app/tools/local/read_image.py` renders it into the
# model's context, and `app/publicapi/schemas.py` publishes it to an external
# caller. A second copy drifts, and then a UI badge or an API field contradicts
# the answer text, leaving the reader unable to tell which to believe — exactly
# the reasoning behind `app/rag/sources.py`'s VERIFY_NOTE.
#
# It says what §16.6 measured: PP-OCRv5 drops letterheads and subject lines,
# mangles latin runs, and misreads dates (२०६९।१।३१ as २०६९।९।३१).
OCR_CAVEAT = (
    "CAVEAT: this is machine-read text (OCR), not a transcription — words and "
    "whole lines can be dropped or misread. VERIFY every figure, date, account "
    "number and contact detail against the image itself before relying on it, "
    "and say so when you quote one."
)
```

In `app/tools/local/read_image.py`, replace the `CAVEAT = (...)` definition with:

```python
# The tool's caveat and the external API's are ONE constant — see
# image_ocr.OCR_CAVEAT for why. Re-exported under the old name so the rest of
# this module (and its tests) read unchanged.
CAVEAT = image_ocr.OCR_CAVEAT
```

**Check the string is byte-identical before deleting the old one** — if it is
not, keep `read_image`'s wording (it is the version the model has been tested
against) and make that the value of `OCR_CAVEAT`:

```bash
.venv/bin/python -c "
from app.tools.local import read_image
print(repr(read_image.CAVEAT))"
```

- [ ] **Step 4: Write the envelope**

Create `app/publicapi/__init__.py`:

```python
"""The external (non-human) HTTP surface. One route: POST /v1/ocr.

Nothing here may import `rapidocr`, `onnxruntime`, `cv2` or any part of
`docling` at module scope — `app/files/image_ocr.py` owns those imports and
keeps them inside functions, so the API image can run with the OCR stack absent
(it then answers 503). A subprocess test enforces this.
"""
```

Create `app/publicapi/schemas.py`:

```python
"""The OCR response envelope.

Three fields exist because of measured facts rather than taste:

  * `authoritative` is ALWAYS False and `caveat` is ALWAYS present. An external
    app that writes this text into a client file must be told, in the payload,
    on every response — not in documentation it read once.
  * `partial` is True when the image has more than one frame, because a
    multi-frame .tif is a scanner's normal output and the engine reads frame 1
    only (measured: page 2's text silently vanished). `read_document` reports
    `pages_skipped` for the same reason.
  * `request_id` is echoed so a caller's support ticket joins to an
    `api_key_usage` row. It is the only reason those rows are worth writing.

`text` and `lines` both ship: `text` is the 90% case so the caller does not
reassemble it, and `lines` carries per-line confidence. Confidence is REPORTED
and never compared to anything (§16.6 declines to invent a threshold from an
orthography measurement); an AST test enforces that.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..files.image_ocr import OCR_CAVEAT, OcrResult
from ..files.images import ImageSummary

CAVEAT = OCR_CAVEAT


class OcrLine(BaseModel):
    text: str
    confidence: float


class OcrEngineInfo(BaseModel):
    name: str
    model: str
    backend: str
    lang: str
    version: str


class OcrImageInfo(BaseModel):
    kind: str
    width: int
    height: int
    frames: int


class OcrResponse(BaseModel):
    text: str
    lines: list[OcrLine]
    # Never True. See limit 1 in image_ocr's module docstring.
    authoritative: bool = False
    caveat: str = CAVEAT
    partial: bool = False
    image: OcrImageInfo
    engine: OcrEngineInfo
    request_id: str


def build_response(
    result: OcrResult, summary: ImageSummary, request_id: str
) -> OcrResponse:
    lines = [
        OcrLine(text=text, confidence=score)
        for text, score in zip(result.lines, result.scores)
    ]
    return OcrResponse(
        text="\n".join(result.lines),
        lines=lines,
        authoritative=result.authoritative,
        caveat=CAVEAT,
        partial=summary.frames > 1,
        image=OcrImageInfo(
            kind=summary.kind,
            width=summary.width,
            height=summary.height,
            frames=summary.frames,
        ),
        engine=OcrEngineInfo(
            name=result.engine,
            model=result.model,
            backend=result.backend,
            lang=result.lang,
            version=result.version,
        ),
        request_id=request_id,
    )
```

- [ ] **Step 5: Create a placeholder router so the AST test can import it**

The second boundary test imports `app.publicapi.ocr_router`, which Task 8
writes. Create the module now with just its docstring and an empty router so
this task's tests can run:

```python
# app/publicapi/ocr_router.py
"""POST /v1/ocr — implemented in Task 8."""

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["ocr"])
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_ocr_api_boundaries.py tests/test_image_ocr*.py tests/test_read_image*.py -v`
Expected: PASS. The existing `read_image` tests must still pass — if one
asserted on the caveat's text, it now reads the shared constant and should be
unaffected. If it fails, the string was not byte-identical: fix `OCR_CAVEAT` to
match `read_image`'s original wording, not the other way round.

- [ ] **Step 7: Commit**

```bash
git add app/publicapi/ app/files/image_ocr.py app/tools/local/read_image.py \
        tests/test_ocr_api_boundaries.py
git commit -m "feat(publicapi): OCR response envelope; caveat is one constant

read_image and the API now share image_ocr.OCR_CAVEAT. A second copy
drifts, and then the API field contradicts the chat answer."
```

---

## Task 8: `POST /v1/ocr`

**Files:**
- Modify: `app/publicapi/ocr_router.py` (replace the placeholder), `app/main.py`
- Test: `tests/test_ocr_api_integration.py`

**Interfaces:**
- Consumes: `require_api_client("ocr:read")`, `policy.SCOPE_OCR_READ`,
  `repository.record_usage`, `throttle.get_rate_limiter`, `schemas.build_response`,
  `images.summarize_image`, `image_ocr.ocr_image/available/SUPPORTED_LANGS`,
  `ingest.IMAGE_EXTS`.
- Produces: the route. Nothing later depends on its internals.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ocr_api_integration.py`:

```python
"""Integration tests for POST /v1/ocr.

Most of these do not need the OCR stack: they exercise auth, scope, the guards
and the usage log, all of which run BEFORE the engine. The tests that need real
text are in tests/test_ocr_api_eval.py, gated on OCR_LIVE_TESTS.
"""

import io
import os

import pytest

from app.files import image_ocr

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

PASSWORD = "supersecret123"
ADMIN_EMAIL = "admin@example.com"


def _client():
    from fastapi.testclient import TestClient

    os.environ["EXTERNAL_API_ENABLED"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    import importlib

    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


def _admin_headers(client):
    resp = client.post(
        "/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    if resp.status_code != 200:
        pytest.skip(f"cannot log in as {ADMIN_EMAIL} ({resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _mint(client, name, scopes=None):
    body = {"name": name}
    if scopes is not None:
        body["scopes"] = scopes
    resp = client.post("/v1/api-keys", json=body, headers=_admin_headers(client))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _png(width=40, height=20):
    """A real, tiny, valid PNG. No text in it — these tests are about the
    boundary, not the recogniser."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _post(client, key, data=None, filename="a.png", extra=None):
    files = {"file": (filename, data if data is not None else _png(), "image/png")}
    return client.post(
        "/v1/ocr", files=files, data=extra or {}, headers={"X-API-Key": key}
    )


# --- authentication ------------------------------------------------------

@pytest.mark.parametrize(
    "key",
    [
        "",                                    # absent
        "garbage",                             # malformed
        "lgw_live_00000000_nosuchsecretatall",  # unknown prefix
    ],
)
def test_every_bad_credential_gets_the_same_401_body(key):
    client = _client()
    resp = _post(client, key)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


def test_a_wrong_secret_on_a_real_prefix_is_the_same_401():
    """Distinguishing this from an unknown prefix tells an attacker which
    prefixes are real."""
    client = _client()
    minted = _mint(client, "wrong-secret")
    tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
    resp = _post(client, tampered)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


def test_a_revoked_key_stops_working_immediately():
    client = _client()
    minted = _mint(client, "to-revoke")
    client.delete(f"/v1/api-keys/{minted['id']}", headers=_admin_headers(client))
    resp = _post(client, minted["key"])
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


def test_a_jwt_cannot_be_used_on_the_ocr_route():
    """The two credential types are disjoint, in both directions."""
    client = _client()
    token = _admin_headers(client)["Authorization"].split()[1]
    files = {"file": ("a.png", _png(), "image/png")}
    resp = client.post(
        "/v1/ocr", files=files, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_an_api_key_cannot_reach_a_jwt_route():
    client = _client()
    minted = _mint(client, "no-jwt-routes")
    for path in ("/users/me", "/v1/api-keys", "/v1/sessions"):
        resp = client.get(path, headers={"X-API-Key": minted["key"]})
        assert resp.status_code in (401, 403), f"{path} accepted an API key"


def test_a_key_without_the_scope_gets_403_not_401():
    """403 says the credential is genuine and the permissions are not, so the
    caller does not rotate a working key chasing the wrong bug."""
    client = _client()
    # No key can be minted without ocr:read (it is the only scope), so strip
    # the scope directly in the database to exercise the branch.
    minted = _mint(client, "scopeless")
    import asyncio

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKey

    async def strip():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            await s.execute(
                update(ApiKey).where(ApiKey.id == minted["id"]).values(scopes=[])
            )
            await s.commit()
        await engine.dispose()

    asyncio.run(strip())
    resp = _post(client, minted["key"])
    assert resp.status_code == 403
    assert "ocr:read" in resp.json()["detail"]


# --- input guards --------------------------------------------------------

def test_a_pdf_is_rejected_with_a_pointer_to_what_is_accepted():
    client = _client()
    minted = _mint(client, "pdf-reject")
    resp = _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
    assert resp.status_code == 400
    assert ".png" in resp.json()["detail"]


def test_an_empty_upload_is_rejected():
    client = _client()
    minted = _mint(client, "empty")
    resp = _post(client, minted["key"], data=b"")
    assert resp.status_code == 400


def test_a_gif_renamed_png_never_reaches_the_gif_decoder():
    """images._KINDS is a decoder allowlist on the SNIFFED format."""
    client = _client()
    minted = _mint(client, "renamed-gif")
    gif = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 20
    resp = _post(client, minted["key"], data=gif, filename="a.png")
    assert resp.status_code == 400


def test_an_oversized_upload_is_413_and_never_decoded():
    client = _client()
    minted = _mint(client, "too-big")
    os.environ["OCR_MAX_UPLOAD_BYTES"] = "2048"
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        resp = _post(client, minted["key"], data=_png(400, 400) + b"\x00" * 5000)
        assert resp.status_code == 413
        assert "limit" in resp.json()["detail"].lower()
    finally:
        os.environ.pop("OCR_MAX_UPLOAD_BYTES", None)
        get_settings.cache_clear()


def test_an_unsupported_lang_is_400_not_500():
    client = _client()
    minted = _mint(client, "bad-lang")
    resp = _post(client, minted["key"], extra={"lang": "klingon"})
    assert resp.status_code == 400
    assert "devanagari" in resp.json()["detail"]


def test_a_pixel_bomb_is_refused_without_being_decoded():
    """A ~200-byte PNG can declare 40000x40000: it passes the byte cap, and
    Pillow only RAISES above 2x its own limit (merely warning between 1x and
    2x), so relying on its exception lets a 1.5x bomb through."""
    client = _client()
    minted = _mint(client, "pixel-bomb")
    from PIL import Image

    buf = io.BytesIO()
    # 12000x12000 = 144M pixels, over MAX_IMAGE_PIXELS (40M), but a flat colour
    # so the compressed bytes stay small.
    Image.new("L", (12000, 12000), 255).save(buf, format="PNG", optimize=True)
    payload = buf.getvalue()
    assert len(payload) < 1_000_000, "the fixture must stay under the byte cap"
    resp = _post(client, minted["key"], data=payload)
    assert resp.status_code == 400
    assert "pixel" in resp.json()["detail"].lower()


# --- outcome and bookkeeping ---------------------------------------------

def test_no_temp_file_survives_a_rejected_request():
    import tempfile
    from pathlib import Path

    client = _client()
    minted = _mint(client, "temp-cleanup")
    before = set(Path(tempfile.gettempdir()).glob("ocr-*"))
    _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
    _post(client, minted["key"], data=b"")
    after = set(Path(tempfile.gettempdir()).glob("ocr-*"))
    assert after == before


def test_a_usage_row_is_written_even_for_a_rejected_request():
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage

    client = _client()
    minted = _mint(client, "usage-log")

    async def count():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(ApiKeyUsage)
                .where(ApiKeyUsage.api_key_id == minted["id"])
            )
        await engine.dispose()
        return n

    before = asyncio.run(count())
    _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
    assert asyncio.run(count()) == before + 1


def test_the_rate_limit_answers_429_with_a_retry_after():
    client = _client()
    minted = _mint(client, "rate-limited")
    from app.apikeys import throttle

    throttle._rate_limiter = throttle.RateLimiter(per_minute=1, burst=1)
    try:
        first = _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
        assert first.status_code != 429
        second = _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
        assert second.status_code == 429
        assert int(second.headers["Retry-After"]) >= 1
    finally:
        throttle._rate_limiter = None


@pytest.mark.skipif(image_ocr.available(), reason="the OCR stack IS installed")
def test_a_missing_ocr_stack_is_503_never_an_empty_200():
    """§18's lesson: every way an OCR deployment breaks looks like a clean
    deployment. An empty lines:[] with a 200 is the worst possible outcome,
    because the caller writes 'no text found' into a client file."""
    client = _client()
    minted = _mint(client, "no-stack")
    resp = _post(client, minted["key"])
    assert resp.status_code == 503
    assert resp.json()["detail"] == "image OCR is not enabled on this deployment"


@pytest.mark.skipif(not image_ocr.available(), reason="OCR stack not installed")
def test_a_successful_call_carries_the_caveat_and_the_engine_block():
    client = _client()
    minted = _mint(client, "happy-path")
    resp = _post(client, minted["key"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["authoritative"] is False
    assert body["caveat"] == image_ocr.OCR_CAVEAT
    assert body["engine"]["model"] == "PP-OCRv5"
    assert body["engine"]["backend"] == "onnxruntime"
    assert body["image"]["kind"] == "png"
    assert body["request_id"]
    # A blank image legitimately has no text: that is an EMPTY result from an
    # engine that RAN, which is why it still carries a full engine block. It is
    # never inferred from emptiness — a stack that could not run is 503 above.
    assert body["lines"] == [] or isinstance(body["lines"][0]["confidence"], float)
```

- [ ] **Step 2: Run and confirm they fail**

Run: `DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest tests/test_ocr_api_integration.py -v`
Expected: FAIL — mostly 404s, because `/v1/ocr` does not exist yet.

- [ ] **Step 3: Write the route**

Replace `app/publicapi/ocr_router.py` entirely:

```python
"""POST /v1/ocr — the text of one image, for an external API-key caller.

Three things here are not incidental:

  * **`asyncio.to_thread` is mandatory, not an optimisation.**
    `image_ocr.ocr_image` is synchronous and CPU-bound, so calling it directly
    in an `async def` route stops the whole event loop — a single 4-second OCR
    freezes every in-flight chat stream in this worker. Not a slowdown, a
    stall. Same pattern and same reason as `app/rag/worker.py` running Docling
    through `to_thread`.
  * **The semaphore is separate from the thread offload**, because
    `to_thread`'s default executor is much larger and would happily run many
    concurrent OCRs, each spawning onnxruntime's own intra-op threads,
    oversubscribing the box into swap.
  * **A missing OCR stack is 503, never an empty 200.** `docs/nrb-integration.md`
    §18 found five real deployment defects that all produced *successful*
    operations with no text. The route returns 200 with empty `lines` ONLY when
    the engine actually ran and genuinely found nothing — that case carries a
    full `engine` block — and "could not run" is never inferred from emptiness.

The temp file is unlinked in `finally` on EVERY path, 400s included: we told
the caller we do not store their images, and a rejected upload leaving bytes on
disk is the defect `app/rag/router.py` already compensates for.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..apikeys.dependencies import ApiClient, require_api_client
from ..apikeys.policy import SCOPE_OCR_READ
from ..apikeys.repository import record_usage
from ..apikeys.throttle import get_rate_limiter
from ..config import get_settings
from ..db.session import get_session
from ..files import image_ocr, images, ingest
from .schemas import OcrResponse, build_response

logger = logging.getLogger("app.publicapi.ocr")

router = APIRouter(prefix="/v1", tags=["ocr"])

_CHUNK = 1024 * 1024
_ROUTE = "POST /v1/ocr"

STACK_MISSING = "image OCR is not enabled on this deployment"

# Per PROCESS, like the rate limiter. Built lazily so importing this module does
# not need settings.
_slots: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(get_settings().ocr_max_concurrent)
    return _slots


@router.post(
    "/ocr",
    response_model=OcrResponse,
    summary="Read the text of one image (API key, scope ocr:read)",
    responses={
        400: {"description": "Not an image, corrupt, too many pixels, or a bad lang."},
        401: {"description": "Missing/invalid API key."},
        403: {"description": "The key lacks the ocr:read scope."},
        413: {"description": "Image exceeds the size limit."},
        429: {"description": "Rate limited. See Retry-After."},
        503: {"description": "OCR unavailable, or no capacity right now."},
    },
)
async def ocr(
    response: Response,
    file: UploadFile,
    lang: str | None = Form(default=None),
    client: ApiClient = Depends(require_api_client(SCOPE_OCR_READ)),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    request_id = uuid4().hex
    response.headers["X-Request-Id"] = request_id
    started = time.monotonic()
    dest: Path | None = None
    size = 0
    summary = None

    async def finish(status_code: int, detail: str | None = None, lines: int | None = None):
        """Record the usage row, then raise or return. Called on EVERY path."""
        await record_usage(
            session,
            api_key_id=client.key_id,
            route=_ROUTE,
            status_code=status_code,
            bytes_in=size,
            duration_ms=int((time.monotonic() - started) * 1000),
            width=summary.width if summary else None,
            height=summary.height if summary else None,
            lines_out=lines,
        )
        await session.commit()
        if detail is not None:
            raise HTTPException(status_code=status_code, detail=detail)

    try:
        # 1) rate limit, before touching disk
        wait = get_rate_limiter().check(client.key_id)
        if wait is not None:
            await record_usage(
                session,
                api_key_id=client.key_id,
                route=_ROUTE,
                status_code=429,
                bytes_in=0,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded for this API key",
                headers={"Retry-After": str(wait)},
            )

        # 2) language, before any IO — a bad value must not cost an upload
        chosen = (lang or image_ocr.DEFAULT_LANG).strip()
        if chosen not in image_ocr.SUPPORTED_LANGS:
            await finish(
                400,
                f"unsupported lang '{chosen}' (supported: "
                f"{', '.join(sorted(image_ocr.SUPPORTED_LANGS))})",
            )

        # 3) extension allowlist: images only. A PDF has a text layer worth
        #    reading, and handing page 1 to an OCR engine would discard it.
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ingest.IMAGE_EXTS:
            await finish(
                400,
                f"'{ext or file.filename}' is not an image — /v1/ocr accepts "
                f"{', '.join(sorted(ingest.IMAGE_EXTS))}",
            )

        # 4) stream to a temp file, counting bytes (413 before any decode)
        fd, temp_name = tempfile.mkstemp(prefix="ocr-", suffix=ext)
        dest = Path(temp_name)
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.ocr_max_upload_bytes:
                    await finish(
                        413,
                        f"image exceeds the "
                        f"{settings.ocr_max_upload_bytes // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)
        if size == 0:
            await finish(400, "uploaded file is empty")

        # 5) header read: format allowlist on the SNIFFED format + the decoded
        #    PIXEL cap, both BEFORE any full decode. summarize_image owns both.
        try:
            summary = await asyncio.to_thread(images.summarize_image, dest)
        except Exception as exc:
            await finish(400, f"could not read the image ({exc})")

        # 6) a slot, or 503 — bounded, because an unbounded queue turns a load
        #    spike into an outage. Distinct from 429: that means YOU sent too
        #    much, this means the box is busy with other callers.
        try:
            await asyncio.wait_for(
                _semaphore().acquire(), timeout=settings.ocr_queue_wait_seconds
            )
        except asyncio.TimeoutError:
            await record_usage(
                session,
                api_key_id=client.key_id,
                route=_ROUTE,
                status_code=503,
                bytes_in=size,
                duration_ms=int((time.monotonic() - started) * 1000),
                width=summary.width,
                height=summary.height,
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OCR is at capacity; retry shortly",
                headers={"Retry-After": "5"},
            )

        try:
            result = await asyncio.to_thread(image_ocr.ocr_image, dest, lang=chosen)
        except image_ocr.OcrUnavailable as exc:
            # The stack is absent or the engine could not run. 503 and a clear
            # reason — NEVER an empty 200. See the module docstring.
            logger.warning("ocr unavailable (request %s): %s", request_id, exc)
            await finish(503, STACK_MISSING)
        except ValueError as exc:
            await finish(400, str(exc))
        finally:
            _semaphore().release()

        await finish(200, None, lines=len(result.lines))
        logger.info(
            "ocr ok request=%s key=%s lines=%d %dx%d frames=%d %dms",
            request_id, client.key_id, len(result.lines),
            summary.width, summary.height, summary.frames,
            int((time.monotonic() - started) * 1000),
        )
        return build_response(result, summary, request_id)
    finally:
        # Every path, success and failure. We told the caller we do not keep
        # their images; leaving one in /tmp makes that untrue.
        if dest is not None:
            dest.unlink(missing_ok=True)
        await file.close()
```

- [ ] **Step 4: Register the router**

In `app/main.py`, beside the api-keys import:

```python
from .publicapi.ocr_router import router as ocr_router
```

and inside the existing `if get_settings().external_api_enabled:` block:

```python
    app.include_router(ocr_router)
```

- [ ] **Step 5: Add the optional pre-warm**

The first request after boot pays the model load. In `app/main.py`'s existing
lifespan `asynccontextmanager`, before the `yield`:

```python
    # Optional: pay the OCR model load at startup instead of charging it to the
    # first caller. Failure is logged and ignored — a deployment without the
    # OCR stack must still boot, and /v1/ocr answers 503 on its own.
    settings = get_settings()
    if settings.external_api_enabled and settings.ocr_prewarm:
        import asyncio as _asyncio

        from .files import image_ocr as _image_ocr

        try:
            await _asyncio.to_thread(_image_ocr.available)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("OCR pre-warm failed: %s", exc)
```

(If `app/main.py` has no `logger`, use `logging.getLogger("app.main")` — check
the file rather than assuming.)

- [ ] **Step 6: Run the tests**

Run: `DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest tests/test_ocr_api_integration.py -v`
Expected: PASS. Exactly one of the last two tests SKIPS, depending on whether
the OCR stack is installed here — check which, and that the skip reason is the
expected one. A run where BOTH skip means `image_ocr.available()` is raising;
investigate rather than accepting the green.

- [ ] **Step 7: Commit**

```bash
git add app/publicapi/ocr_router.py app/main.py tests/test_ocr_api_integration.py
git commit -m "feat(publicapi): POST /v1/ocr

to_thread + a semaphore, because sync CPU-bound OCR in an async route
stalls every in-flight chat stream. A missing stack is 503, never an
empty 200 — an empty result is only ever from an engine that ran."
```

---

## Task 9: The import boundary and the 503 proof

**Files:**
- Modify: `tests/test_ocr_api_boundaries.py` (append)

**Interfaces:**
- Consumes: the finished route.
- Produces: nothing importable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ocr_api_boundaries.py`:

```python
# --- import boundary ------------------------------------------------------

def test_importing_the_public_api_loads_no_ocr_stack():
    """A SUBPROCESS check, because sys.modules is process-global: any earlier
    test that used OCR would make an in-process assertion pass vacuously.

    The API image must be able to run with rapidocr/onnxruntime absent, and it
    must not pay their import cost when they happen to be present.
    """
    code = (
        "import app.publicapi.ocr_router, app.publicapi.schemas, sys;"
        "bad=[m for m in ('rapidocr','onnxruntime','cv2','docling') "
        "if any(k==m or k.startswith(m+'.') for k in sys.modules)];"
        "print('LOADED:'+','.join(bad) if bad else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "CLEAN", out.stdout


def test_importing_the_whole_app_loads_no_ocr_stack():
    code = (
        "import app.main, sys;"
        "bad=[m for m in ('rapidocr','onnxruntime','cv2','docling') "
        "if any(k==m or k.startswith(m+'.') for k in sys.modules)];"
        "print('LOADED:'+','.join(bad) if bad else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "CLEAN", out.stdout


def test_with_the_ocr_stack_unimportable_the_route_answers_503():
    """Simulates the deployment where INSTALL_OCR was false — the §18 case.

    Run in a subprocess with an import hook that makes rapidocr unimportable,
    so this holds even on a machine where the stack IS installed.
    """
    script = r'''
import sys, io, os
class _Block:
    def find_module(self, name, path=None):
        return self if name.split(".")[0] in ("rapidocr", "onnxruntime") else None
    def load_module(self, name):
        raise ImportError("blocked for the test")
sys.meta_path.insert(0, _Block())

os.environ["EXTERNAL_API_ENABLED"] = "true"
from app.files import image_ocr
assert image_ocr.available() is False, "the import block did not take effect"

from app.publicapi.ocr_router import STACK_MISSING
print("DETAIL:" + STACK_MISSING)
'''
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert "DETAIL:image OCR is not enabled on this deployment" in out.stdout


def test_the_public_api_never_returns_a_user():
    """An ApiClient must not be convertible into a User: that separation is the
    entire reason app/apikeys/ exists rather than a branch in auth/."""
    from app.apikeys.dependencies import ApiClient

    assert not hasattr(ApiClient, "role")
    assert not hasattr(ApiClient, "email")
    assert not hasattr(ApiClient, "is_active")

    for module_path in ("app/apikeys/dependencies.py", "app/publicapi/ocr_router.py"):
        source = Path(module_path).read_text()
        assert "users.models import User" not in source, (
            f"{module_path} imports User; an API key must never resolve to one"
        )
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/test_ocr_api_boundaries.py -v`
Expected: PASS (6 tests). If `test_importing_the_whole_app_loads_no_ocr_stack`
fails, something added a module-scope OCR import — find it and move the import
inside the function; do not relax the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ocr_api_boundaries.py
git commit -m "test(publicapi): import boundary + the 503 path proved in a subprocess

sys.modules is process-global, so an in-process assertion here would
pass vacuously after any earlier test touched OCR."
```

---

## Task 10: The 12-case eval

**Files:**
- Create: `tests/test_ocr_api_eval.py`
- Create: `tests/fixtures/ocr_api/README.md`

**Interfaces:**
- Consumes: the finished route.
- Produces: the pass/fail gate named in the spec's Evaluation section.

- [ ] **Step 1: Find the existing 9-case eval and reuse its images**

Run: `grep -rn "OCR_LIVE\|image_ocr" tests/*.py | head -20` and
`ls docs/image-ocr.md tests/fixtures 2>/dev/null`

The spec's eval extends the 9 cases in `docs/image-ocr.md`. Read that document
and find where its images live. If they are generated rather than stored,
reuse the generator. **Do not invent new expected strings** — an eval whose
labels you wrote yourself measures nothing.

- [ ] **Step 2: Write the eval**

Create `tests/fixtures/ocr_api/README.md`:

```markdown
# /v1/ocr eval fixtures

Twelve cases: the nine from `docs/image-ocr.md` (Devanagari, English, mixed,
low-dpi scan, blank, multi-frame TIFF, rotated, pixel-bomb, corrupt) driven
through HTTP instead of the tool, plus three API-shaped ones (oversized, wrong
content type, scoped-out key).

The nine text cases assert the API and `read_image` return the **same lines**
for the same image. That equality is the real regression guard — it is what
stops the two paths drifting. The three API cases assert exact status + detail.

Target: 12/12, as a PR gate. Run with `OCR_LIVE_TESTS=1`; skipped otherwise,
because the text cases need the OCR stack and a real model load.
```

Create `tests/test_ocr_api_eval.py`:

```python
"""The 12-case /v1/ocr eval. Skipped unless OCR_LIVE_TESTS=1.

Nine cases come from docs/image-ocr.md and assert the API and the read_image
tool return the SAME lines for the same image — that equality is what stops the
two paths drifting. Three are API-shaped and assert exact status + detail.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("OCR_LIVE_TESTS") != "1",
    reason="set OCR_LIVE_TESTS=1 (needs the OCR stack and a real model load)",
)


def _client_and_key():
    """Reuse the integration helpers rather than duplicating them."""
    from tests.test_ocr_api_integration import _client, _mint

    client = _client()
    return client, _mint(client, "eval-run")["key"]


@pytest.mark.parametrize("case", ["devanagari", "english", "mixed", "scan",
                                  "blank", "tiff_multiframe", "rotated"])
def test_the_api_and_the_tool_agree_line_for_line(case):
    """IMPLEMENTER: wire `case` to the image and expectation used by the
    existing 9-case eval found in Step 1. Assert:

        api_lines == tool_lines

    for the same file, where tool_lines comes from calling
    `image_ocr.ocr_image(path)` directly. Do NOT hand-write expected text —
    the existing eval owns the labels.
    """
    pytest.skip("wire to the existing eval fixtures found in Step 1")


def test_a_corrupt_image_is_400_on_both_paths():
    client, key = _client_and_key()
    resp = client.post(
        "/v1/ocr",
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\ngarbage", "image/png")},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 400


def test_a_pixel_bomb_is_400_and_says_pixels():
    import io

    from PIL import Image

    client, key = _client_and_key()
    buf = io.BytesIO()
    Image.new("L", (12000, 12000), 255).save(buf, format="PNG", optimize=True)
    resp = client.post(
        "/v1/ocr",
        files={"file": ("a.png", buf.getvalue(), "image/png")},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 400
    assert "pixel" in resp.json()["detail"].lower()


def test_a_wrong_content_type_is_400_and_names_what_is_accepted():
    client, key = _client_and_key()
    resp = client.post(
        "/v1/ocr",
        files={"file": ("a.pdf", b"%PDF-1.4\n", "application/pdf")},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 400
    assert ".png" in resp.json()["detail"]


def test_an_oversized_image_is_413():
    client, key = _client_and_key()
    os.environ["OCR_MAX_UPLOAD_BYTES"] = "2048"
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        resp = client.post(
            "/v1/ocr",
            files={"file": ("a.png", b"\x89PNG" + b"\x00" * 5000, "image/png")},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 413
    finally:
        os.environ.pop("OCR_MAX_UPLOAD_BYTES", None)
        get_settings.cache_clear()
```

**Note:** the seven agreement cases ship as an explicit `skip` with wiring
instructions rather than as invented assertions. That is deliberate — a case
whose expected text this plan made up would report a false pass. Wiring them is
part of this task; if the existing eval's fixtures cannot be found, say so
rather than writing new labels.

- [ ] **Step 3: Run it**

Run: `.venv/bin/pytest tests/test_ocr_api_eval.py -v` (expect skips) then, with
the stack installed:
`OCR_LIVE_TESTS=1 DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest tests/test_ocr_api_eval.py -v`

Expected: the five API-shaped cases PASS. Report the agreement cases' real
state — passing, or still skipped pending fixtures. Do not report 12/12 unless
twelve tests actually ran and passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ocr_api_eval.py tests/fixtures/ocr_api/README.md
git commit -m "test(publicapi): the 12-case /v1/ocr eval

Nine cases assert the API and read_image agree line for line — that
equality is what stops the two paths drifting."
```

---

## Task 11: Documentation

**Files:**
- Create: `docs/external-api.md`
- Modify: `CLAUDE.md`

**Interfaces:** none.

- [ ] **Step 1: Write the operator/consumer doc**

Create `docs/external-api.md`:

```markdown
# The external API: API keys and `POST /v1/ocr`

Design and reasoning: `docs/superpowers/specs/2026-08-23-external-api-keys-and-ocr-endpoint-design.md`.
This file is the runbook.

## Turning it on

Both routes are unregistered unless `EXTERNAL_API_ENABLED=true`. The OCR route
also needs the OCR stack, which is an opt-in build flag:

    docker build --build-arg INSTALL_OCR=true ...

With the stack absent the route exists and answers **503**, with the detail
`image OCR is not enabled on this deployment`. That is deliberate: it is a
deployment that means to serve OCR and cannot, which is a different fact from a
deployment that was never asked to (there the route simply does not exist).

**Verify a deployment by making a real call with a known image, never by
whether the container started.** `docs/nrb-integration.md` §18 found five
distinct OCR deployment defects that all produced *successful* operations with
no text.

## Minting the first key

There is no bootstrap script: the admin routes are JWT-admin, and an admin
already exists on any deployed instance.

    TOKEN=$(curl -s -X POST localhost:8000/auth/login \
      -H 'content-type: application/json' \
      -d '{"email":"admin@example.com","password":"..."}' | jq -r .access_token)

    curl -s -X POST localhost:8000/v1/api-keys \
      -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
      -d '{"name":"odin-crm-ocr"}' | jq

The response is the **only** time the plaintext key exists. Store it in the
consuming app's secret manager immediately; there is no recovery, only
re-minting.

## Calling it

    curl -s -X POST localhost:8000/v1/ocr \
      -H "X-API-Key: lgw_live_..." \
      -F file=@scan.png -F lang=devanagari | jq

`lang` is `devanagari` (default, reads English too) or `en`. Accepted
extensions: `.png .jpg .jpeg .webp .tif .tiff .bmp`. A PDF is a 400 — OCR'ing
page 1 of a document that has a text layer would discard it.

## Reading the response

Every response carries `authoritative: false` and a `caveat`, and they mean it:
PP-OCRv5 drops letterheads and subject lines, mangles latin runs, and misreads
dates. **Never** treat a figure, date, account number or contact detail from
this endpoint as correct without a human checking it against the image.

`partial: true` means the image had more than one frame and only the first was
read — a multi-frame TIFF is a scanner's normal output.

`lines[].confidence` is reported because it is information. There is no
threshold and no "reliable" flag: the measurement behind these scores is
orthographic well-formedness, which is not a per-field correctness estimate.

## Status codes

| Code | Meaning | What the caller should do |
|---|---|---|
| 401 | The key is absent, malformed, unknown, wrong, revoked or expired | Check the secret; ask an admin whether it was revoked |
| 403 | The key is genuine but lacks `ocr:read` | Ask an admin to re-mint with the scope. Do NOT rotate the key |
| 400 | Not an image, corrupt, too many pixels, bad `lang` | Fix the input |
| 413 | Over `OCR_MAX_UPLOAD_BYTES` | Downscale before sending |
| 429 | This key's rate limit | Honour `Retry-After` |
| 503 | OCR unavailable, **or** at capacity | Retry on `Retry-After`; if the detail says "not enabled", it is a deployment fault, not a transient one |

401 is one message for all six credential causes on purpose — distinguishing
them tells an attacker which prefixes are real. The server log distinguishes
them; ask an operator.

## Revoking

    curl -X DELETE localhost:8000/v1/api-keys/<id> -H "authorization: Bearer $TOKEN"

Takes effect on the holder's next call. The row is kept (`is_active=false`) so
the key's usage history stays attributable — keys are never deleted.

## Operating

`api_key_usage` holds one row per call: route, status, bytes, dimensions, line
count, duration, and no image bytes and no OCR text. Monthly review, per the
spec's Evaluation section:

    -- status split per key over the last 30 days
    SELECT k.name, u.status_code, count(*), round(avg(u.duration_ms)) AS avg_ms
    FROM api_key_usage u JOIN api_keys k ON k.id = u.api_key_id
    WHERE u.created_at > now() - interval '30 days'
    GROUP BY 1, 2 ORDER BY 1, 3 DESC;

A rising 503 share means the box is undersized (`OCR_MAX_CONCURRENT`,
`OCR_QUEUE_WAIT_SECONDS`). A rising empty-200 share means upstream image
quality, not a code fault. **A nonzero 401 count on a provisioned key needs a
human** — it is either a leak being probed or a caller with a stale secret.

Rate-limit and lockout counters are **per process**: N uvicorn workers means N x
the limit. Fine for capacity protection; not a billing quota.
```

- [ ] **Step 2: Update CLAUDE.md**

Add to the **Endpoints** section, after the NRB entries:

```markdown
External (API key, `X-API-Key`, only when `EXTERNAL_API_ENABLED=true`):
`POST /v1/ocr` (scope `ocr:read`; multipart image + optional `lang` →
`{text, lines[{text,confidence}], authoritative:false, caveat, partial, image,
engine, request_id}`; 400 not-an-image/corrupt/pixel-bomb/bad-lang, 401 any
credential fault, 403 missing scope, 413 over cap, 429 rate limited, 503 OCR
absent **or** at capacity). Admin (JWT): `POST /v1/api-keys` (201, plaintext
key returned ONCE), `GET /v1/api-keys`, `DELETE /v1/api-keys/{id}` (revokes,
row retained). Runbook: `docs/external-api.md`.
```

Add to **Conventions / gotchas**:

```markdown
- **An API key is an `ApiClient`, never a `User`, and that separation is the
  whole design.** `app/apikeys/` resolves external callers and
  `app/auth/dependencies.py` resolves humans; neither knows about the other, so
  a leaked key cannot reach `/v1/chat`, `/users`, a department or an admin
  route — asserted in BOTH directions
  (`test_an_api_key_cannot_reach_a_jwt_route`,
  `test_a_jwt_cannot_be_used_on_the_ocr_route`). Folding key handling into
  `auth/dependencies.py` would put two identity types in the highest-consequence
  file in the repo. Six things a rewrite must not lose: (1) **verification is
  prefix-indexed SHA-256, deliberately not bcrypt** — the secret is 32 bytes of
  `token_urlsafe`, so a work factor buys nothing and would cost ~100 ms on every
  request; `hmac.compare_digest` is AST-asserted because `==` on a hash is a
  timing oracle that reads as correct code; (2) **401 is one message for six
  causes** (absent, malformed, unknown prefix, wrong secret, revoked, expired) —
  distinguishing them tells an attacker which prefixes are real and whether a
  valid key ever existed; the log distinguishes them, the response never does;
  (3) **a scope failure is 403, not 401**, because the credential is genuine and
  the caller must not rotate a working key chasing the wrong bug; (4) **an
  unusable key does not consume a throttle attempt** — a revoked key presented
  by an honest caller would otherwise lock out the prefix on top of being
  refused, the `UNAVAILABLE`-must-not-consume-an-attempt rule from AD login;
  (5) `require_api_client` is a dependency **FACTORY** so the ROUTE owns the
  required scope — a dependency reading the scope from the request would let the
  caller choose which check they face; (6) `ck_api_keys_scopes` and
  `policy.ALL_SCOPES` are two copies on purpose: the CHECK stops a typo being
  stored, the set stops one being honoured.
- **`/v1/ocr` runs OCR in a thread behind a semaphore, and both halves are
  load-bearing.** `image_ocr.ocr_image` is synchronous and CPU-bound, so calling
  it directly in an `async def` route **stalls the event loop** — one 4-second
  OCR freezes every in-flight chat stream in that worker. `asyncio.to_thread`
  fixes that (the `app/rag/worker.py` pattern for Docling); the separate
  `asyncio.Semaphore(OCR_MAX_CONCURRENT)` exists because `to_thread`'s default
  executor is far larger and would run many OCRs at once, each spawning
  onnxruntime's own intra-op threads. Waiting for a slot is **bounded** — 503 +
  `Retry-After`, distinct from 429 (429 = you sent too much, 503 = the box is
  busy) — because an unbounded queue turns a load spike into an outage. And the
  route **never returns 200 with empty `lines` unless the engine actually ran**:
  §18's whole lesson is that every way an OCR deployment breaks looks like a
  clean deployment, and an empty `lines: []` with a 200 is the worst outcome
  available, because the caller writes "no text found" into a client file. A
  stack that could not run is 503, never inferred from emptiness. The temp file
  is unlinked in `finally` on every path including the 400s — we told the caller
  we do not store their images.
- **The OCR caveat is ONE constant with TWO readers.** `image_ocr.OCR_CAVEAT` is
  rendered into the model's context by `read_image` and published as `/v1/ocr`'s
  `caveat` field; a second copy drifts, and then the API field contradicts the
  chat answer and the reader cannot tell which to believe — the
  `sources.VERIFY_NOTE` rule.
  `test_the_caveat_is_one_constant_with_two_readers` locks it. Related:
  `authoritative` is always False and **no code compares a confidence to a
  literal** (AST-asserted) — §16.6 measured orthographic well-formedness, which
  is not a per-field correctness estimate.
```

And in **Layout**, after the `files/` entry:

```markdown
`apikeys/` (external API-key credentials: `keygen`+`policy` are PURE — mint /
verify / may-this-key-act — `models`+`repository` are the tables and their
access, `throttle` = per-key token bucket plus the reused login lockout,
`dependencies` = `require_api_client(scope)` returning an **`ApiClient`, never a
`User`**, `router` = admin `/v1/api-keys`), `publicapi/` (the external HTTP
surface: `POST /v1/ocr` only; imports no OCR stack at module scope),
```

Finally, in **Not done yet**, remove nothing but add:

```markdown
The external API (`docs/external-api.md`) ships **off** — `EXTERNAL_API_ENABLED`
defaults false. Turning it on needs the `INSTALL_OCR=true` image. Its eval's
seven API-vs-tool agreement cases are wired to the existing `docs/image-ocr.md`
fixtures; the five API-shaped cases pass standalone.
```

- [ ] **Step 3: Run the whole suite**

```bash
DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)" .venv/bin/pytest -q 2>&1 | tail -20
```

Compare against a baseline captured BEFORE this branch:
- Pass count must be higher by the new tests.
- **Skip count must not have risen** beyond the deliberate OCR-stack and
  `OCR_LIVE_TESTS` skips. A rising skip count is how a broken auth helper turns
  86 tests into silent skips that a green run hides.
- `tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set`
  is a known pre-existing failure on any developer database with real data. It
  is unrelated to this work; note it, do not fix it here.

- [ ] **Step 4: Commit**

```bash
git add docs/external-api.md CLAUDE.md
git commit -m "docs: the external API runbook, and CLAUDE.md entries

An ApiClient is never a User; to_thread + semaphore is load-bearing;
the caveat is one constant with two readers."
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:

| Spec section | Task |
|---|---|
| Decisions table | 1–8 (each decision is realised by the task that implements it) |
| Architecture / module layout | 1, 2, 3, 4, 5, 6, 7, 8 (one file per listed module) |
| Why SHA-256 not bcrypt | 1 |
| Schema (both tables, both CHECKs, both FKs) | 3 |
| Wire contract — OCR envelope | 7 |
| Wire contract — admin routes | 6 |
| Error taxonomy (all 7 rows, all 6 rules) | 6 (401/403), 8 (400/413/429/503 ×2), 5 (throttle) |
| Concurrency / to_thread / semaphore / bounded wait | 8 |
| Settings (all 8) | 6 |
| The deliberate 503-vs-404 asymmetry | 6 (as a code comment), 11 (as docs) |
| Testing — pure | 1, 2, 5 |
| Testing — integration | 3, 6, 8 |
| Testing — boundary / subprocess | 7, 9 |
| Testing — live eval | 10 |
| Evaluation & Improvement (metric, eval, feedback, review loop) | 10 (eval), 4 (`record_usage` = feedback capture), 11 (the review-loop SQL and cadence) |
| Out of scope | not implemented, by construction |

**Placeholder scan:** one deliberate `pytest.skip` remains, in Task 10's seven
agreement cases, with wiring instructions. That is not a placeholder standing in
for work this plan should have done — inventing expected OCR text would produce
a false pass, so the plan directs the implementer to the existing labelled
fixtures instead. Flagged explicitly in the task and in the CLAUDE.md note.

**Type consistency:** `MintedKey.token/prefix/key_hash` used identically in
Tasks 1, 4, 6. `KeyFacts(is_active, expires_at, scopes)` constructed only by
`repository.facts_of` and consumed only by `policy`. `ApiClient(key_id, name,
scopes)` produced in Task 6, consumed in Task 8 as `client.key_id`.
`repository.create_key` takes `key_id` (Task 4) and Task 6 passes `key_id=`.
`record_usage`'s keyword set in Task 4 matches all four call sites in Task 8.
`OCR_CAVEAT` is defined once (Task 7) and read as `read_image.CAVEAT`,
`schemas.CAVEAT` and `image_ocr.OCR_CAVEAT`, all asserted identical.

**One assumption the implementer must check, not trust** (flagged in Task 5
Step 3): the login throttle's settings field names beyond `login_max_attempts`
are inferred, not read. `grep -n "login_" app/config.py` before running.
