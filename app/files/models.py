"""ORM model for generated files.

Every file produced by a tool (create_excel/html/chart/pdf) gets one row here,
owned by the user whose turn produced it. This is the durable source of truth
for the "my files" list and for owner-scoped downloads — the old in-memory index
survived neither restarts nor cross-user scoping.

`id` is UUID-hex (like chat rows and the on-disk name): unguessable and a stable
render key for the frontend.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from ..db.base import Base


def _uuid_hex() -> str:
    return uuid4().hex


class GeneratedFile(Base):
    __tablename__ = "generated_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Which chat produced it (nullable — enables "files in this chat" later).
    # SET NULL so deleting a session doesn't delete the file rows.
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)  # on-disk location
    # How the file got here: model output ('generated') or a user upload
    # ('uploaded'). Lets GET /v1/files filter, and the read tools target uploads.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="generated"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
