"""Generated-file storage: a per-turn "sink" plus an in-memory fallback.

Files are addressed by an unguessable UUID, not a caller-supplied path, and are
served only through GET /v1/files/{id} (never a public static mount).

Tools call `await file_store.save(...)` and never see the caller. The caller is
threaded in via a **contextvar sink** set for the duration of a chat turn:

- During a turn the chat router installs a `PostgresFileSink(user_id, session_id)`
  (see `sink.py`) with `file_sink(...)`. `save` delegates to it, so the file is
  written under the user's folder AND a durable `generated_files` row is written.
- With no sink installed (calls outside a turn, and offline tool tests), `save`
  falls back to a flat on-disk write + a per-process in-memory index — exactly
  the old behaviour, so tool unit tests need no database.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol
from uuid import uuid4

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HTML_MEDIA_TYPE = "text/html; charset=utf-8"
SVG_MEDIA_TYPE = "image/svg+xml"
PDF_MEDIA_TYPE = "application/pdf"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass
class FileRecord:
    id: str
    path: str
    filename: str  # original/display name, used for the download filename
    media_type: str
    size: int
    created_at: str


class FileSink(Protocol):
    """Something that can persist a generated file and return its record."""

    async def save(self, data: bytes, *, filename: str, media_type: str) -> FileRecord: ...


# The sink in effect for the current turn (None -> use the in-memory fallback).
_current_sink: ContextVar[FileSink | None] = ContextVar("current_file_sink", default=None)


@contextmanager
def file_sink(sink: FileSink) -> Iterator[None]:
    """Install `sink` as the active file sink for the enclosed block.

    Must wrap the code that actually runs the tools (the agent loop). For the
    streaming path that means setting it INSIDE the async generator Starlette
    iterates, not merely in the router before returning the StreamingResponse.
    """
    token = _current_sink.set(sink)
    try:
        yield
    finally:
        _current_sink.reset(token)


class FileStore:
    """In-memory + on-disk fallback sink, and holder of the base directory."""

    def __init__(self) -> None:
        self._dir: Path | None = None
        self._records: dict[str, FileRecord] = {}

    def configure(self, directory: str) -> None:
        """Set (and create) the on-disk base directory. Called at app startup."""
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        if self._dir is None:
            raise RuntimeError("FileStore is not configured (call configure() first).")
        return self._dir

    async def save(self, data: bytes, *, filename: str, media_type: str) -> FileRecord:
        """Persist a file. Delegates to the active turn sink if one is installed,
        else does a flat on-disk write tracked in the per-process index."""
        sink = _current_sink.get()
        if sink is not None:
            return await sink.save(data, filename=filename, media_type=media_type)
        return self._save_local(data, filename=filename, media_type=media_type)

    def _save_local(self, data: bytes, *, filename: str, media_type: str) -> FileRecord:
        file_id = uuid4().hex
        # On-disk name is the UUID + original extension; the caller-supplied
        # filename is NOT used to build the path (no traversal from user input).
        ext = Path(filename).suffix or ".bin"
        path = self.base_dir / f"{file_id}{ext}"
        path.write_bytes(data)
        record = FileRecord(
            id=file_id,
            path=str(path),
            filename=filename,
            media_type=media_type,
            size=len(data),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._records[file_id] = record
        return record

    def get(self, file_id: str) -> FileRecord | None:
        """Per-process in-memory lookup (fallback path / offline tests only).

        The authenticated download route uses the Postgres index, not this."""
        return self._records.get(file_id)


# Module-level singleton: the fallback sink and the base-dir holder.
file_store = FileStore()
