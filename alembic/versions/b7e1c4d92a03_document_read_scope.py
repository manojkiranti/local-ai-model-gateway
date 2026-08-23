"""add the document:read scope to ck_api_keys_scopes

Revision ID: b7e1c4d92a03
Revises: 53c2ce388596
Create Date: 2026-08-23

`ck_api_keys_scopes` enumerates the vocabulary as a literal, so a new scope is
a schema change, not a config change. That is the point: the CHECK stops a
typo'd scope being STORED while `policy.ALL_SCOPES` stops one being HONOURED.
The literal below must stay in the same sorted order `app/apikeys/models.py`
generates it in (`sorted(ALL_SCOPES)`), or a future autogenerate run will
propose a spurious diff.
"""

from alembic import op

revision = "b7e1c4d92a03"
down_revision = "53c2ce388596"
branch_labels = None
depends_on = None

_NEW = "scopes <@ ARRAY['document:read', 'ocr:read']::text[]"
_OLD = "scopes <@ ARRAY['ocr:read']::text[]"


def upgrade() -> None:
    op.drop_constraint("ck_api_keys_scopes", "api_keys", type_="check")
    op.create_check_constraint("ck_api_keys_scopes", "api_keys", _NEW)


def downgrade() -> None:
    # A key already holding document:read would violate the old CHECK, so strip
    # it first. Revoking a capability on downgrade is correct; leaving a row
    # that the constraint forbids is not.
    op.execute(
        "UPDATE api_keys SET scopes = array_remove(scopes, 'document:read')"
    )
    op.drop_constraint("ck_api_keys_scopes", "api_keys", type_="check")
    op.create_check_constraint("ck_api_keys_scopes", "api_keys", _OLD)
