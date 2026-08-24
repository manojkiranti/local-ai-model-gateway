"""user_mcp_grants

Revision ID: a3f7c21e8b04
Revises: b7e1c4d92a03
Create Date: 2026-08-24

Per-user MCP tool grants. The CHECK enumerates the vocabulary as a literal, so
adding a grant is a schema change and not a config change — the same
arrangement as ck_api_keys_scopes. The literal below must stay in the sorted
order `app/mcp/models.py` generates (`sorted(ALL_GRANTS)`), or a future
autogenerate run will propose a spurious diff.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f7c21e8b04"
down_revision = "b7e1c4d92a03"
branch_labels = None
depends_on = None

_VOCABULARY = (
    "grant_key IN ('mcp-ems', 'mcp-hrms', 'mcp-izone', "
    "'mcp.ems.query', 'mcp.hrms.full', 'mcp.hrms.tasks')"
)


def upgrade() -> None:
    op.create_table(
        "user_mcp_grants",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("grant_key", sa.String(length=64), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(_VOCABULARY, name="ck_user_mcp_grants_key"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "grant_key"),
    )


def downgrade() -> None:
    op.drop_table("user_mcp_grants")
