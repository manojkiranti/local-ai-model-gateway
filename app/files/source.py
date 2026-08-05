"""Owner-scoped file resolver used by the read tools during a chat turn.

Symmetric to `sink.py`: the read tools (`inspect_excel`/`read_excel`) call
`await resolve_file(file_id)` and never see the caller. During a turn the chat
router installs a `PostgresFileSource(user_id)` via `file_source(...)`; it looks
the id up in `generated_files` and returns a `FileRecord` ONLY when the row
belongs to this user. A foreign or unknown id resolves to None -> the tool
returns a friendly `ERROR: no such file`, never another user's data.

`turn_files(user_id, session_id)` bundles the sink + source so the chat router
installs both with one `with`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from ..db.session import SessionLocal
from . import repository as repo
from .sink import PostgresFileSink
from .store import FileRecord, file_sink, file_source


class PostgresFileSource:
    def __init__(self, *, user_id: int) -> None:
        self.user_id = user_id

    async def resolve(self, file_id: str) -> FileRecord | None:
        async with SessionLocal() as session:
            row = await repo.get_owned_file(session, file_id=file_id, user_id=self.user_id)
            if row is None:
                return None
            return FileRecord(
                id=row.id,
                path=row.path,
                filename=row.filename,
                media_type=row.media_type,
                size=row.size,
                created_at=row.created_at.isoformat(timespec="seconds"),
            )


@contextmanager
def turn_files(*, user_id: int, session_id: str | None) -> Iterator[None]:
    """Install BOTH the write sink and the owner-scoped read source for a turn.
    Must wrap the agent loop (inside the stream generator for streaming)."""
    with file_sink(PostgresFileSink(user_id=user_id, session_id=session_id)):
        with file_source(PostgresFileSource(user_id=user_id)):
            yield
