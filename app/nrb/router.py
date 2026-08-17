"""NRB operations API — admin only, and deliberately three endpoints.

    POST /v1/nrb/runs          trigger an update
    GET  /v1/nrb/runs/{id}     one run (reconciling it if it is still waiting)
    GET  /v1/nrb/status        operational state for the future admin UI

THIN MEANS THIN
    Every handler below parses a request, calls ONE application service and
    shapes the answer. There is no orchestration here: `pipeline.start` owns the
    sequence, the advisory lock, the durable run row, the active-run gate and the
    status arithmetic, and `pipeline.reconcile` owns the terminal verdict. Nothing
    shells out to a script — the scripts are adapters over the same services, so
    the CLI and this router are two callers of one implementation.

STAGING IS SYNCHRONOUS IN THE REQUEST, AND THAT IS WHY THE SCOPE MUST BE BOUNDED
    `POST` returns the run as it stands when staging ends, which is what makes
    `PipelineBusy` answerable in the response at all. So the request lasts as long
    as the stages it asked for: a `rag`-only pass over a named cohort is
    sub-second, a `sync` is minutes because it reads ~190 pages of a central
    bank's REST API. `RunTriggerIn` therefore requires a bound and does not
    expose `all_files`; a full-corpus trigger stays a considered decision at a
    terminal (§20.7 item 2 is still open). Moving staging off-request belongs with
    the scheduler step, which is not built.

    Note what is NOT synchronous: recovery, chunking and embedding. Those are the
    separate worker's, as they have always been — the run comes back
    `awaiting_jobs` and the client polls. The API still never parses or embeds.
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
    """Run the NRB pipeline once. 202 with the run, or 409 with the active one.

    `trigger` is recorded as `api` and `requested_by` as the admin's email, so
    `nrb_pipeline_runs` says who asked — the reason those columns exist.

    **409, never 500, for "already running".** The two ways that happens are one
    thing to a client: another orchestrator holding the advisory lock, and a
    durable run still `running` or `awaiting_jobs` (the lock is released when
    staging returns while its jobs outlive it — §24.3). `pipeline.start` raises
    `PipelineBusy` for both and carries the run, so both produce the identical
    body with `started: false`. A caller retries later or polls that run; it
    never needs to know which of the two fired.

    202 rather than 201: what came back is an accepted unit of work, usually
    still `awaiting_jobs`, and no resource was created at the posted URL.
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
        run = await pipeline.start(
            scope, trigger="api", requested_by=admin.email
        )
    except pipeline.PipelineBusy as busy:
        if busy.run is None:
            # The lock was held but no durable row names the holder — a run
            # opening at this exact instant. Same meaning to the caller, so the
            # same status; there is simply no run to hand back yet.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An NRB update is already in progress",
            ) from None
        logger.info("NRB api: trigger refused, run %s is active", busy.run.id)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=RunTriggerOut(
                started=False, run=_run_out(busy.run)
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
