"""NRB operations API — admin only, and deliberately three endpoints.

    POST /v1/nrb/runs          trigger an update
    GET  /v1/nrb/runs/{id}     one run (reconciling it if it is still waiting)
    GET  /v1/nrb/status        operational state for the future admin UI

THIN MEANS THIN
    Every handler below parses a request, calls ONE application service and
    shapes the answer. There is no orchestration here: `pipeline.request_run` owns
    admission and the active-run gate, `pipeline.execute_run` (called by
    `app/nrb/runner.py`, never by this router) owns the sequence, the advisory
    lock and the status arithmetic, and `pipeline.reconcile` owns the terminal
    verdict. Nothing shells out to a script — the scripts are adapters over the
    same services, so the CLI and this router are callers of one implementation.

NOTHING IS EXECUTED IN THE REQUEST
    `POST` durably ACCEPTS the request — one INSERT of a `queued` row — and
    returns 202 with the run identity. The four staging stages run in
    `app/nrb/runner.py`, a separate process, and recovery/chunking/embedding/
    supersession remain `app.rag.worker`'s as they always were. So no handler
    here waits on NRB's website, downloads anything, parses anything or embeds
    anything, and the gateway dying after a 202 loses nothing: the accepted run
    is a committed row.

    `RunTriggerIn` still requires a bound and still does not expose `all_files`.
    That guard was never really about request duration — a full-corpus pass is
    18,266 files and the `RAG_DOCS_DIR` duplication decision is still open
    (§20.7 item 2), so it stays a considered decision at a terminal.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..users.models import User
from . import catalog, corpus, pipeline
from .schemas import NRBStatusOut, RunOut, RunTriggerIn, RunTriggerOut

logger = logging.getLogger("app.nrb.router")

router = APIRouter(prefix="/v1/nrb", tags=["nrb"])


def _run_out(run: pipeline.RunView) -> RunOut:
    return RunOut.model_validate(run.as_dict())


@router.post(
    "/runs",
    response_model=RunTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": RunTriggerOut,
            "description": "An NRB update is already in progress; the body "
                           "carries that run with `started: false`.",
        }
    },
)
async def trigger_run(
    payload: RunTriggerIn,
    admin: User = Depends(require_admin),
) -> JSONResponse:
    """Accept an NRB update. 202 with the queued run, or 409 with the active one.

    Returns as soon as the request is DURABLE. `pipeline.request_run` inserts a
    `queued` row and nothing else; `app/nrb/runner.py` claims it and executes the
    stages. So this handler performs no sync, no fetch, no extract and no
    enqueue, and an accepted run survives an API restart because it is a
    committed row rather than a task in this process's memory.

    `trigger` is recorded as `api` and `requested_by` as the admin's email, so
    `nrb_pipeline_runs` says who asked — the reason those columns exist.

    **409, never 500, for "already running", and one body shape for all of it.**
    An update is in progress if any run is `queued`, `running` or
    `awaiting_jobs`, and there is one further case: another runner holding the
    advisory lock in the instant before its own row is visible. All of them raise
    `PipelineBusy`; the first three carry the run, the last carries None and gets
    `detail` instead. A client reads `started`, then `run` if present — never a
    second schema.

    202 rather than 201: nothing was created at the posted URL, and the work is
    accepted rather than done.
    """
    scope = pipeline.PipelineScope(
        department=payload.department,
        stages=tuple(payload.stages),
        keys=tuple(payload.keys),
        sections=tuple(payload.sections),
        owners=tuple(payload.owners),
        years=tuple(payload.years),
        resource_types=tuple(payload.resource_types),
        extensions=tuple(payload.extensions),
        limit=payload.limit,
        retry_failed=payload.retry_failed,
        # Never settable over HTTP. `RunTriggerIn` has already refused an
        # unbounded request; this is the second half of the same rule.
        all_files=False,
    )
    try:
        # ACCEPT only. One INSERT of a `queued` row; `app/nrb/runner.py` stages
        # it. Never `pipeline.start`, which is the synchronous composition the
        # CLI's `--run-now` uses — calling it here would put sync, fetch and
        # extract back inside the request, which is the thing this step removed.
        run = await pipeline.request_run(
            scope, trigger="api", requested_by=admin.email
        )
    except pipeline.PipelineBusy as busy:
        # ONE body for every kind of busy, including the rare case where the
        # advisory lock is held but no durable row names the holder yet: `run` is
        # simply null and `detail` explains. A client branches on `started`, not
        # on which mechanism fired.
        logger.info(
            "NRB api: trigger refused (%s)",
            f"run {busy.run.id} is {busy.run.status}" if busy.run
            else "the pipeline lock is held",
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=RunTriggerOut(
                started=False,
                run=_run_out(busy.run) if busy.run else None,
                detail=str(busy),
            ).model_dump(mode="json"),
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=RunTriggerOut(started=True, run=_run_out(run)).model_dump(
            mode="json"
        ),
    )


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    """One run. Reconciles it through the pipeline service if it is still waiting.

    `pipeline.reconcile` is called rather than reimplemented, and it is the right
    call for both cases: a run in `awaiting_jobs` is advanced to its terminal
    status if its jobs have all finished, and a run that is ALREADY terminal is
    returned untouched — frozen job counts, `finished_at` never rewritten (§24).
    So polling is safe and idempotent, and reading a finished run cannot change
    what it says however much later work has happened to the same documents.
    """
    run = await pipeline.reconcile(session, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown pipeline run"
        )
    await session.commit()
    return _run_out(run)


@router.get("/status", response_model=NRBStatusOut)
async def nrb_status(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    department: str | None = None,
) -> NRBStatusOut:
    """Operational state, composed from existing helpers. Read-only.

    Four blocks, none of them a new source of truth: the active and latest runs
    from `nrb_pipeline_runs`, the catalog and file-state counts from
    `catalog.catalog_counts` / `catalog.fetch_counts` (the same numbers
    `nrb_sync.py` and `nrb_fetch.py` print), and NRB's RAG readiness from
    `corpus.nrb_rag_counts`.

    `active_run` is the field a UI needs most: non-null means a trigger would be
    refused, and it is the same run a 409 would return. Waiting runs are settled
    first, so an update whose jobs have finished but which nobody polled does not
    show as active forever — the same `settle_waiting` the trigger path uses,
    for the same reason.

    `department` narrows only the `rag` block. The catalog is global; there is no
    per-department view of it to give.
    """
    await pipeline.settle_waiting(session)
    await session.commit()

    active = await pipeline.active_run(session)
    latest = await pipeline.latest_run(session)
    return NRBStatusOut(
        active_run=_run_out(active) if active else None,
        latest_run=_run_out(latest) if latest else None,
        catalog=await catalog.catalog_counts(session),
        files=await catalog.fetch_counts(session),
        rag=await corpus.nrb_rag_counts(session, department_code=department),
    )
