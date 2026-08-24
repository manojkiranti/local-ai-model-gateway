"""The `user_mcp_grants` table: which MCP grants a user holds.

Deliberately separate from `users.role`. A global gateway admin holds NO grant
implicitly (design §3.4) — admin confers the ability to GRANT, not the grants
themselves — because gateway admin is an IT/ops role and auto-conferring
`Salary_Level` plus a SQL console over the expenses database on whoever operates
the gateway is precisely the quiet escalation a bank audit objects to. This
departs from `permissions.effective_level(is_global_admin=True) -> owner` on
purpose; do not "fix" it back.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base
from .grants import ALL_GRANTS

# Rendered into the CHECK below and into the migration. Sorted so a future
# autogenerate run does not propose a spurious diff.
_VOCABULARY = ", ".join(f"'{key}'" for key in sorted(ALL_GRANTS))


class UserMcpGrant(Base):
    __tablename__ = "user_mcp_grants"
    __table_args__ = (
        # Closes the vocabulary, the same way ck_user_departments_role and
        # ck_documents_status do. The CHECK stops a typo being STORED;
        # grants.ALL_GRANTS stops one being HONOURED. Adding a grant means
        # editing both, plus the MCP server's own copy.
        CheckConstraint(
            f"grant_key IN ({_VOCABULARY})",
            name="ck_user_mcp_grants_key",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    grant_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # SET NULL, not CASCADE: the audit fact outlives the admin who granted it.
    granted_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
