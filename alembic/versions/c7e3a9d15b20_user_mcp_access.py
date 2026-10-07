"""user_mcp_access

Revision ID: c7e3a9d15b20
Revises: b9d2e6f1a4c8
Create Date: 2026-10-05

Per-user MCP tool access (`app/toolaccess/mcp_policy.py`): a none/read/full
level per MCP system, and per-tool overrides that beat it. Rows exist only
where an admin decided something; no row means the default (read). Systems and
levels are the gateway's own vocabulary and are CHECKed; tool names are the
MCP server's live list and are validated by the admin route instead.
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e3a9d15b20"
down_revision = "b9d2e6f1a4c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_mcp_levels",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("system", sa.String(length=16), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.CheckConstraint("system IN ('hrms', 'izone', 'ems', 'other')", name="ck_user_mcp_levels_system"),
        sa.CheckConstraint("level IN ('none', 'read', 'full')", name="ck_user_mcp_levels_level"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "system"),
    )
    op.create_table(
        "user_mcp_tool_access",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "tool_name"),
    )


def downgrade() -> None:
    op.drop_table("user_mcp_tool_access")
    op.drop_table("user_mcp_levels")
