"""Generated- and uploaded-file routes (authed):
  POST /v1/files       — upload a file the model can read (spreadsheet or document)
  GET  /v1/files       — the caller's files, newest first (the "my files" list)
  GET  /v1/files/{id}  — download one file the caller owns
  DELETE /v1/files/{id} — delete one file the caller owns

Ownership is enforced from the Postgres `generated_files` index, not the raw id:
the id resolves to a row only when it belongs to the caller, so another user's
UUID reads as 404 (no enumeration/traversal — the on-disk path comes from the
row, never from the request).

Auth note: a browser <a href> can't send an Authorization header, so the
frontend fetches these with the bearer token and turns the response into a blob
URL for download / listing.
"""

import asyncio
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..config import get_settings
from ..db.session import get_session
from ..users.models import User
from . import ingest, readers, repository as repo
from .store import file_store

router = APIRouter(prefix="/v1", tags=["files"])

_CHUNK = 64 * 1024


class FileMeta(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int
    source: str
    created_at: datetime


class FileListResponse(BaseModel):
    files: list[FileMeta]


class UploadResponse(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int
    source: str
    summary: dict  # spreadsheet: {kind, sheets, total_rows} | document: {kind, lines, chars, pages, text_pages}


def _reject(path: Optional[Path], code: int, detail: str) -> HTTPException:
    """Unlink a partial upload (best effort) and build the HTTPException."""
    if path is not None:
        try:
            os.remove(path)
        except OSError:
            pass
    return HTTPException(status_code=code, detail=detail)


@router.post(
    "/files",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file the model can read (spreadsheet, document or image)",
    responses={
        400: {"description": "Bad extension, corrupt/encrypted file, zip-bomb, or pixel-bomb."},
        401: {"description": "Missing/invalid JWT."},
        413: {"description": "File exceeds the size limit."},
    },
)
async def upload_file(
    file: UploadFile,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    # 1) extension allowlist (cheap, before touching disk)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ingest.UPLOAD_TYPES:
        raise _reject(
            None,
            400,
            "only .xlsx, .csv, .pdf, .docx, .txt, .md, .json, .png, .jpg, "
            ".jpeg, .webp, .tif, .tiff and .bmp files are accepted",
        )

    # 2) stream to the owner's folder under a UUID name, counting bytes (413 cap)
    file_id = uuid4().hex
    user_dir = file_store.base_dir / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    dest = user_dir / f"{file_id}{ext}"
    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.upload_max_bytes:
                    raise _reject(
                        dest, 413,
                        f"file exceeds the {settings.upload_max_bytes // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)
    finally:
        await file.close()
    if size == 0:
        raise _reject(dest, 400, "uploaded file is empty")

    # 3) zip-bomb guard for the OOXML formats: refuse absurd expansion
    if ext in (".xlsx", ".docx"):
        try:
            with zipfile.ZipFile(dest) as zf:
                uncompressed = sum(i.file_size for i in zf.infolist())
        except zipfile.BadZipFile:
            raise _reject(dest, 400, f"file is not a valid {ext} document")
        if uncompressed > settings.upload_xlsx_max_uncompressed:
            raise _reject(dest, 400, "file expands too large to process safely")

    # 4) parse check + summary. Never evaluates formulas; never OCRs. A scanned
    # PDF passes here deliberately — it is a valid file, and read_document is
    # where the user is told it has no text layer. An IMAGE is summarised by its
    # dimensions for the same reason plus one more: this runs again on every turn
    # (history/service._resolve_attachments), so it must stay a header read —
    # image TEXT comes from the read_image tool. This is also where the
    # decoded-pixel cap and the image-format allowlist are enforced (see
    # images.summarize_image); the zip guard above cannot see either.
    # Bad file -> unlink + 400.
    try:
        summary = await asyncio.to_thread(ingest.summarize, dest)
    except readers.ReadError as exc:
        raise _reject(dest, 400, f"could not read the file ({exc})")

    # 5) durable owned row, source='uploaded'
    await repo.record_file(
        session,
        id=file_id,
        user_id=user.id,
        filename=file.filename,
        media_type=ingest.UPLOAD_TYPES[ext],
        size=size,
        path=str(dest),
        source="uploaded",
    )
    await session.commit()

    return UploadResponse(
        id=file_id,
        filename=file.filename,
        media_type=ingest.UPLOAD_TYPES[ext],
        size=size,
        source="uploaded",
        summary=summary.as_dict(),
    )


@router.get(
    "/files",
    response_model=FileListResponse,
    summary="List the caller's files (newest first; optional source filter)",
    responses={401: {"description": "Missing/invalid JWT."}},
)
async def list_files(
    source: Optional[str] = Query(
        None, description="Filter by origin: 'generated' or 'uploaded'."
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if source is not None and source not in ("generated", "uploaded"):
        raise HTTPException(status_code=400, detail="source must be 'generated' or 'uploaded'")
    rows = await repo.list_files(session, user_id=user.id, source=source)
    return FileListResponse(
        files=[
            FileMeta(
                id=r.id,
                filename=r.filename,
                media_type=r.media_type,
                size=r.size,
                source=r.source,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get(
    "/files/{file_id}",
    summary="Download a generated file the caller owns (authenticated)",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "The file."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "Unknown file id, or not owned by the caller."},
        410: {"description": "File no longer available on disk."},
    },
)
async def get_file(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    record = await repo.get_owned_file(session, file_id=file_id, user_id=user.id)
    if record is None:
        # Unknown id AND not-owned both surface as 404 (no existence oracle).
        raise HTTPException(status_code=404, detail="file not found")
    if not os.path.exists(record.path):
        raise HTTPException(status_code=410, detail="file no longer available")
    # FileResponse sets Content-Disposition: attachment (has a filename), so the
    # browser downloads rather than renders in our origin. nosniff stops content
    # sniffing/inline execution of model-generated HTML. Safe rendering is the
    # frontend's job (sandboxed <iframe srcdoc>, no allow-scripts).
    return FileResponse(
        record.path,
        media_type=record.media_type,
        filename=record.filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a generated file the caller owns",
    responses={
        204: {"description": "Deleted."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "Unknown file id, or not owned by the caller."},
    },
)
async def delete_file(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    path = await repo.delete_owned_file(session, file_id=file_id, user_id=user.id)
    if path is None:
        # Unknown id AND not-owned both surface as 404 (no existence oracle).
        raise HTTPException(status_code=404, detail="file not found")
    await session.commit()  # DB is the source of truth; drop the row first
    # Best-effort unlink of the on-disk file (a leftover file is harmless; a row
    # pointing at a missing file would only 410 on download).
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
