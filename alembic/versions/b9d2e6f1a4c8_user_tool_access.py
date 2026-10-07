"""user_tool_access

Revision ID: b9d2e6f1a4c8
Revises: e1a4c6f9b2d7
Create Date: 2026-10-02

Per-user overrides of local tool access. A row exists only where an admin has
decided something for that user; no row means the default starter set
(`app/toolaccess/policy.py`). No CHECK on tool_name: the vocabulary is the
code's tool list, and the admin route refuses unknown names.
"""

import sqlalchemy as sa
from alembic import op

revision = "b9d2e6f1a4c8"
down_revision = "e1a4c6f9b2d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_tool_access",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "tool_name"),
    )


def downgrade() -> None:
    op.drop_table("user_tool_access")
