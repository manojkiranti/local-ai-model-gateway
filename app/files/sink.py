"""The Postgres-backed file sink used during an authenticated chat turn.

Installed via `file_sink(PostgresFileSink(user_id, session_id))` around the agent
loop. Each saved file is written under the owner's folder and recorded as a
durable `generated_files` row in its OWN committed transaction — decoupled from
the chat transaction on purpose, so a file the model produced survives even if
the turn later errors out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..db.session import SessionLocal
from . import repository as repo
from .store import FileRecord, file_store


class PostgresFileSink:
    def __init__(self, *, user_id: int, session_id: str | None = None) -> None:
        self.user_id = user_id
        self.session_id = session_id

    async def save(self, data: bytes, *, filename: str, media_type: str) -> FileRecord:
        file_id = uuid4().hex
        ext = Path(filename).suffix or ".bin"
        # Per-user subfolder; the UUID (not the caller filename) is the on-disk
        # name, so there's no path traversal from model-supplied input.
        user_dir = file_store.base_dir / str(self.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{file_id}{ext}"
        path.write_bytes(data)

        # Own short transaction -> the file row is durable regardless of the turn.
        async with SessionLocal() as session:
            await repo.record_file(
                session,
                id=file_id,
                user_id=self.user_id,
                session_id=self.session_id,
                filename=filename,
                media_type=media_type,
                size=len(data),
                path=str(path),
            )
            await session.commit()

        return FileRecord(
            id=file_id,
            path=str(path),
            filename=filename,
            media_type=media_type,
            size=len(data),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
