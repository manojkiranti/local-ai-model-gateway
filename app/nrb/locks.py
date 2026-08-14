"""One implementation of the "only one of these may run" rule, for NRB commands.

Both long-running NRB commands — the catalog sync and the file fetch — must not
run twice at once, for different reasons (the sync would interleave counters and
race on rows; the fetch would double the load on a central bank's website and race
on the same `nrb_files` rows). Both use the same mechanism, so it lives here once.

**The subtlety this module exists to encapsulate:** a Postgres advisory lock taken
with `pg_try_advisory_lock` is held by the *session*, i.e. the connection. An
`AsyncSession` hands its connection back to the pool at every `commit()`, so a lock
taken on the working session would be silently released at the first phase
boundary — and left stranded on a pooled connection that some later, unrelated
request happens to check out. So the lock lives on a connection of its very own,
opened for exactly as long as the command runs.

Why an advisory lock rather than a lock row: it dies with the connection. A killed
command leaves nothing to clean up, whereas a lock row would need a stale-lock
sweep, which is a second mechanism with its own bugs. The `nrb_sync_runs` /
`nrb_fetch_runs` tables are *records*, never mutexes; a crashed run's row stays
`running` forever and blocks nothing.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("app.nrb.locks")

__all__ = ["FETCH_LOCK_KEY", "SYNC_LOCK_KEY", "LockBusy", "advisory_lock"]

# ASCII, so the key is recognisable in `pg_locks` instead of being a magic number.
# Both fit a signed bigint.
SYNC_LOCK_KEY = int.from_bytes(b"NRB_SYNC", "big")
FETCH_LOCK_KEY = int.from_bytes(b"NRB_FTCH", "big")


class LockBusy(Exception):
    """Someone else holds this lock. Refuse rather than wait or race."""


@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, key: int, *, what: str) -> AsyncIterator[None]:
    """Hold `key` for the body, or raise `LockBusy` immediately.

    `pg_try_advisory_lock` rather than `pg_advisory_lock`: waiting would turn a
    second invocation into a process that looks hung and then does the work twice
    in sequence, which is worse than a readable refusal.

    The `commit()` after acquiring ends the *transaction* while keeping the
    connection — the lock is session-scoped, so it survives — because leaving a
    connection idle-in-transaction for the length of a multi-minute command holds
    back vacuum for no reason.
    """
    async with engine.connect() as connection:
        acquired = bool(
            (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                )
            ).scalar_one()
        )
        await connection.commit()
        if not acquired:
            raise LockBusy(f"another {what} is already running (advisory lock held)")
        logger.info("NRB: acquired the %s lock", what)
        try:
            yield
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
            await connection.commit()
            logger.info("NRB: released the %s lock", what)
