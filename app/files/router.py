"""Generated-file routes (authed):
  GET /v1/files        — the caller's files, newest first (the "my files" list)
  GET /v1/files/{id}   — download one file the caller owns

Ownership is enforced from the Postgres `generated_files` index, not the raw id:
the id resolves to a row only when it belongs to the caller, so another user's
UUID reads as 404 (no enumeration/traversal — the on-disk path comes from the
row, never from the request).

Auth note: a browser <a href> can't send an Authorization header, so the
frontend fetches these with the bearer token and turns the response into a blob
URL for download / listing.
"""

import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..users.models import User
from . import repository as repo

router = APIRouter(prefix="/v1", tags=["files"])


class FileMeta(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int
    created_at: datetime


class FileListResponse(BaseModel):
    files: list[FileMeta]


@router.get(
    "/files",
    response_model=FileListResponse,
    summary="List the caller's generated files (newest first)",
    responses={401: {"description": "Missing/invalid JWT."}},
)
async def list_files(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await repo.list_files(session, user_id=user.id)
    return FileListResponse(
        files=[
            FileMeta(
                id=r.id,
                filename=r.filename,
                media_type=r.media_type,
                size=r.size,
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
