"""add nrb file download columns and nrb_fetch_runs

Revision ID: 2b7f5c9d1a34
Revises: 9a1c4f7b2e05
Create Date: 2026-08-14 15:10:00.000000

Phase 5: the catalog can now record that it *has* a file, not just that NRB
publishes one. Adds the content columns to `nrb_files` and a run log for download
passes.

Two things here are edits rather than additions, and both are deliberate:

  * `ck_nrb_files_fetch_status` is **dropped and recreated** to widen the
    vocabulary to `fetched`/`failed`. `app/nrb/models.py` warns that a status
    outside the CHECK would match no predicate and no query, so adding a value
    means editing the CHECK — this is that edit, not a bypass.
  * `ck_nrb_files_fetched_is_complete` is new and is the reason a half-written row
    cannot exist: a `fetched` row that cannot say which bytes it holds would read
    to Phase 6 as available and then resolve to nothing.

Downgrade drops the columns and the table and restores the two-value CHECK. That
is lossy by nature — it discards the record of what was downloaded — so it first
resets any `fetched`/`failed` row back to `pending`, which is the only value the
narrower CHECK would accept. The blobs on disk under NRB_FILES_DIR are NOT touched
by a downgrade; they are content-addressed, so a re-upgrade plus a re-fetch
recognises them as already present.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '2b7f5c9d1a34'
down_revision: Union[str, None] = '9a1c4f7b2e05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------ fetch runs #
    # Created first: nrb_files.last_fetch_run_id references it.
    op.create_table(
        "nrb_fetch_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # Which slice of the corpus this run was told to fetch. A fetch is always
        # partial by design, so the counters alone cannot say what was considered.
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("files_selected", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("files_fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("files_failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("files_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("files_deduplicated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("bytes_downloaded", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("bytes_stored", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_nrb_fetch_runs_status",
        ),
    )

    # ------------------------------------------------- nrb_files new columns #
    op.add_column("nrb_files", sa.Column("content_sha256", sa.String(length=64), nullable=True))
    op.add_column("nrb_files", sa.Column("content_length", sa.BigInteger(), nullable=True))
    # RELATIVE, content-addressed key under NRB_FILES_DIR.
    op.add_column("nrb_files", sa.Column("storage_key", sa.String(length=1024), nullable=True))
    # What the bytes say they are, beside NRB's claim in reported_mime_type.
    op.add_column("nrb_files", sa.Column("sniffed_mime", sa.String(length=128), nullable=True))
    op.add_column("nrb_files", sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("nrb_files", sa.Column("fetch_attempts", sa.Integer(),
                                         server_default=sa.text("0"), nullable=False))
    op.add_column("nrb_files", sa.Column("fetch_error", sa.Text(), nullable=True))
    op.add_column("nrb_files", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column("nrb_files", sa.Column("last_fetch_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_nrb_files_last_fetch_run", "nrb_files", "nrb_fetch_runs",
        ["last_fetch_run_id"], ["id"], ondelete="SET NULL",
    )
    # Not unique: two rows legitimately share bytes (the same PDF under a second
    # URL), which is what this index makes findable.
    op.create_index("ix_nrb_files_content_sha256", "nrb_files", ["content_sha256"])

    # ------------------------------------------------------ widened vocabulary #
    op.drop_constraint("ck_nrb_files_fetch_status", "nrb_files", type_="check")
    op.create_check_constraint(
        "ck_nrb_files_fetch_status", "nrb_files",
        "fetch_status IN ('pending', 'blocked_host', 'fetched', 'failed')",
    )
    # All three columns or none: a `fetched` row that cannot name its bytes reads
    # to Phase 6 as available and resolves to nothing.
    op.create_check_constraint(
        "ck_nrb_files_fetched_is_complete", "nrb_files",
        "(fetch_status <> 'fetched') OR (content_sha256 IS NOT NULL"
        " AND content_length IS NOT NULL AND storage_key IS NOT NULL)",
    )


def downgrade() -> None:
    # The narrower CHECK cannot accept the new statuses, so reset them first. This
    # discards the record of which files were downloaded; the blobs themselves stay
    # on disk and, being content-addressed, are recognised again after a re-fetch.
    op.execute(
        "UPDATE nrb_files SET fetch_status = 'pending'"
        " WHERE fetch_status IN ('fetched', 'failed')"
    )
    op.drop_constraint("ck_nrb_files_fetched_is_complete", "nrb_files", type_="check")
    op.drop_constraint("ck_nrb_files_fetch_status", "nrb_files", type_="check")
    op.create_check_constraint(
        "ck_nrb_files_fetch_status", "nrb_files",
        "fetch_status IN ('pending', 'blocked_host')",
    )
    op.drop_index("ix_nrb_files_content_sha256", table_name="nrb_files")
    op.drop_constraint("fk_nrb_files_last_fetch_run", "nrb_files", type_="foreignkey")
    for column in (
        "last_fetch_run_id", "http_status", "fetch_error", "fetch_attempts",
        "downloaded_at", "sniffed_mime", "storage_key", "content_length",
        "content_sha256",
    ):
        op.drop_column("nrb_files", column)
    op.drop_table("nrb_fetch_runs")
