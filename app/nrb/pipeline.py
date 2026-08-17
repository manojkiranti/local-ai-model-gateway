"""ONE way to bring the NRB corpus up to date, whoever asks for it.

The CLI, a future admin endpoint and a future schedule must not be three
implementations of the same sequence — they must be three callers of `start`.
Nothing here shells out to a script, and no stage is reimplemented: every stage
is the application service that already existed and is already tested.

    start(scope)
      ├─ advisory lock (locks.PIPELINE_LOCK_KEY) — or return the run in progress
      ├─ sweep any run left `running` by a dead orchestrator
      ├─ reconcile every `awaiting_jobs` run, then refuse if one is STILL waiting
      ├─ open an nrb_pipeline_runs row
      ├─ sync     -> sync.run_sync          (nrb_sync_runs is its detailed record)
      ├─ fetch    -> fetch.run_fetch        (nrb_fetch_runs)
      ├─ extract  -> extract.run_extract    (nrb_extractions)
      ├─ rag      -> corpus.run_rag_enqueue (documents + ingest_jobs)
      │              ...and record WHICH jobs, in nrb_pipeline_run_jobs
      └─ release the lock; status = awaiting_jobs

    reconcile(run_id)
      recompute the terminal status from this run's OWN jobs. Callable by
      anyone, any time, from any process — including after the orchestrator has
      exited.

ENQUEUEING IS NOT FINISHING
    The RAG worker is a separate process by design (Docling and the CUDA stack
    must never enter the API image). So a run that has staged 400 documents is
    not finished; it is `awaiting_jobs`, and only `reconcile` can move it on.
    Collapsing the two would make a future UI report "update complete" while the
    corpus was a quarter indexed — which is precisely the class of quiet failure
    §18 is about.

    The relation is EXPLICIT (`nrb_pipeline_run_jobs`), never reconstructed from
    timestamps. The scratch database alone holds 190 unrelated `ri*` documents
    with stale jobs (§20.7 item 4); a run that counted jobs created "around the
    same time" would adopt them and report someone else's failure as its own.

EXCLUSION, AND WHY A CRASH CANNOT WEDGE IT
    A Postgres advisory lock, the mechanism `locks.py` already uses for sync,
    fetch and extract, for the reason given there: it dies with the connection,
    so a killed orchestrator leaves nothing to clean up. A second trigger does
    not queue and does not wait — it gets the run already in progress, which is
    the answer an API wants to return.

    The run ROW is a record, not a mutex. A crashed orchestrator leaves one
    stuck in `running`, so the next run sweeps it: holding the lock is proof
    that no orchestrator is alive, which makes the sweep sound with no timeout
    to tune. `heartbeat_at` is advanced at each stage boundary for observability
    — it is not the liveness test, and nothing depends on it.

    **`awaiting_jobs` still blocks a new run, and the lock cannot express that.**
    The lock is released the moment orchestration returns, while the jobs it
    queued outlive it by design — so exclusion is the DURABLE row, checked while
    holding the lock (which is what makes check-then-insert atomic against a
    concurrent starter). A second trigger gets `PipelineBusy` naming the waiting
    run. Every `awaiting_jobs` run is reconciled first (`settle_waiting`), so one
    whose jobs have all finished stops counting as active rather than wedging the
    pipeline until somebody polls it.

WHAT THIS DELIBERATELY DOES NOT DO
    It does not recover, chunk or embed: that is the worker's, through the
    versioned recovery cache. It does not archive anything: supersession happens
    in the worker's activation transaction (§22), and an orchestrator that
    archived would be able to retire a version before its replacement had
    succeeded. It does not purge or refresh the recovery cache — unresolved
    recovery outcomes are cached deliberately, and re-running OCR on them is a
    DIFFERENT, explicitly-requested operation
    (`scripts/nrb_recovery_cache.py --purge`), never a side effect of a routine
    update. And it does not drain jobs, because that races the deployed worker.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import String, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..rag.models import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    IngestJob,
)
from .locks import PIPELINE_LOCK_KEY, LockBusy, advisory_lock
from .models import (
    PIPELINE_ACTIVE_STATUSES,
    PIPELINE_AWAITING,
    PIPELINE_FAILED,
    PIPELINE_PARTIAL,
    PIPELINE_QUEUED,
    PIPELINE_RUNNING,
    PIPELINE_SUCCEEDED,
    NRBPipelineRun,
    NRBPipelineRunJob,
)

logger = logging.getLogger("app.nrb.pipeline")

__all__ = [
    "PIPELINE_AWAITING",
    "PIPELINE_FAILED",
    "STAGES",
    "PipelineBusy",
    "PipelineScope",
    "RunView",
    "PIPELINE_QUEUED",
    "active_run",
    "claim_next",
    "execute_run",
    "get_run",
    "latest_run",
    "recover_abandoned",
    "reconcile",
    "request_run",
    "settle_waiting",
    "start",
    "sweep_abandoned",
]

# In execution order. Each is optional so an operator (or a test) can run a
# bounded slice — "re-enqueue what is already downloaded" is a real request, and
# `sync` is the one stage that cannot be scoped at all (it reads NRB's whole REST
# corpus by nature).
STAGES = ("sync", "fetch", "extract", "rag")


class PipelineBusy(Exception):
    """Another orchestrator holds the lock. Carries the run in progress."""

    def __init__(self, run: "RunView | None") -> None:
        super().__init__(
            f"an NRB pipeline run is already in progress"
            + (f" (run {run.id}, stage {run.stage})" if run else "")
        )
        self.run = run


@dataclass(frozen=True)
class PipelineScope:
    """What was asked for. Stored verbatim on the run row.

    Every bound the stage services already accept, plus the two decisions that
    are not bounds: which stages to run, and whether failed documents are
    retried. `retry_failed` defaults FALSE — a routine update must not keep
    re-attempting a permanently unparseable file, and deciding otherwise is an
    operator's call (§21.1).
    """

    department: str | None = None
    stages: tuple[str, ...] = STAGES
    keys: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    resource_types: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    limit: int | None = None
    retry_failed: bool = False
    # Belt and braces for a caller that means "everything". The stage services
    # already treat empty scope as unbounded; this makes an unbounded run
    # something the CALLER had to say, which is what the CLI enforces.
    all_files: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "stages": list(self.stages),
            "keys": len(self.keys),          # a count, not 18k strings on the row
            "sections": list(self.sections),
            "owners": list(self.owners),
            "years": list(self.years),
            "resource_types": list(self.resource_types),
            "extensions": list(self.extensions),
            "limit": self.limit,
            "retry_failed": self.retry_failed,
            "all_files": self.all_files,
        }

    @property
    def is_bounded(self) -> bool:
        return bool(
            self.keys or self.sections or self.owners or self.years
            or self.resource_types or self.extensions or self.limit
        )

    def selection(self) -> dict[str, Any]:
        """The scope in the shape the stage services take it."""
        return {
            "keys": list(self.keys) or None,
            "sections": list(self.sections) or None,
            "owners": list(self.owners) or None,
            "years": list(self.years) or None,
            "resource_types": list(self.resource_types) or None,
            "limit": self.limit,
        }


@dataclass
class RunView:
    """One run, as a caller sees it. What a status endpoint would serialise."""

    id: int
    trigger: str
    requested_by: str | None
    status: str
    stage: str
    department: str | None
    scope: dict[str, Any]
    counters: dict[str, Any]
    error: str | None
    created_at: datetime | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    jobs: dict[str, int] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status not in (PIPELINE_RUNNING, PIPELINE_AWAITING)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trigger": self.trigger,
            "requested_by": self.requested_by,
            "status": self.status,
            "stage": self.stage,
            "department": self.department,
            "scope": self.scope,
            "counters": self.counters,
            "error": self.error,
            "jobs": self.jobs,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def _view(row: NRBPipelineRun, jobs: dict[str, int] | None = None) -> RunView:
    return RunView(
        id=row.id, trigger=row.trigger, requested_by=row.requested_by,
        status=row.status, stage=row.stage, department=row.department,
        scope=dict(row.scope or {}), counters=dict(row.counters or {}),
        error=row.error, created_at=row.created_at, started_at=row.started_at,
        heartbeat_at=row.heartbeat_at, finished_at=row.finished_at,
        jobs=jobs or {},
    )


# --------------------------------------------------------------------------- #
# Reading runs.
# --------------------------------------------------------------------------- #
async def _live_job_counts(session: AsyncSession, run_id: int) -> dict[str, int]:
    """This run's OWN jobs, by CURRENT status. The explicit relation, never a
    time window."""
    rows = (
        await session.execute(
            select(IngestJob.status, func.count())
            .join(NRBPipelineRunJob, NRBPipelineRunJob.job_id == IngestJob.id)
            .where(NRBPipelineRunJob.run_id == run_id)
            .group_by(IngestJob.status)
        )
    ).all()
    return {status: count for status, count in rows}


async def _job_counts(
    session: AsyncSession, run_id: int, *, row: NRBPipelineRun | None = None
) -> dict[str, int]:
    """A run's job counts: FROZEN once the run is terminal, live before that.

    A terminal run must describe what happened during THAT run, not the current
    state of the rows it happened to touch. So `_freeze_jobs` stamps the counts
    into `counters['jobs']` at the moment the run leaves `awaiting_jobs`/
    `running`, and from then on this returns the stamp.

    Two paths make the live query wrong for a terminal run, and only the second
    is reachable today:

    1. **A reused job row.** `jobs.enqueue` always INSERTs, so `--retry-failed`
       creates a NEW job against the existing document rather than reviving the
       old one, and `claim_next`/`sweep_stale` only ever touch `queued`/`running`
       rows. A terminal job is therefore never mutated — which is why the live
       query gave the right answer, but by borrowing an invariant from
       `app/rag/jobs.py` rather than holding one here. A future in-place retry
       would silently rewrite history.
    2. **A run that went terminal with jobs still in flight.** Reachable now: if
       anything raises after `_record_jobs` has associated the jobs (a database
       blip in `_mark_stage`), the run is recorded `failed` while its jobs are
       still `queued`. They then drain, and a run frozen at `failed` would start
       reporting `succeeded: N`.

    Freezing closes both. It costs one JSONB key of bounded integers on a column
    that is already the per-stage rollup — no migration, and no second table.
    """
    if row is not None and row.status not in (PIPELINE_RUNNING, PIPELINE_AWAITING):
        frozen = (row.counters or {}).get("jobs")
        if isinstance(frozen, dict):
            return {k: int(v) for k, v in frozen.items()}
    return await _live_job_counts(session, run_id)


def _with_frozen_jobs(counters: dict[str, Any], jobs: dict[str, int]) -> dict[str, Any]:
    """`counters` with this run's final job counts stamped in.

    Written into the counters JSONB rather than a new column so the freeze is one
    small change to an existing bounded rollup. `counters['jobs']` is the only
    key not contributed by a stage, which is why it is named for the thing it
    describes rather than for a stage.
    """
    merged = dict(counters or {})
    merged["jobs"] = {k: int(v) for k, v in jobs.items()}
    return merged


async def get_run(session: AsyncSession, run_id: int) -> RunView | None:
    row = (
        await session.execute(
            select(NRBPipelineRun)
            .where(NRBPipelineRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return _view(row, await _job_counts(session, run_id, row=row))


async def latest_run(session: AsyncSession) -> RunView | None:
    row = (
        await session.execute(
            select(NRBPipelineRun)
            .order_by(NRBPipelineRun.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return None if row is None else _view(
        row, await _job_counts(session, row.id, row=row)
    )


async def active_run(
    session: AsyncSession,
    *,
    statuses: tuple[str, ...] = PIPELINE_ACTIVE_STATUSES,
) -> RunView | None:
    """The NRB update currently in progress, if any.

    All THREE non-terminal statuses by default, because each is an update in
    progress: `queued` is accepted and waiting for a runner, `running` is an
    orchestrator mid-flight, `awaiting_jobs` is its documents mid-ingest
    (§24.3). The set is `models.PIPELINE_ACTIVE_STATUSES`, shared with the
    singleton index so the gate and the constraint cannot disagree.

    Callers that specifically want the LOCK holder — `running` by definition,
    since neither a queued nor a waiting run holds one — pass
    `statuses=(PIPELINE_RUNNING,)`.
    """
    row = (
        await session.execute(
            select(NRBPipelineRun)
            .where(NRBPipelineRun.status.in_(statuses))
            .order_by(NRBPipelineRun.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return None if row is None else _view(row)


# --------------------------------------------------------------------------- #
# Status arithmetic. Pure, so every rule below is testable without a database.
# --------------------------------------------------------------------------- #
def _stage_failures(counters: dict[str, Any]) -> int:
    """Item-level failures the stages reported. NOT stage crashes.

    A fetch that downloaded 99 files and failed 1 is a `partial` run, not a
    failed one — the other 99 are on disk and usable. A stage that RAISED is a
    different thing and is recorded as `error`.
    """
    total = 0
    for stage, key in (
        ("fetch", "failed"), ("extract", "failed"),
        ("rag", "missing_blob"), ("rag", "conflict_document"),
    ):
        value = (counters.get(stage) or {}).get(key)
        if isinstance(value, int):
            total += value
    return total


def resolve_status(counters: dict[str, Any], jobs: dict[str, int]) -> str:
    """The run's status given its stage counters and its own jobs' statuses.

    Order matters: waiting beats everything, because a run with one job still
    queued has not finished no matter how the others went.
    """
    if jobs.get(JOB_QUEUED) or jobs.get(JOB_RUNNING):
        return PIPELINE_AWAITING
    succeeded = jobs.get(JOB_SUCCEEDED, 0)
    failed = jobs.get(JOB_FAILED, 0) + _stage_failures(counters)
    if failed and succeeded:
        return PIPELINE_PARTIAL
    if failed:
        # Nothing this run staged came through. `partial` would overstate it;
        # `failed` is only reached when there is no success to weigh against.
        return PIPELINE_FAILED if not succeeded else PIPELINE_PARTIAL
    return PIPELINE_SUCCEEDED


async def reconcile(session: AsyncSession, run_id: int) -> RunView | None:
    """Recompute a waiting run's terminal status from its own jobs.

    Idempotent, callable from any process, and deliberately not a background
    task: the orchestrator may be long gone (the jobs outlive it by design), so
    whoever asks about the run is the one who advances it. A run that is already
    terminal is returned unchanged — its `finished_at` is not rewritten.
    """
    row = (
        await session.execute(
            select(NRBPipelineRun)
            .where(NRBPipelineRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.status != PIPELINE_AWAITING:
        # Terminal already: return the frozen history, and do NOT rewrite
        # `finished_at`. A status endpoint polls this on every request, and a run
        # that kept moving its own completion time would make "how long did the
        # update take" unanswerable.
        return _view(row, await _job_counts(session, run_id, row=row))

    jobs = await _live_job_counts(session, run_id)
    status = resolve_status(dict(row.counters or {}), jobs)
    if status == PIPELINE_AWAITING:
        return _view(row, jobs)
    row.status = status
    row.stage = "done"
    row.finished_at = datetime.now(timezone.utc)
    # THE freeze. From here on this run reports these numbers, whatever later
    # runs do to the documents it touched.
    row.counters = _with_frozen_jobs(row.counters, jobs)
    await session.flush()
    logger.info("NRB pipeline run %s finished: %s (jobs %s)", run_id, status, jobs)
    return _view(row, jobs)


async def settle_waiting(session: AsyncSession) -> int:
    """Reconcile every `awaiting_jobs` run. Returns how many are STILL waiting.

    Called while holding the lock, immediately before deciding whether a new run
    may start. Without it, `awaiting_jobs` blocking a new run would be a trap: a
    run whose jobs all finished but which nobody ever asked about would wedge the
    pipeline forever, because `reconcile` only advances a run when someone reads
    it. Reconciling here means a stale wait costs one query, not an operator.
    """
    ids = (
        await session.execute(
            select(NRBPipelineRun.id).where(NRBPipelineRun.status == PIPELINE_AWAITING)
        )
    ).scalars().all()
    still_waiting = 0
    for run_id in ids:
        view = await reconcile(session, run_id)
        if view is not None and view.status == PIPELINE_AWAITING:
            still_waiting += 1
    return still_waiting


async def recover_abandoned(
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Clear what a dead process left behind. Returns how many runs were swept.

    THE UNWEDGING STEP, and it has to be callable without any work to do.
    Admission is now guarded by `ux_nrb_pipeline_runs_one_active`, so a run left
    `running` by a killed runner occupies the only active slot — and
    `execute_run`'s own sweep cannot help, because it only runs when there is a
    `queued` run to execute and no `queued` run can be created while the corpse
    is there. That is a deadlock, and this is what breaks it: every process that
    is *able* to orchestrate calls this before it looks for work.

    Takes the advisory lock, which is what makes the sweep sound with no timeout
    to tune: an orchestrator is alive IFF it holds the key, so if we got it, any
    `running` row is a corpse. `LockBusy` means a runner IS orchestrating — there
    is nothing abandoned to find, so it is not an error and not a wait.

    `settle_waiting` runs too, so a run whose jobs all finished stops occupying
    the slot even if nobody ever polled it. Between them, no unobserved failure
    can permanently prevent the next update.
    """
    from ..db.session import SessionLocal, engine as app_engine

    engine = engine or app_engine
    Session = session_factory or SessionLocal
    try:
        async with advisory_lock(engine, PIPELINE_LOCK_KEY, what="NRB recovery"):
            async with Session() as session:
                swept = await sweep_abandoned(session)
                await settle_waiting(session)
                await session.commit()
            return swept
    except LockBusy:
        return 0


async def sweep_abandoned(session: AsyncSession) -> int:
    """Fail every run still `running`. Only ever called while holding the lock.

    That is the whole argument for its safety and it is why there is no timeout:
    an orchestrator is running IFF it holds `PIPELINE_LOCK_KEY`, so if this
    process holds it, any row claiming to be `running` belongs to a process that
    died. `awaiting_jobs` rows are untouched — they hold no lock, they are
    legitimately unfinished, and their jobs are still being drained.
    """
    result = await session.execute(
        update(NRBPipelineRun)
        .where(NRBPipelineRun.status == PIPELINE_RUNNING)
        .values(
            status=PIPELINE_FAILED,
            stage="done",
            finished_at=func.now(),
            error=(
                "orchestrator did not finish: no process held the pipeline lock "
                "when the next run started"
            ),
        )
    )
    swept = result.rowcount or 0
    if swept:
        logger.warning("NRB pipeline: swept %d abandoned run(s)", swept)
    return swept


# --------------------------------------------------------------------------- #
# The orchestration.
# --------------------------------------------------------------------------- #
def _scope_from_row(row: NRBPipelineRun) -> PipelineScope:
    """Rebuild the requested scope from the stored row.

    The runner is a different PROCESS from the requester, so the scope has to
    survive as data. `PipelineScope.as_dict` stores `keys` as a COUNT rather than
    18k strings, which is right for a status view and useless here — so the
    verbatim key list travels in `scope['key_list']`, written by `request_run`
    and read only here. Everything else round-trips as itself.
    """
    stored = dict(row.scope or {})
    return PipelineScope(
        department=row.department,
        stages=tuple(stored.get("stages") or STAGES),
        keys=tuple(stored.get("key_list") or ()),
        sections=tuple(stored.get("sections") or ()),
        owners=tuple(stored.get("owners") or ()),
        years=tuple(int(y) for y in (stored.get("years") or ())),
        resource_types=tuple(stored.get("resource_types") or ()),
        extensions=tuple(stored.get("extensions") or ()),
        limit=stored.get("limit"),
        retry_failed=bool(stored.get("retry_failed")),
        all_files=bool(stored.get("all_files")),
    )


async def _claim(Session, run_id: int) -> PipelineScope | None:
    """Take this run from `queued` to `running`, or return None.

    `FOR UPDATE SKIP LOCKED` on the one row, the same shape `jobs.claim_next`
    uses: two runners polling the same queue pass over each other rather than
    blocking, and the loser sees `queued` gone and moves on. The advisory lock
    already means only one runner orchestrates at a time; this is what makes the
    CLAIM itself unambiguous, and it is what keeps the transition atomic even for
    a caller that skipped the lock.
    """
    async with Session() as session:
        row = (
            await session.execute(
                select(NRBPipelineRun)
                .where(
                    NRBPipelineRun.id == run_id,
                    NRBPipelineRun.status == PIPELINE_QUEUED,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        scope = _scope_from_row(row)
        row.status = PIPELINE_RUNNING
        row.stage = scope.stages[0] if scope.stages else "done"
        row.started_at = datetime.now(timezone.utc)
        row.heartbeat_at = datetime.now(timezone.utc)
        await session.commit()
    return scope


async def claim_next(Session) -> int | None:
    """The oldest `queued` run's id, or None. The runner's poll.

    Reads only — the claim itself is `_claim`, inside `execute_run` and under the
    advisory lock. Splitting them keeps the runner loop free of any transition:
    it asks what is waiting, then asks the service to run it.
    """
    async with Session() as session:
        run_id = (
            await session.execute(
                select(NRBPipelineRun.id)
                .where(NRBPipelineRun.status == PIPELINE_QUEUED)
                .order_by(NRBPipelineRun.created_at, NRBPipelineRun.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        await session.rollback()
    return run_id


async def request_run(
    scope: PipelineScope,
    *,
    trigger: str = "cli",
    requested_by: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> RunView:
    """DURABLY ACCEPT an update request. Fast, and it executes nothing.

    THE admission point — the API, the CLI and a future schedule all come
    through here, so there is one definition of "an update was requested" and one
    place the admission rule lives. It inserts a `queued` row and returns; the
    stages are `app/nrb/runner.py`'s to execute.

    Why it takes no advisory lock: this runs inside an HTTP request and must
    return in milliseconds, while the lock is held for the whole of a run's
    orchestration (minutes, once `sync` is involved). Taking it here would make
    the API's answer depend on whether a runner happened to be mid-flight, which
    is exactly the coupling that made the previous synchronous design a problem.

    So admission is guarded twice, on purpose:

      * the SELECT gate below, which is what produces the useful `PipelineBusy`
        carrying the run already in progress; and
      * `ux_nrb_pipeline_runs_one_active`, a UNIQUE index over a constant
        restricted to the active statuses, which is what makes the gate not have
        to be correct. Two simultaneous requests both passing the SELECT end with
        one insert and one `IntegrityError` — caught below and answered exactly as
        a lost race should be, with the run that won.

    `settle_waiting` runs first for the reason it always does: a run whose jobs
    have all finished but which nobody polled must stop counting as active, or
    the first unobserved completion wedges every later request.
    """
    from ..db.session import SessionLocal

    Session = session_factory or SessionLocal

    async with Session() as session:
        await settle_waiting(session)
        await session.commit()

    async with Session() as session:
        active = await active_run(session)
        await session.rollback()
    if active is not None:
        raise PipelineBusy(active)

    async with Session() as session:
        row = NRBPipelineRun(
            trigger=trigger,
            requested_by=requested_by,
            status=PIPELINE_QUEUED,
            stage=PIPELINE_QUEUED,
            department=scope.department,
            # `key_list` is the verbatim scope for the RUNNER to read back;
            # `as_dict`'s `keys` stays a count, which is what a status view wants.
            scope={**scope.as_dict(), "key_list": list(scope.keys)},
            counters={},
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # Lost the race to the singleton index. Not an error to the caller —
            # the same answer the gate would have given a moment earlier.
            await session.rollback()
            winner = await active_run(session)
            await session.rollback()
            raise PipelineBusy(winner) from None
        view = _view(row)
        await session.commit()

    logger.info(
        "NRB pipeline run %s accepted (%s, stages=%s, dept=%s)",
        view.id, trigger, ",".join(scope.stages), scope.department,
    )
    return view


async def _mark_stage(Session, run_id: int, stage: str, counters: dict) -> None:
    """Advance the stage and merge in its counters. One short transaction.

    Written per stage rather than at the end so a run that is killed mid-pass
    still says how far it got — the difference between "we know it died during
    fetch" and "we know nothing".
    """
    async with Session() as session:
        await session.execute(
            update(NRBPipelineRun)
            .where(NRBPipelineRun.id == run_id)
            .values(
                stage=stage,
                heartbeat_at=func.now(),
                # A jsonb merge, with the bind typed as text before the cast.
                # `cast(<py str>, JSONB)` would bind the parameter AS jsonb,
                # serialising it into a JSON *string scalar*, and
                # `object || string` is a legal operation producing an ARRAY —
                # the same trap `supersession._stamp` documents.
                counters=NRBPipelineRun.counters.op("||")(
                    cast(literal(json.dumps({stage: counters}), String), JSONB)
                ),
            )
        )
        await session.commit()


async def _record_jobs(
    Session, run_id: int, jobs: Sequence[tuple[str, str]], *, reason: str
) -> None:
    if not jobs:
        return
    async with Session() as session:
        await session.execute(
            NRBPipelineRunJob.__table__.insert(),
            [
                {"run_id": run_id, "job_id": job_id, "document_id": doc_id,
                 "reason": reason}
                for job_id, doc_id in jobs
            ],
        )
        await session.commit()


async def _finish(
    Session, run_id: int, *, status: str, error: str | None = None
) -> None:
    """Move a run out of `running`, freezing its job counts if that is terminal.

    The freeze matters most on the failure path, which is the one place a run can
    go terminal while its jobs are still in flight: if anything raises after
    `_record_jobs` has associated them, they are `queued` at this moment and will
    drain afterwards. Stamping them here is what stops a run recorded `failed`
    from later reporting `succeeded: N`.
    """
    async with Session() as session:
        row = (
            await session.execute(
                select(NRBPipelineRun)
                .where(NRBPipelineRun.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        row.status = status
        row.heartbeat_at = datetime.now(timezone.utc)
        if error is not None:
            row.error = error[:4000]
        if status == PIPELINE_AWAITING:
            row.stage = "waiting"
        else:
            row.stage = "done"
            row.finished_at = datetime.now(timezone.utc)
            row.counters = _with_frozen_jobs(
                row.counters, await _live_job_counts(session, run_id)
            )
        await session.commit()


async def execute_run(
    run_id: int,
    *,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    dry_run: bool = False,
) -> RunView:
    """Execute one ALREADY-ACCEPTED run: claim it, stage it, record it.

    The second half of the pair. `request_run` admitted the request and returned;
    this is what `app/nrb/runner.py` calls to actually do it, holding the advisory
    lock for the whole of it so two runners can never orchestrate at once.

    Returns the run as it stands when staging ends — usually `awaiting_jobs`,
    because the RAG worker owns everything after the enqueue.

    Three ways it declines to do the work, none of them an error:

      * `LockBusy` from the lock — another runner is mid-flight. Raised as
        `PipelineBusy` carrying that runner's `running` row.
      * the run is no longer `queued` — another runner claimed it between the
        poll and the claim, or the sweep failed it. `_claim` returns None and the
        current row is returned untouched.
      * `run_id` does not exist.

    A stage that RAISES ends the run `failed` with the stage named, and stops —
    later stages would be operating on a corpus state the failed stage was
    supposed to establish. Item-level failures inside a stage do not stop
    anything; they surface in the counters and make the run `partial`.

    The searchable corpus is untouched by any failure here. This function
    creates and enqueues; it never recovers, never embeds, never archives and
    never writes a chunk — that division is why the RAG worker exists and is not
    changed by moving orchestration into a process of its own.
    """
    from ..db.session import SessionLocal, engine as app_engine
    from . import corpus as corpus_mod
    from . import extract as extract_mod
    from . import fetch as fetch_mod
    from . import sync as sync_mod

    engine = engine or app_engine
    Session = session_factory or SessionLocal
    scope = PipelineScope()

    try:
        async with advisory_lock(engine, PIPELINE_LOCK_KEY, what="NRB pipeline"):
            # Sweep runs left `running` by a dead orchestrator BEFORE claiming.
            # Safe with no timeout precisely because we hold the lock: an
            # orchestrator is alive IFF it holds this key, so a `running` row is
            # a corpse. It must happen before the claim, or a crashed run would
            # keep the singleton index occupied and nothing could ever be
            # accepted again.
            async with Session() as session:
                await sweep_abandoned(session)
                await session.commit()

            claimed = await _claim(Session, run_id)
            if claimed is None:
                # Someone else claimed it, or it is no longer `queued` at all
                # (swept, or already run). Not this runner's work.
                async with Session() as session:
                    view = await get_run(session, run_id)
                    await session.rollback()
                return view  # type: ignore[return-value]
            scope = claimed
            logger.info(
                "NRB pipeline run %s started (stages=%s, dept=%s)",
                run_id, ",".join(scope.stages), scope.department,
            )
            selection = scope.selection()
            queued = 0
            try:
                if "sync" in scope.stages:
                    result = await sync_mod.run_sync(
                        engine=engine, session_factory=Session, dry_run=dry_run
                    )
                    await _mark_stage(Session, run_id, "sync", dict(result.counters))

                if "fetch" in scope.stages:
                    result = await fetch_mod.run_fetch(
                        engine=engine, session_factory=Session, dry_run=dry_run,
                        retry_failed=scope.retry_failed, **selection,
                    )
                    await _mark_stage(Session, run_id, "fetch", dict(result.counters))

                if "extract" in scope.stages:
                    result = await extract_mod.run_extract(
                        engine=engine, session_factory=Session, dry_run=dry_run,
                        **selection,
                    )
                    await _mark_stage(
                        Session, run_id, "extract", dict(result.counters)
                    )

                if "rag" in scope.stages:
                    if not scope.department:
                        raise ValueError(
                            "the rag stage needs a department to ingest into"
                        )
                    rag = await corpus_mod.run_rag_enqueue(
                        Session,
                        department_code=scope.department,
                        keys=list(scope.keys) or None,
                        sections=list(scope.sections) or None,
                        owners=list(scope.owners) or None,
                        years=list(scope.years) or None,
                        resource_types=list(scope.resource_types) or None,
                        extensions=list(scope.extensions) or None,
                        limit=scope.limit,
                        retry_failed=scope.retry_failed,
                        dry_run=dry_run,
                    )
                    await _record_jobs(
                        Session, run_id, rag.created_jobs, reason="created"
                    )
                    await _record_jobs(
                        Session, run_id, rag.retried_jobs, reason="retried"
                    )
                    queued = len(rag.created_jobs) + len(rag.retried_jobs)
                    await _mark_stage(Session, run_id, "rag", rag.counters())
            except Exception as exc:  # noqa: BLE001 - one run must not kill a caller
                logger.exception("NRB pipeline run %s failed", run_id)
                await _finish(
                    Session, run_id, status=PIPELINE_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
                async with Session() as session:
                    view = await get_run(session, run_id)
                    await session.rollback()
                return view  # type: ignore[return-value]

            # Staging is done. If it queued nothing there is nothing to wait
            # for, so the run can settle immediately; otherwise the worker owns
            # the rest and `reconcile` finishes the run later.
            if queued:
                await _finish(Session, run_id, status=PIPELINE_AWAITING)
            else:
                async with Session() as session:
                    row = await get_run(session, run_id)
                    await session.rollback()
                await _finish(
                    Session, run_id,
                    status=resolve_status(row.counters if row else {}, {}),
                )
    except LockBusy:
        # Another orchestrator is mid-flight. Report ITS run, which is `running`
        # by definition — a waiting run holds no lock.
        async with Session() as session:
            active = await active_run(session, statuses=(PIPELINE_RUNNING,))
            await session.rollback()
        raise PipelineBusy(active) from None

    async with Session() as session:
        view = await get_run(session, run_id)
        await session.rollback()
    return view  # type: ignore[return-value]


async def start(
    scope: PipelineScope,
    *,
    trigger: str = "cli",
    requested_by: str | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    dry_run: bool = False,
) -> RunView:
    """Request a run AND execute it here. `request_run` then `execute_run`.

    The synchronous convenience, and deliberately nothing more than those two
    calls in that order — so it is not a third orchestration path, it is the
    same one without the process boundary. Used by
    `scripts/nrb_pipeline.py --run-now` (a laptop session wants the answer, not a
    daemon) and by tests.

    **The API never calls this.** `POST /v1/nrb/runs` calls `request_run` and
    returns; staging is `app/nrb/runner.py`'s. That separation is the whole point
    of Phase 7 step 6 — a request that included `sync` used to hold an HTTP
    connection open for minutes — and collapsing it back here would undo it.
    """
    # Same first move a runner makes: this process is able to orchestrate, so it
    # is the one that can prove nothing else is and clear a corpse out of the
    # active slot. Without it, one crashed run would refuse every later request.
    await recover_abandoned(engine=engine, session_factory=session_factory)
    view = await request_run(
        scope, trigger=trigger, requested_by=requested_by,
        session_factory=session_factory,
    )
    return await execute_run(
        view.id, engine=engine, session_factory=session_factory, dry_run=dry_run
    )
