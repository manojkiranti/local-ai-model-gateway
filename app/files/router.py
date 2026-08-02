"""GET /v1/files/{file_id} — download a generated file by UUID (authed).

Explicit capability lookup: the id is resolved against the in-memory store and
the file streamed from the record's stored path. The raw id never builds a
filesystem path and the dir isn't a static mount, so no enumeration/traversal.

Auth note: this is behind JWT like everything else. A browser <a href> can't
send an Authorization header, so the frontend fetches this with the bearer token
and turns the response into a blob URL for download.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..auth.dependencies import get_current_user
from ..users.models import User
from .store import file_store

router = APIRouter(prefix="/v1", tags=["files"])


@router.get(
    "/files/{file_id}",
    summary="Download a generated file by UUID (authenticated)",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "The file."},
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "Unknown file id."},
        410: {"description": "File no longer available on disk."},
    },
)
async def get_file(file_id: str, _user: User = Depends(get_current_user)):
    record = file_store.get(file_id)
    if record is None:
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
