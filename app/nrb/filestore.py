"""On-disk layout for downloaded NRB files. Content-addressed.

`nrb_files.storage_key` is a RELATIVE key under `NRB_FILES_DIR`, the same
convention as `documents.storage_key` (see `app/rag/storage.py`): rows stay
portable across hosts and the key becomes the object-storage key later. A separate
tree from `RAG_DOCS_DIR` because these are not department corpus documents — they
are raw upstream artefacts, and only Phase 7 decides which of them becomes a
`documents` row.

**The key is the content hash, not the filename**, and that decision does three
things at once:

  * **Deduplication is free.** NRB republishes the same PDF under different URLs —
    Phase 3 measured 42 duplicate attachment *references*, and identical bytes
    under two different paths are a separate and larger class. Two rows with the
    same `content_sha256` share one blob, so 8.6 GB of reported size is an upper
    bound on the disk it takes.
  * **No Devanagari, and no attacker-chosen name, ever reaches the filesystem.**
    NRB's filenames are Nepali text with spaces, brackets and percent-escapes; a
    hash is 64 hex characters. `..` cannot appear in one.
  * **A re-download is verifiable.** The path *is* the checksum, so a corrupted
    blob is detectable without a second column.

Fanned out one byte deep (`ab/abcd…`) so no directory holds 18,000 entries.

Writes are atomic: bytes stream into `.incoming/<uuid>.part` and are then
`os.replace`d into place. That matters because the hash is only known *after* the
whole body has been read — the final name cannot be chosen up front — and because a
half-written blob under its own checksum's name would be a permanently wrong file.
`.incoming` sits inside the base directory so the rename never crosses a
filesystem, which is what makes it atomic rather than a copy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from ..config import get_settings

__all__ = [
    "FileStoreError",
    "base_dir",
    "delete_blob",
    "new_temp_path",
    "promote",
    "resolve_path",
    "storage_key_for",
]

# The subdirectory holding partial downloads. Inside the base directory on purpose
# (see the module docstring); dotted so it sorts and greps out of the way.
INCOMING = ".incoming"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")


class FileStoreError(Exception):
    """A key that does not resolve inside the base directory, or is malformed."""


def base_dir() -> Path:
    """The absolute root of the NRB blob tree.

    A relative `NRB_FILES_DIR` is anchored to the **repository root**, never the
    process working directory: the fetch command is run from wherever a developer
    happens to be standing, and two different CWDs must not produce two different
    corpora. An absolute setting is used verbatim.
    """
    configured = Path(get_settings().nrb_files_dir)
    if configured.is_absolute():
        return configured
    # app/nrb/filestore.py -> app/nrb -> app -> repo root
    return Path(__file__).resolve().parents[2] / configured


def storage_key_for(sha256: str, extension: str | None) -> str:
    """`<first two hex>/<full hash>.<ext>` — the relative key for these bytes.

    The extension is cosmetic (it makes a directory listing readable and lets a
    human open a blob); identity is the hash alone. A missing or implausible
    extension becomes `.bin` rather than being trusted, since it arrives from
    NRB's URL.
    """
    digest = (sha256 or "").strip().lower()
    if not _SHA256.match(digest):
        raise FileStoreError(f"not a sha256 hex digest: {sha256!r}")
    suffix = (extension or "").strip().lower().lstrip(".")
    if not _EXTENSION.match(suffix):
        suffix = "bin"
    return f"{digest[:2]}/{digest}.{suffix}"


def resolve_path(storage_key: str, base: Path | None = None) -> Path:
    """Absolute path for a key, refusing anything that escapes the base directory.

    We mint every key, but each one round-trips through the database, so on the way
    back it is treated as untrusted — exactly as `rag/storage.resolve_storage_path`
    does, and for the same reason.
    """
    root = (base or base_dir()).resolve()
    candidate = (root / storage_key).resolve()
    if candidate != root and root not in candidate.parents:
        raise FileStoreError(f"storage key escapes the base directory: {storage_key!r}")
    return candidate


def new_temp_path(base: Path | None = None) -> Path:
    """A fresh path under `.incoming/` to stream a download into."""
    root = base or base_dir()
    incoming = root / INCOMING
    incoming.mkdir(parents=True, exist_ok=True)
    return incoming / f"{uuid4().hex}.part"


def promote(temp_path: Path, storage_key: str, base: Path | None = None) -> bool:
    """Move a completed download to its content-addressed home.

    Returns **True if these bytes were new**, False if the blob already existed —
    which is the deduplication signal the caller counts, not an error. In the
    duplicate case the temp file is removed and the existing blob is left exactly
    as it is: it is already, by definition, the same bytes.
    """
    target = resolve_path(storage_key, base)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        temp_path.unlink(missing_ok=True)
        return False
    # Same filesystem (both under the base directory), so this is atomic.
    os.replace(temp_path, target)
    return True


def delete_blob(storage_key: str, base: Path | None = None) -> bool:
    """Remove a blob. True if something was deleted.

    Compensation, in the same sense as `rag/storage.delete_document`: the bytes
    land on disk before the row that points at them is committed, so a failed
    update would otherwise orphan a blob. It must not raise on a file that is
    already gone — an exception here would mask the original error being cleaned up
    after. A traversal attempt still raises, because deleting outside the base
    directory is never compensation.
    """
    path = resolve_path(storage_key, base)   # raises on traversal
    try:
        path.unlink()
        return True
    except OSError:
        return False
