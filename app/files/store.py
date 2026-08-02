"""A tiny capability-based file store for generated files (ported).

Files are addressed by an unguessable UUID, not a caller-supplied path, and are
served only through GET /v1/files/{id} (never a public static mount). The data
isn't public — knowing the UUID is the capability to download the file.

The index (UUID -> record) is in-memory: per-process, does not survive a
restart. Files stay on disk but become unindexed. Persist the index later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HTML_MEDIA_TYPE = "text/html; charset=utf-8"


@dataclass
class FileRecord:
    id: str
    path: str
    filename: str  # original/display name, used for the download filename
    media_type: str
    size: int
    created_at: str


class FileStore:
    def __init__(self) -> None:
        self._dir: Path | None = None
        self._records: dict[str, FileRecord] = {}

    def configure(self, directory: str) -> None:
        """Set (and create) the on-disk directory. Called at app startup."""
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, *, filename: str, media_type: str) -> FileRecord:
        if self._dir is None:
            raise RuntimeError("FileStore is not configured (call configure() first).")
        file_id = uuid4().hex
        # On-disk name is the UUID + original extension; the caller-supplied
        # filename is NOT used to build the path (no traversal from user input).
        ext = Path(filename).suffix or ".bin"
        path = self._dir / f"{file_id}{ext}"
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
        """Look up by id. Returns None for unknown ids (no path is built from
        the raw id, so this is safe against path traversal)."""
        return self._records.get(file_id)


# Module-level singleton shared by the create_excel tool and the files router.
file_store = FileStore()
