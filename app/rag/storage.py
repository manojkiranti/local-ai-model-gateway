"""On-disk layout for department corpus documents.

`documents.storage_key` is a RELATIVE key like `hr/9f3c....pdf`, never an
absolute path (unlike `generated_files.path`). Rows stay portable across hosts
and the same value becomes the bucket key if this moves to object storage.

Resolution is traversal-safe: a key is only ever joined to the configured base
and then checked to still be inside it. The key is minted by us, but it round
trips through the database, so it is treated as untrusted on the way back.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4


class StorageError(Exception):
    """A key that does not resolve to a location inside the base directory."""


_SLUG = re.compile(r"[^a-z0-9._-]+")


def _slug(value: str) -> str:
    cleaned = _SLUG.sub("-", value.strip().lower()).strip(".-")
    return cleaned or "misc"


def mint_storage_key(department_code: str, filename: str) -> str:
    """A fresh relative key: `<dept>/<uuid><ext>`.

    The caller-supplied filename contributes ONLY its extension — the on-disk
    name is a uuid, exactly like the generated-file store, so a hostile name
    cannot traverse or collide.
    """
    ext = Path(filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext or ""):
        ext = ".bin"
    return f"{_slug(department_code)}/{uuid4().hex}{ext}"


def resolve_storage_path(storage_key: str, base_dir: str) -> Path:
    """Absolute path for a key, refusing anything outside `base_dir`."""
    base = Path(base_dir).resolve()
    candidate = (base / storage_key).resolve()
    if candidate != base and base not in candidate.parents:
        raise StorageError(f"storage key escapes the base directory: {storage_key!r}")
    return candidate


def write_document(data: bytes, storage_key: str, base_dir: str) -> None:
    """Persist bytes at `storage_key`, creating parent directories."""
    path = resolve_storage_path(storage_key, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def delete_document(storage_key: str, base_dir: str) -> bool:
    """Remove a stored file. True if something was deleted.

    This is **compensation**: the upload routes write the file before the
    database work is known to succeed, so a duplicate-content 409 or a failed
    commit would otherwise leak an orphan. It therefore must not raise on a
    missing file or a directory — an exception here would mask the original
    error it is cleaning up after. A traversal attempt still raises, because
    deleting outside the base directory is never compensation.
    """
    path = resolve_storage_path(storage_key, base_dir)  # raises on traversal
    try:
        path.unlink()
        return True
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return False
