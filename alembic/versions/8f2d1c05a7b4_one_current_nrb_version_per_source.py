"""one current NRB version per logical source

Revision ID: 8f2d1c05a7b4
Revises: 714264eba2fd
Create Date: 2026-08-17

Phase 7 step 3 — safe NRB supersession (`docs/nrb-integration.md` §22).

ONE INDEX, NO NEW COLUMN, NO DATA MIGRATION
    The logical source identity (`metadata->>'comparison_key'`), the version
    identity (`content_hash`) and the current/archived state (`status`) all
    already exist on `documents`, and both NRB ingest drivers already write the
    metadata. Nothing here is added for tidiness.

    What JSONB alone cannot do is REFUSE two current versions of one source.
    `ux_documents_active_content` is keyed on `content_hash`, and two versions
    of a republished circular have two different hashes — so it is satisfied by
    exactly the state supersession exists to prevent. Row locking in
    `app/nrb/supersession.py` serialises two promoting workers; this index is
    what makes the invariant a property of the database rather than of that
    file continuing to be correct. Same posture as the index above it and as
    `document_chunks`' composite FK.

    Partial and expression-based, so it touches nothing else: a row without
    `metadata->>'comparison_key'` indexes as NULL and never conflicts, which
    leaves ordinary uploads, typed text and pre-Phase-7 NRB documents exactly
    as they were. Verified against `local_ai_gateway_p4` before creation — 39
    NRB documents, all carrying a `comparison_key`, zero duplicate
    (department, key) pairs among `ready` rows.

LINEAGE
    `down_revision` is `714264eba2fd`, this branch's actual head. The deferred
    `feat/rag-source-citations` lineage (`d4a91f2c7b3e`) is untouched and
    nothing is stamped; when the branches meet, a merge revision will still be
    needed, and that is not solved here.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f2d1c05a7b4"
down_revision: Union[str, None] = "714264eba2fd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Hand-written: Alembic cannot emit a JSONB expression index.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_documents_nrb_current_source
            ON documents (department_id, ((metadata ->> 'comparison_key')))
         WHERE status = 'ready'
           AND metadata ->> 'origin' = 'nrb'
           AND metadata ->> 'comparison_key' IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_documents_nrb_current_source")
