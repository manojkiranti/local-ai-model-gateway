"""The NRB pipeline runner: a separate process, deliberately.

Run it with:

    .venv/bin/python -m app.nrb.runner

WHY IT EXISTS
    `POST /v1/nrb/runs` used to execute the whole orchestration inside the HTTP
    request, so a run that included `sync` held a request open for minutes while
    it read ~190 pages of a central bank's REST API. Now the endpoint durably
    ACCEPTS the request (`pipeline.request_run` → a `queued` row) and returns 202,
    and this process is what picks it up.

    That also fixes something worse than latency: an accepted run is now a
    committed row, so the gateway dying between "202" and "done" loses nothing.

WHAT IT DOES AND DOES NOT DO
    It runs the four staging stages — sync, fetch, extract, RAG select/enqueue —
    and stops. Recovery, the versioned recovery cache, chunking, embedding and
    supersession remain `app.rag.worker`'s, exactly as before: two processes with
    two jobs, and the split is the reason the API image needs neither Docling nor
    an OCR stack.

    So this runner needs **no parsing or OCR dependencies of its own**. It runs on
    the API image: `sync` and `fetch` are httpx, `extract` is pypdf/openpyxl/
    python-docx (all in `requirements.txt`, and `extraction` imports Docling
    lazily or not at all), and the RAG stage copies bytes and inserts rows.

THE LOOP IS DELIBERATELY DULL, LIKE `app.rag.worker`'s
    Poll for the oldest `queued` run, hand its id to `pipeline.execute_run`,
    repeat. Every safety property is the service's, not this file's:

      * `execute_run` holds `PIPELINE_LOCK_KEY` for the whole orchestration, so
        two runners can never orchestrate at once — and because an advisory lock
        dies with its connection, a killed runner leaves nothing to clean up.
      * `recover_abandoned` runs FIRST, whether or not there is work: a run left
        `running` by a dead runner occupies the only active slot
        (`ux_nrb_pipeline_runs_one_active`), so nothing new could be accepted and
        no queued run would ever appear to trigger a sweep. Calling it
        unconditionally is what stops one crash wedging the pipeline for good.
      * The claim itself is `SELECT … FOR UPDATE SKIP LOCKED` on the one row, so
        two runners pass over each other rather than both running it.

    This file therefore contains no locking, no transitions and no stage logic —
    if it grows any, the CLI and the API have stopped sharing an implementation
    with it.

    There is no `preflight` here. `app.rag.worker` has one because a wrong
    embedding width would corrupt half a corpus before anyone noticed; staging
    has no such cliff, and every stage already records its own failure.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ..config import Settings, get_settings
from . import pipeline
from .locks import LockBusy

log = logging.getLogger("nrb.runner")

# How long to wait before asking again when the queue is empty. Runs arrive by
# human or (later) schedule, not in bursts, so this is deliberately lazy: the
# poll costs one indexed SELECT and nothing is waiting on sub-second pickup.
POLL_SECONDS = 5.0


async def run_once(engine: AsyncEngine, settings: Settings) -> bool:
    """Claim and execute one queued run. True if there was one.

    `PipelineBusy` here means another runner holds the lock, which is ordinary in
    a two-replica deployment and not an error: the other runner will take the
    queue. Returning True asks the loop not to sleep, so this one comes back
    promptly once the lock frees.
    """
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # First, unwedge. A run left `running` by a killed runner occupies the only
    # active slot (`ux_nrb_pipeline_runs_one_active`), so nothing new could be
    # accepted and no queued run would ever appear to sweep it. This has to
    # happen whether or not there is work — see `pipeline.recover_abandoned`.
    await pipeline.recover_abandoned(engine=engine, session_factory=Session)

    run_id = await pipeline.claim_next(Session)
    if run_id is None:
        return False

    try:
        run = await pipeline.execute_run(
            run_id, engine=engine, session_factory=Session
        )
    except (pipeline.PipelineBusy, LockBusy) as busy:
        log.info("run %s left queued: another runner is orchestrating (%s)",
                 run_id, busy)
        return True

    log.info(
        "run %s -> %s (stage %s)%s",
        run.id, run.status, run.stage,
        f", jobs {run.jobs}" if run.jobs else "",
    )
    return True


async def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    try:
        log.info("nrb pipeline runner started; polling every %.1fs", POLL_SECONDS)
        while not stopping.is_set():
            try:
                did_work = await run_once(engine, settings)
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("runner iteration failed; continuing")
                did_work = False
            if not did_work:
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
    finally:
        await engine.dispose()
        log.info("nrb pipeline runner stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
