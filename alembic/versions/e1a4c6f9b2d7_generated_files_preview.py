"""generated_files.preview

Revision ID: e1a4c6f9b2d7
Revises: a3f7c21e8b04
Create Date: 2026-09-20

RECONSTRUCTED. Production ran this revision (alembic_version measured
2026-09-14) but the file never reached this repository. A full schema diff
between the production snapshot and a database at `a3f7c21e8b04` showed a
single difference — this nullable JSONB column — so the revision is recreated
under its ORIGINAL id: a dump built here now restores into production with one
linear head, and `alembic upgrade head` on production is a no-op.

Production writes the column for generated .pptx files (the deck's slide
structure, for a preview in the frontend). Nothing in this repository writes
it yet; the ORM column exists so autogenerate does not propose dropping it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1a4c6f9b2d7"
down_revision = "a3f7c21e8b04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generated_files",
        sa.Column("preview", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generated_files", "preview")
