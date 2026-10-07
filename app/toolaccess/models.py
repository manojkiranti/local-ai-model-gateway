"""The `user_tool_access` table: one row per admin override of a local tool.

A row exists only where an admin has decided something for that user; no row
means the default (`policy.default_allowed`). `allowed` records the decision in
either direction — a block of a starter tool, or a grant of a default-off one.

No CHECK on `tool_name`: the vocabulary is the code's own tool list, which
changes with each release; the admin route refuses an unknown name instead. An
override for a tool later removed from the gateway is simply never consulted.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base


class UserToolAccess(Base):
    __tablename__ = "user_tool_access"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tool_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # SET NULL, not CASCADE: the audit fact outlives the admin who set it.
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UserMcpLevel(Base):
    """One row per admin-set MCP access level (`mcp_policy`): a user's level for
    one MCP system. No row means `mcp_policy.DEFAULT_LEVEL`. Unlike tool names,
    systems and levels are the gateway's OWN vocabulary, so both are closed by
    CHECKs — the rule reads them, the same reason `ck_documents_status` exists."""

    __tablename__ = "user_mcp_levels"
    __table_args__ = (
        CheckConstraint("system IN ('hrms', 'izone', 'ems', 'other')", name="ck_user_mcp_levels_system"),
        CheckConstraint("level IN ('none', 'read', 'full')", name="ck_user_mcp_levels_level"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    system: Mapped[str] = mapped_column(String(16), primary_key=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UserMcpToolAccess(Base):
    """One row per admin override of a single MCP tool for one user — beats the
    user's level for that tool's system. No CHECK on `tool_name`: the vocabulary
    is the MCP server's live tool list, which the admin route validates against."""

    __tablename__ = "user_mcp_tool_access"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tool_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
