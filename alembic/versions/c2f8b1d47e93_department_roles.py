"""department-scoped roles: a level on every grant, and two closed vocabularies

`user_departments.role` says what a granted user may DO in a department, ordered
viewer < editor < owner (`app/rag/permissions.py`). Before this, a grant was binary
and the only way to let someone curate their own department's corpus was to make
them a GLOBAL admin — over every other department, the user table and the NRB
pipeline.

NOT NULL DEFAULT 'viewer' backfills every existing grant in the ALTER itself, and
'viewer' is exactly what a granted member could already do (curation was
admin-only), so this migration is behaviour-neutral: nobody gains or loses a
capability until an admin sets a level.

The default STAYS after the backfill. Least privilege is the right failure mode for
an insert that forgets the level.

`ck_users_role` closes a vocabulary that was open: `require_admin` compares
`users.role` to the exact string 'admin', so an unrecognised value is silently a
non-admin. Same rule as ck_users_auth_provider and ck_documents_status. It is not
backfillable, and should not be — if the ALTER fails, a row already violates the
rule and wants a human. Check before upgrading:

    SELECT id, email, role FROM users WHERE role NOT IN ('admin', 'member');

Every row was created by `POST /auth/register` or directory provisioning, both of
which write 'admin' or 'member', so the expected result is zero rows.

Revision ID: c2f8b1d47e93
Revises: b7e3d95a41c8
Create Date: 2026-08-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2f8b1d47e93"
down_revision: Union[str, None] = "b7e3d95a41c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_departments",
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'viewer'"),
        ),
    )
    op.create_check_constraint(
        "ck_user_departments_role",
        "user_departments",
        "role IN ('viewer', 'editor', 'owner')",
    )
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'member')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_constraint(
        "ck_user_departments_role", "user_departments", type_="check"
    )
    op.drop_column("user_departments", "role")
