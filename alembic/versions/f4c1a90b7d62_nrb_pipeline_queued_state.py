"""nrb pipeline queued state + one-active-run index

Revision ID: f4c1a90b7d62
Revises: 1fb5a0d183d6
Create Date: 2026-08-17

Phase 7 step 6 — orchestration moves out of the HTTP request
(`docs/nrb-integration.md` §26).

WHY A MIGRATION IS UNAVOIDABLE HERE
    Not for tidiness. `nrb_pipeline_runs` carries three CHECK constraints that
    enumerate the exact strings a row may hold, and the new lifecycle needs a
    status and a stage that those vocabularies forbid:

      * `ck_nrb_pipeline_runs_status` — `queued` is a new status. It was
        deliberately absent while the only caller ran the stages inline; now
        `POST` accepts a run durably and a runner claims it later, so a run
        really can exist that nothing is executing.
      * `ck_nrb_pipeline_runs_stage` — `queued` is also a stage, so an unclaimed
        run does not have to claim a stage it has not reached. `stage='sync'` on
        a run nobody has started would read to a UI as a sync in progress.
      * `ck_nrb_pipeline_runs_finished` — its predicate lists the statuses that
        must have a NULL `finished_at`, and `queued` is one of them.

    Editing a CHECK's vocabulary is exactly the case CLAUDE.md calls out ("adding
    a status value means editing the CHECK too"), so it is a DDL change or
    nothing.

    The index is the second half. Admission moved out of the orchestrator, so
    `POST` inserts without taking the advisory lock — two simultaneous requests
    could both pass a plain SELECT gate. `ux_nrb_pipeline_runs_one_active` is
    UNIQUE over the constant `(true)` restricted to the active statuses: the
    singleton-row idiom, making "at most one active NRB update" a database
    invariant rather than a property of the gate being correct. Same posture as
    `ux_documents_active_content` and `ux_documents_nrb_current_source`.

    No column is added, no data is rewritten, and no existing row changes
    meaning: every pre-existing run is already `running`/`awaiting_jobs` or
    terminal.

LINEAGE
    `down_revision` is `1fb5a0d183d6`, this branch's actual head. The deferred
    `feat/rag-source-citations` lineage (`d4a91f2c7b3e`) is untouched and nothing
    is stamped; the eventual merge revision is still required and still not
    solved here. Applied against `local_ai_gateway_p4` only.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f4c1a90b7d62"
down_revision: Union[str, None] = "1fb5a0d183d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_STATUS = "status IN ('running', 'awaiting_jobs', 'succeeded', 'partial', 'failed')"
_NEW_STATUS = (
    "status IN ('queued', 'running', 'awaiting_jobs', 'succeeded', 'partial', "
    "'failed')"
)
_OLD_STAGE = "stage IN ('sync', 'fetch', 'extract', 'rag', 'waiting', 'done')"
_NEW_STAGE = (
    "stage IN ('queued', 'sync', 'fetch', 'extract', 'rag', 'waiting', 'done')"
)
_OLD_FINISHED = "(finished_at IS NULL) = (status IN ('running', 'awaiting_jobs'))"
_NEW_FINISHED = (
    "(finished_at IS NULL) = (status IN ('queued', 'running', 'awaiting_jobs'))"
)


def _swap(name: str, expression: str) -> None:
    op.drop_constraint(name, "nrb_pipeline_runs", type_="check")
    op.create_check_constraint(name, "nrb_pipeline_runs", expression)


def upgrade() -> None:
    _swap("ck_nrb_pipeline_runs_status", _NEW_STATUS)
    _swap("ck_nrb_pipeline_runs_stage", _NEW_STAGE)
    _swap("ck_nrb_pipeline_runs_finished", _NEW_FINISHED)
    # Hand-written: Alembic can emit neither the constant expression nor the
    # partial predicate.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_nrb_pipeline_runs_one_active
            ON nrb_pipeline_runs ((true))
         WHERE status IN ('queued', 'running', 'awaiting_jobs')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_nrb_pipeline_runs_one_active")
    # Any `queued` run must go before the old vocabulary can be restored; there
    # is nothing to run it under the previous design.
    op.execute(
        "UPDATE nrb_pipeline_runs SET status = 'failed', stage = 'done', "
        "       finished_at = now(), "
        "       error = 'queued run discarded by downgrade' "
        " WHERE status = 'queued' OR stage = 'queued'"
    )
    _swap("ck_nrb_pipeline_runs_finished", _OLD_FINISHED)
    _swap("ck_nrb_pipeline_runs_stage", _OLD_STAGE)
    _swap("ck_nrb_pipeline_runs_status", _OLD_STATUS)
