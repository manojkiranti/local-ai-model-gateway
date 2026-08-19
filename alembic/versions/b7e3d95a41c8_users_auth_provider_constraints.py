"""pin the users auth_provider vocabulary and the one-credential-store rule

Two CHECK constraints, both of which exist to make a security rule
unrepresentable rather than merely observed by `app/auth/router.py`:

`ck_users_auth_provider` closes the vocabulary to ('local', 'ad'). The login
route dispatches on this column to decide WHICH credential store to consult, so
an unrecognised value is not a cosmetic problem — it decides authentication. Same
rule as `ck_documents_status`: adding a provider means editing this CHECK.

`ck_users_credential` says a local user has a password hash and a directory user
has none. Without it an `ad` user who somehow acquired a `password_hash` would
hold a second way in that survives their AD account being disabled — the classic
directory-integration hole. `password_hash` stays NULLABLE; the constraint is
what makes the nullability provider-specific.

Neither is backfillable, and neither should be: if `ALTER TABLE` fails here, a row
already violates the rule and wants a human. Check before upgrading:

    SELECT id, email, auth_provider, (password_hash IS NULL) AS no_hash
    FROM users
    WHERE auth_provider NOT IN ('local','ad')
       OR (auth_provider = 'local' AND password_hash IS NULL)
       OR (auth_provider <> 'local' AND password_hash IS NOT NULL);

Every row created before this migration came from `POST /auth/register`, which
always wrote 'local' plus a hash, so the expected result is zero rows.

Revision ID: b7e3d95a41c8
Revises: d4a91f2c7b3e
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op

revision: str = "b7e3d95a41c8"
down_revision: Union[str, None] = "d4a91f2c7b3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_users_auth_provider",
        "users",
        "auth_provider IN ('local', 'ad')",
    )
    op.create_check_constraint(
        "ck_users_credential",
        "users",
        "(auth_provider = 'local' AND password_hash IS NOT NULL)"
        " OR (auth_provider <> 'local' AND password_hash IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_credential", "users", type_="check")
    op.drop_constraint("ck_users_auth_provider", "users", type_="check")
