"""The `X-API-Key` boundary. The ONLY place the pieces are combined.

`require_api_client` returns an `ApiClient` and never a `User`. That is the
whole reason this package exists: `app/auth/dependencies.py` resolves humans
and never sees a key, so no route written for a JWT user can be reached with
one, and no key can inherit admin or a department grant.

It is a dependency FACTORY (`require_api_client("ocr:read")`) rather than a
plain dependency, because the required scope belongs to the ROUTE. A single
dependency reading a scope out of the request would let the caller choose which
scope they are checked against.

**Which outcomes get a usage row.** `api_key_usage` needs a real `api_keys.id`
to attach to, so exactly two causes are unattributable and write nothing: the
header is absent/malformed (never parsed into a prefix), and the prefix
matches no stored key at all. Of the SIX 401 credential causes, three have a
genuine key id and get a row: wrong secret, revoked, expired. A missing-scope
403 is a separate outcome (not one of the six) and also gets a row, since the
credential there is genuine. **The credential-lockout 429 gets exactly ONE
row per lockout episode, not one per request** (R2, 2026-08-23): before that
fix, every request that arrived already locked did its own `find_by_prefix` +
`record_usage` + `commit`, which turned a leaked prefix with no secret at all
into a write amplifier for the whole `API_KEY_LOCKOUT_SECONDS` window, at
zero authentication cost. The one row is written by `_record_failure`, on the
specific request whose own `record_failure` call is what trips the lock —
every later request while still locked is answered by the pre-check alone,
with no DB access at all. Every attributed row is `bytes_in=0`, with a
duration local to this dependency (the route's own usage rows measure the OCR
call itself and are separate). `docs/external-api.md` documents this split
explicitly rather than claiming "every path" or "four of six", both of which
were wrong at different times.

**Why an unusable key (revoked/expired) now also consumes a throttle
attempt.** The original exemption copied AD login's rule that an unreachable
directory must not cost an attempt — but that rule protects an HONEST caller
from a TRANSIENT fault: the directory recovers, the account is fine, and
consuming the attempt would only add insult to an outage nobody caused them.
Revocation and expiry are the opposite: they are PERMANENT for a given prefix
(no route un-revokes one, and re-minting hands out a fresh random prefix), so
the exemption never spared an honest caller anything — a key that is dead
stays dead however many times it is presented. What it did do is give an
attacker unlimited free probes AND a way to distinguish "wrong secret" (which
locks out at the configured threshold) from "right secret, dead key" (which
never does) — exactly the six-causes-one-message boundary this module exists
to hide. So the not-usable branch throttles like every other rejection now.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from . import keygen, policy, repository
from .throttle import get_auth_throttle

logger = logging.getLogger("app.apikeys.auth")

# `auto_error=False` is load-bearing: it keeps the absent-header 401 OURS
# (same body, same status) rather than FastAPI's own generic 403 from the
# security dependency. Using `APIKeyHeader` instead of a bare `Header(...)`
# is what makes the OpenAPI schema honest — Swagger shows a lock icon and a
# generated client marks the header required, instead of rendering it as an
# ordinary optional parameter with no security scheme at all.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


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
        request: Request,
        x_api_key: str | None = Depends(_api_key_header),
        session: AsyncSession = Depends(get_session),
    ) -> ApiClient:
        started = time.monotonic()
        route = f"{request.method} {request.url.path}"

        async def _log(key_id: str, status_code: int) -> None:
            """Attribute a rejection to the real key that caused it. Only
            called when a genuine `api_keys.id` is in hand.

            R1: a usage row is EVIDENCE, not a precondition for refusing a
            credential. Before this, anything raised by the INSERT or the
            COMMIT itself — disk full, a statement timeout, a role with
            SELECT but not INSERT — escaped the dependency and turned a
            clean 401/403/429 into a 500, for every ATTRIBUTABLE cause only
            (the two unattributable ones never reach this function, so they
            kept answering 401 — a write-only fault made a refusal's status
            code depend on which cause it was, which is its own bug).
            `except Exception`, deliberately not `BaseException`:
            `asyncio.CancelledError` must keep propagating, since a
            cancelled request is not a fault in this write.
            """
            try:
                await repository.record_usage(
                    session,
                    api_key_id=key_id,
                    route=route,
                    status_code=status_code,
                    bytes_in=0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                await session.commit()
            except Exception:
                logger.warning(
                    "failed to record api key usage (key=%s status=%s)",
                    key_id, status_code, exc_info=True,
                )
                # Every caller of `_log` raises the credential's own
                # HTTPException right after this and never touches `session`
                # again in this request (only the success path at the end of
                # `dependency` still needs it), so nothing downstream depends
                # on the transaction staying open. But a failed commit can
                # leave the connection in an aborted-transaction state, and
                # `get_session`'s `async with SessionLocal()` closes rather
                # than rolls back — so roll back explicitly rather than
                # trusting close() to leave the connection clean for the pool.
                try:
                    await session.rollback()
                except Exception:
                    logger.warning(
                        "failed to roll back session after a usage-log fault",
                        exc_info=True,
                    )

        async def _record_failure(prefix: str, key_id: str) -> None:
            """Count the failure and, if THIS call is the one that trips the
            prefix's lockout, write the ONE usage row a whole lockout episode
            is allowed to cost (R2).

            `retry_after` is read immediately before and immediately after
            `record_failure` — not reused from the pre-check earlier in this
            request — because a concurrent request can trip the lock in
            between; there is no `await` between the two reads below, so
            nothing can interleave with THIS pair, and that is what makes
            "was it me" answerable at all without adding state to
            `LoginThrottle`. A request that arrives already locked never
            reaches this function — it is answered entirely by the pre-check
            below, with no DB access.
            """
            was_locked = throttle.retry_after(prefix) is not None
            throttle.record_failure(prefix)
            if not was_locked and throttle.retry_after(prefix) is not None:
                await _log(key_id, status.HTTP_429_TOO_MANY_REQUESTS)

        parsed = keygen.parse(x_api_key or "")
        if parsed is None:
            # No prefix to throttle on and no key id to attribute a row to.
            logger.info("api key rejected: malformed or absent header")
            raise _invalid()
        prefix, _secret = parsed

        throttle = get_auth_throttle()
        retry_after = throttle.retry_after(prefix)
        if retry_after is not None:
            # R2: zero DB, unconditionally. Before this fix, EVERY request
            # that arrived while a prefix was locked did a find_by_prefix +
            # record_usage + commit — so a leaked prefix with no secret at
            # all turned the cheapest rejection path into the most
            # expensive one, at whatever rate the network allowed, for the
            # whole `API_KEY_LOCKOUT_SECONDS` window. The one attributable
            # row for this lockout episode was already written by
            # `_record_failure`, on the specific request whose own failure
            # tripped it — this branch only ever runs on LATER requests, so
            # there is nothing left to look up or log.
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
            await _record_failure(prefix, key.id)
            logger.warning(
                "api key rejected: secret mismatch for key %s (%s)", key.id, key.name
            )
            await _log(key.id, status.HTTP_401_UNAUTHORIZED)
            raise _invalid()
        if not policy.is_usable(
            repository.facts_of(key), now=datetime.now(timezone.utc)
        ):
            # A revoked/expired key IS a throttled attempt now — see the
            # module docstring for why the AD-UNAVAILABLE analogy this used
            # to follow does not transfer: revocation and expiry are
            # PERMANENT for this prefix, so no honest caller is ever spared,
            # while an attacker got unlimited probes and a 429-vs-401 signal
            # that the secret is genuine.
            await _record_failure(prefix, key.id)
            logger.info(
                "api key rejected: not usable (key=%s active=%s expires=%s)",
                key.id, key.is_active, key.expires_at,
            )
            await _log(key.id, status.HTTP_401_UNAUTHORIZED)
            raise _invalid()

        refusal = policy.scope_refusal(repository.facts_of(key), required=scope)
        if refusal is not None:
            # 403, not 401: the credential is GENUINE. Telling the caller their
            # key is fine and their permissions are not stops them rotating a
            # working key chasing the wrong bug.
            logger.info("api key %s lacks scope %s", key.id, scope)
            await _log(key.id, status.HTTP_403_FORBIDDEN)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

        throttle.reset(prefix)
        await repository.touch_last_used(session, key.id)
        await session.commit()
        return ApiClient(key_id=key.id, name=key.name, scopes=tuple(key.scopes or ()))

    return dependency
