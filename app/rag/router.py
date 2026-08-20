"""Department administration.

Creating departments and granting access are admin-only. The one route open to
every authenticated caller is `GET /v1/departments`, which returns *that
caller's* departments — granted and active — because it is what the frontend
renders as tabs. A member must not be able to enumerate departments they cannot
use.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_admin
from ..config import get_settings
from ..db.session import get_session
from ..users import repository as users_repo
from ..users.models import ROLE_ADMIN, User
from . import access, permissions
from . import documents as docs_repo
from . import jobs as jobs_repo
from . import repository as repo
from .models import STATUS_READY
from .permissions import LEVEL_EDITOR, LEVEL_OWNER, LEVEL_VIEWER
from .parsing import ParseError, detect_file_type
from .schemas import (
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    DocumentAdminOut,
    DocumentOut,
    GrantCreate,
    IngestAccepted,
    MemberOut,
    TextDocumentCreate,
)
from .storage import (
    StorageError,
    delete_document,
    mint_storage_key,
    resolve_storage_path,
    write_document,
)

router = APIRouter(prefix="/v1/departments", tags=["departments"])


async def _require_department(session: AsyncSession, code: str):
    dept = await repo.get_department_by_code(session, code)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
        )
    return dept


def _department_out(dept, role: str | None) -> DepartmentOut:
    """`role` is the CALLER's effective level, which is not on the ORM row."""
    return DepartmentOut(
        id=dept.id,
        code=dept.code,
        name=dept.name,
        is_active=dept.is_active,
        created_at=dept.created_at,
        role=role,
    )


@router.post("", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_department(
    body: DepartmentCreate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DepartmentOut:
    try:
        dept = await repo.create_department(session, code=body.code, name=body.name)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Department '{body.code}' already exists",
        )
    # The caller is a global admin, so owner is their effective level here.
    return _department_out(dept, LEVEL_OWNER)


@router.get("", response_model=list[DepartmentOut])
async def list_departments(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DepartmentOut]:
    """Admins see every department; everyone else sees only their own tabs.

    Each row carries the caller's OWN level, so the frontend decides what to draw
    from one field instead of reimplementing the policy against /users/me.
    """
    if user.role == ROLE_ADMIN:
        rows = await repo.list_departments(session)
        return [_department_out(d, LEVEL_OWNER) for d in rows]
    granted = await repo.list_departments_for_user(session, user.id)
    return [_department_out(d, level) for d, level in granted]


@router.patch("/{code}", response_model=DepartmentOut)
async def update_department(
    code: str,
    body: DepartmentUpdate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DepartmentOut:
    dept = await _require_department(session, code)
    if body.name is not None:
        dept.name = body.name
    if body.is_active is not None:
        # Soft-disable is the only retirement path: documents and chat_sessions
        # reference departments with ON DELETE RESTRICT.
        dept.is_active = body.is_active
    await session.commit()
    await session.refresh(dept)
    # The caller is a global admin, so owner is their effective level here.
    return _department_out(dept, LEVEL_OWNER)


@router.get("/{code}/members", response_model=list[MemberOut])
async def list_members(
    code: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    """Owner-visible, INCLUDING other owners: `grant_refusal` restricts writes,
    not reads, and hiding owners would leave an owner unable to see why a revoke
    was refused."""
    dept, _level = await _require_level(session, user, code, LEVEL_OWNER)
    rows = await repo.list_department_members(session, dept.id)
    return [MemberOut.model_validate(m) for m in rows]


@router.post("/{code}/members", status_code=status.HTTP_204_NO_CONTENT)
async def grant_member(
    code: str,
    body: GrantCreate,
    caller: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Grant access at a level, or change an existing member's level.

    An owner delegates viewer and editor; only a global admin mints owners. The
    guard is `permissions.grant_refusal`, which takes `caller_is_global_admin`
    SEPARATELY from the level for the reason documented there.
    """
    dept, caller_level = await _require_level(session, caller, code, LEVEL_OWNER)

    user_id = body.user_id
    if user_id is None:
        # Granting by email: resolve it here rather than trusting the client to
        # have looked the id up correctly. Same 404 as an unknown id, so the two
        # spellings of "that user does not exist" read identically. This path is
        # also what lets an OWNER grant at all — `GET /users` is global-admin-only,
        # so they cannot resolve an address to an id themselves.
        user = await users_repo.get_by_email(session, body.email.lower())
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
            )
        user_id = user.id

    existing = await repo.get_department_level(
        session, user_id=user_id, department_id=dept.id
    )
    refusal = permissions.grant_refusal(
        caller_level=caller_level,
        caller_is_global_admin=caller.role == ROLE_ADMIN,
        requested_level=body.role,
        existing_target_level=existing,
    )
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

    try:
        await repo.grant_department(
            session, user_id=user_id, department_id=dept.id,
            granted_by=caller.id, role=body.role,
        )
        await session.commit()
    except IntegrityError:
        # Unknown user_id -> FK violation.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{code}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_member(
    code: str,
    user_id: int,
    caller: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept, caller_level = await _require_level(session, caller, code, LEVEL_OWNER)
    existing = await repo.get_department_level(
        session, user_id=user_id, department_id=dept.id
    )
    # `requested_level=None` is revocation. An owner may not evict another owner:
    # that is the lateral case, and it needs no last-owner guard because global
    # admin is always the backstop.
    refusal = permissions.grant_refusal(
        caller_level=caller_level,
        caller_is_global_admin=caller.role == ROLE_ADMIN,
        requested_level=None,
        existing_target_level=existing,
    )
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

    removed = await repo.revoke_department(
        session, user_id=user_id, department_id=dept.id
    )
    await session.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such grant"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Corpus documents (admin uploads; the API never parses or embeds — the worker
# does, so Docling must never load in this process).
# --------------------------------------------------------------------------- #
async def _require_active_department(session: AsyncSession, code: str):
    """Corpus operations reject an INACTIVE department, not just an unknown one.

    404 rather than 403, and for admins too — matching `access.resolve_department`
    in slice 1. A soft-disabled department is gone from the product; ingesting
    into it or listing it would contradict that.
    """
    dept = await _require_department(session, code)
    if not dept.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
        )
    return dept


async def _require_level(
    session: AsyncSession, user: User, code: str, minimum: str
):
    """The active department plus the caller's level, or the right refusal.

    The order matters and is the order slice 1 established: 404 for unknown or
    inactive FIRST (admins included — a soft-disabled department is gone from the
    product, and 403 would confirm it still exists), then 403 for no grant, then
    403 naming the level required. Naming it is safe here because the caller holds
    a grant, so the department's existence is not the secret; where existence IS
    the secret — a document, an ingest job — the answer is 404 instead.
    """
    dept = await _require_active_department(session, code)
    level = await access.effective_department_level(session, user, dept)
    if level is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this department",
        )
    if not permissions.allows(level, minimum):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=permissions.insufficient_level(minimum),
        )
    return dept, level


async def _accept(
    session: AsyncSession, doc, *, storage_key: str, docs_dir: str
) -> IngestAccepted:
    """Queue the ingest and return 202's body.

    Compensates the stored file if queuing or committing fails: the bytes were
    written before the transaction was known to succeed, so without this a
    failed enqueue leaves an orphan on disk that nothing will ever reference.
    """
    try:
        job = await jobs_repo.enqueue(session, document_id=doc.id)
        await session.commit()
    except jobs_repo.JobConflict as exc:
        delete_document(storage_key, docs_dir)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, docs_dir)
        raise
    return IngestAccepted(document_id=doc.id, job_id=job.id, status=job.status)


@router.post(
    "/{code}/documents",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    code: str,
    title: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    settings = get_settings()
    dept, _level = await _require_level(session, user, code, LEVEL_EDITOR)

    try:
        file_type = detect_file_type(file.filename or "")
    except ParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {settings.upload_max_bytes} bytes",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="file is empty"
        )

    storage_key = mint_storage_key(dept.code, file.filename or "document")
    write_document(data, storage_key, settings.rag_docs_base)

    try:
        doc = await docs_repo.create_document(
            session, department_id=dept.id, title=title, source="upload",
            file_type=file_type, content_hash=docs_repo.content_hash_of(data),
            storage_key=storage_key, file_name=file.filename,
            uploaded_by=user.id,
        )
    except docs_repo.DocumentConflict as exc:
        # Compensate: the bytes are already on disk and nothing will reference
        # them now. A duplicate upload must not leak a file.
        delete_document(storage_key, settings.rag_docs_base)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, settings.rag_docs_base)
        raise

    return await _accept(
        session, doc, storage_key=storage_key, docs_dir=settings.rag_docs_base
    )


@router.post(
    "/{code}/documents/text",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_text_document(
    code: str,
    body: TextDocumentCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    """Typed-in knowledge: source='manual', no file_name, no storage_key file."""
    settings = get_settings()
    dept, _level = await _require_level(session, user, code, LEVEL_EDITOR)

    content = body.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="content is empty"
        )

    # Stored as a .txt under the same tree so the worker has one read path.
    storage_key = mint_storage_key(dept.code, "typed.txt")
    data = content.encode("utf-8")
    write_document(data, storage_key, settings.rag_docs_base)

    try:
        doc = await docs_repo.create_document(
            session, department_id=dept.id, title=body.title, source="manual",
            file_type="text", content_hash=docs_repo.content_hash_of(data),
            storage_key=storage_key, file_name=None, uploaded_by=user.id,
        )
    except docs_repo.DocumentConflict as exc:
        delete_document(storage_key, settings.rag_docs_base)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, settings.rag_docs_base)
        raise

    return await _accept(
        session, doc, storage_key=storage_key, docs_dir=settings.rag_docs_base
    )


@router.get("/{code}/documents", response_model=None)
async def list_department_documents(
    code: str,
    include_archived: bool = Query(False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Editors manage; viewers browse.

    A viewer sees only `ready` documents — a `pending` or `failed` one is not
    part of the corpus their answers can cite, and surfacing it just invites
    "why can't the assistant see this?". Editors see every non-archived document
    because managing failures is exactly their job, plus `?include_archived=`.

    `response_model=None` because the two levels genuinely return different
    shapes; FastAPI serializes whichever model is returned.
    """
    dept, level = await _require_level(session, user, code, LEVEL_VIEWER)

    if not permissions.allows(level, LEVEL_EDITOR):
        if include_archived:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=permissions.insufficient_level(LEVEL_EDITOR),
            )
        rows = await docs_repo.list_documents(session, dept.id, ready_only=True)
        return [DocumentOut.model_validate(d) for d in rows]

    rows = await docs_repo.list_documents(
        session, dept.id, include_archived=include_archived
    )
    return [DocumentAdminOut.model_validate(d) for d in rows]


# Content types for the corpus's closed `file_type` vocabulary (see
# parsing._EXT_MAP). Anything unrecognised downloads as a binary blob rather
# than being guessed at — a wrong type invites the browser to render it inline.
_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "text": "text/plain; charset=utf-8",
}

# Control characters have no place in a filename and are the classic vector for
# splitting a response header. Starlette percent-encodes via `filename*`, so this
# is defence in depth rather than the only guard.
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f]")


def _download_filename(doc) -> str:
    """What the browser should save the file as.

    Typed-in documents (`source='manual'`) have no `file_name` — their bytes are
    stored under a minted `.txt` key — so the admin-supplied title becomes the
    name. The title is arbitrary text up to 512 chars, hence the sanitising and
    the length cap.
    """
    if doc.file_name:
        return _UNSAFE_FILENAME.sub("", doc.file_name).strip() or "document"
    base = _UNSAFE_FILENAME.sub("", doc.title or "").strip()
    base = base[:120].rstrip(" .") or "document"
    return f"{base}.txt"


def _document_path(doc, settings) -> Path:
    """Where this document's bytes actually live.

    Two trees, because §28 removed the per-corpus copy. An NRB document's bytes
    exist once, content-addressed under `NRB_FILES_DIR`; everything else lives
    under `RAG_DOCS_DIR`. The NRB key is RECONSTRUCTED from the content hash
    rather than read from `storage_key`, exactly as `worker._document_path` does:
    a row minted under the old copy scheme carries a `RAG_DOCS_DIR`-style key that
    no longer points at anything, and following it would 404 a document that is
    present on disk. `metadata.blob_sha256` is the same digest again, kept as the
    fallback for a row whose `content_hash` was never backfilled.

    The `app.nrb` import is local so the module graph stays honest about what the
    API loads; `filestore` is stdlib + config only and pulls in no worker
    dependency (no docling, no torch, no OCR stack).
    """
    if (doc.meta or {}).get("origin") == "nrb":
        from ..nrb import filestore

        digest = doc.content_hash or str((doc.meta or {}).get("blob_sha256") or "")
        return filestore.resolve_path(
            filestore.storage_key_for(digest, doc.file_type)
        )
    if not doc.storage_key:
        raise StorageError(f"document {getattr(doc, 'id', '?')} has no storage_key")
    return resolve_storage_path(doc.storage_key, settings.rag_docs_base)


@router.get(
    "/{code}/documents/{document_id}/download",
    response_class=FileResponse,
    responses={
        403: {"description": "You have no grant for this department."},
        404: {"description": "Unknown department/document, or not readable by you."},
    },
)
async def download_department_document(
    code: str,
    document_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve a corpus document's original bytes — what a chat citation links to.

    Status codes follow the routes beside this one rather than one blanket rule:
    an ungranted **department** is 403 (as in `_require_level`, and as
    `GET /{code}/documents` already answers), while anything at **document**
    granularity is 404 — unknown id, a document belonging to another department,
    or one a viewer is not allowed to read. Viewers are held to `ready`
    documents, matching the list route: a pending or archived document is not
    part of the corpus their answers can cite, and distinguishing "exists but
    you may not have it" from "does not exist" would leak the corpus's shape.

    Behind JWT like every other download here, so the frontend must fetch with
    the Authorization header and build a blob URL.
    """
    dept, level = await _require_level(session, user, code, LEVEL_VIEWER)
    doc = await docs_repo.get_document(session, document_id)
    if doc is None or doc.department_id != dept.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        )
    if not permissions.allows(level, LEVEL_EDITOR) and doc.status != STATUS_READY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        )
    settings = get_settings()
    try:
        path = _document_path(doc, settings)
    except Exception as exc:  # StorageError, FileStoreError, or an unusable digest
        # Every key here is ours, but each round-trips through the database, so on
        # the way back it is untrusted: a traversal attempt, a malformed hash or a
        # row with no bytes at all is a 404 to the caller and never a served file.
        # (A missing storage_key used to be checked separately; an NRB row is
        # legitimately without a usable one, so the check moved in here.)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        ) from exc
    if not path.is_file():
        # Row without bytes: the file was removed out of band. Not a 500 — there
        # is genuinely nothing to serve.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document file is missing"
        )

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(doc.file_type, "application/octet-stream"),
        filename=_download_filename(doc),
    )


@router.delete(
    "/{code}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def archive_department_document(
    code: str,
    document_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Archive: chunks removed so it stops being retrievable, row retained for
    audit. Not a delete — `documents.chunk_count` stays as the record."""
    dept, _level = await _require_level(session, user, code, LEVEL_EDITOR)
    doc = await docs_repo.get_document(session, document_id)
    if doc is None or doc.department_id != dept.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        )
    await docs_repo.archive_document(session, document_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
