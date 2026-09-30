"""Generated- and uploaded-file routes (authed):
  POST /v1/files       — upload a file the model can read (spreadsheet or document)
  GET  /v1/files       — the caller's files, newest first (the "my files" list)
  GET  /v1/files/{id}  — download one file the caller owns
  GET  /v1/files/{id}/preview — structured preview content behind a generated file
  DELETE /v1/files/{id} — delete one file the caller owns
  GET  /v1/branding/pptx-cover, /v1/branding/pptx-header — the org's pptx
       template artwork (shared, not per-user), for the deck preview panel

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
from ..tools.local import memo as memo_tool
from ..tools.local import pptx as pptx_tool
from . import ingest, pdf_convert, readers, repository as repo
from .store import file_store

router = APIRouter(prefix="/v1", tags=["files"])

_CHUNK = 64 * 1024


@router.get(
    "/branding/pptx-cover",
    summary="The org's PPTX template cover background, for the deck preview panel",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The cover background image."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "No branded template is configured on this deployment, or it has no cover slide."},
    },
)
async def get_pptx_cover_background(user: User = Depends(get_current_user)):
    # Read fresh from the template file on every call (via a thread — this is
    # sync python-pptx/zip/XML work) rather than serving a pre-extracted copy,
    # so swapping the template file (see app/tools/local/pptx.py) updates this
    # preview background with no separate step. Not cached here; the
    # Cache-Control header still lets the browser skip repeat fetches.
    data = await asyncio.to_thread(pptx_tool.cover_background_bytes)
    if data is None:
        raise HTTPException(status_code=404, detail="no branded template configured")
    return Response(
        content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"}
    )


@router.get(
    "/branding/pptx-header",
    summary="The org's PPTX template content-slide background, for the deck preview panel",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The content-slide background image."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "No branded template is configured on this deployment."},
    },
)
async def get_pptx_header_background(user: User = Depends(get_current_user)):
    data = await asyncio.to_thread(pptx_tool.header_background_bytes)
    if data is None:
        raise HTTPException(status_code=404, detail="no branded template configured")
    return Response(
        content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"}
    )


@router.get(
    "/branding/memo-logo",
    summary="The logo printed on generated memos, for the memo preview panel",
    responses={
        200: {"content": {"image/png": {}}, "description": "The memo logo."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "No memo logo is configured on this deployment."},
    },
)
async def get_memo_logo(user: User = Depends(get_current_user)):
    data = memo_tool.logo_bytes()
    if data is None:
        raise HTTPException(status_code=404, detail="no memo logo configured")
    return Response(
        content=data, media_type="image/png", headers={"Cache-Control": "private, max-age=300"}
    )


class FileMeta(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int
    source: str
    created_at: datetime


class FilePreviewResponse(BaseModel):
    """The exact structured content create_pptx validated and rendered from —
    not a re-extraction of the saved bytes — so the frontend can draw each
    slide itself rather than parsing the binary file."""

    title: str
    subtitle: str
    slides: list[dict]


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
            "only .xlsx, .csv, .pdf, .docx, .pptx, .txt, .md, .json, .png, .jpg, "
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
    if ext in (".xlsx", ".docx", ".pptx"):
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


@router.get(
    "/files/{file_id}/preview",
    response_model=FilePreviewResponse,
    summary="Structured preview content behind a generated file the caller owns",
    responses={
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "Unknown file id, not owned by the caller, or no structured "
                              "preview was recorded for it (not every tool provides one)."},
    },
)
async def get_file_preview(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    record = await repo.get_owned_file(session, file_id=file_id, user_id=user.id)
    # Unknown id, not-owned, and no-preview-recorded all read as 404 — same
    # no-existence-oracle rule as the download route, plus: a file with no
    # preview is not a special case the client needs to distinguish from one
    # that was never generated at all.
    if record is None or record.preview is None:
        raise HTTPException(status_code=404, detail="no preview available for this file")
    return record.preview


# LibreOffice is CPU- and memory-heavy; a burst of previews must queue, not
# fork a dozen soffice processes.
_PDF_SLOTS = asyncio.Semaphore(2)


@router.get(
    "/files/{file_id}/pdf",
    summary="A PDF rendering of an Office file the caller owns, for the preview panel",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The file rendered as PDF."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "Unknown file id, or not owned by the caller."},
        410: {"description": "File no longer available on disk."},
        415: {"description": "Not an Office file (.docx/.pptx/.xlsx)."},
        422: {"description": "The file could not be converted."},
        503: {"description": "PDF conversion is not installed on this deployment."},
    },
)
async def get_file_pdf(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Convert (once, then cached) and serve a .docx/.pptx/.xlsx as a PDF, so
    the browser can show it inline exactly as it will print. Owner-scoped like
    the download: 404 for unknown and not-yours alike."""
    record = await repo.get_owned_file(session, file_id=file_id, user_id=user.id)
    if record is None:
        raise HTTPException(status_code=404, detail="file not found")
    source = Path(record.path)
    if source.suffix.lower() not in pdf_convert.CONVERTIBLE_EXTS:
        raise HTTPException(status_code=415, detail="only Office files can be previewed as PDF")
    if not source.exists():
        raise HTTPException(status_code=410, detail="file no longer available")
    if not pdf_convert.available():
        raise HTTPException(status_code=503, detail="PDF preview is not enabled on this deployment")
    async with _PDF_SLOTS:
        try:
            pdf = await asyncio.to_thread(pdf_convert.convert_to_pdf, source)
        except pdf_convert.ConvertError as exc:
            raise HTTPException(status_code=422, detail=f"could not convert this file: {exc}") from exc
    return FileResponse(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
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
    for leftover in (path, pdf_convert.cached_pdf_path(Path(path))):
        try:
            os.remove(leftover)
        except FileNotFoundError:
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
