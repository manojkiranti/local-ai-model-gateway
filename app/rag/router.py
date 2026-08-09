"""Department administration.

Creating departments and granting access are admin-only. The one route open to
every authenticated caller is `GET /v1/departments`, which returns *that
caller's* departments — granted and active — because it is what the frontend
renders as tabs. A member must not be able to enumerate departments they cannot
use.
"""

from __future__ import annotations

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_admin
from ..config import get_settings
from ..db.session import get_session
from ..users.models import ROLE_ADMIN, User
from . import documents as docs_repo
from . import jobs as jobs_repo
from . import repository as repo
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
from .storage import delete_document, mint_storage_key, write_document

router = APIRouter(prefix="/v1/departments", tags=["departments"])


async def _require_department(session: AsyncSession, code: str):
    dept = await repo.get_department_by_code(session, code)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
        )
    return dept


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
    return DepartmentOut.model_validate(dept)


@router.get("", response_model=list[DepartmentOut])
async def list_departments(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DepartmentOut]:
    """Admins see every department; everyone else sees only their own tabs."""
    if user.role == ROLE_ADMIN:
        rows = await repo.list_departments(session)
    else:
        rows = await repo.list_departments_for_user(session, user.id)
    return [DepartmentOut.model_validate(d) for d in rows]


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
    return DepartmentOut.model_validate(dept)


@router.get("/{code}/members", response_model=list[MemberOut])
async def list_members(
    code: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    dept = await _require_department(session, code)
    rows = await repo.list_department_members(session, dept.id)
    return [MemberOut.model_validate(m) for m in rows]


@router.post("/{code}/members", status_code=status.HTTP_204_NO_CONTENT)
async def grant_member(
    code: str,
    body: GrantCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept = await _require_department(session, code)
    try:
        await repo.grant_department(
            session, user_id=body.user_id, department_id=dept.id,
            granted_by=admin.id,
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
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept = await _require_department(session, code)
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


async def _require_department_access(session: AsyncSession, user: User, code: str):
    """Read access to a department's document list: admin, or a grant."""
    dept = await _require_active_department(session, code)
    if user.role != ROLE_ADMIN:
        allowed = await repo.has_department_access(
            session, user_id=user.id, department_id=dept.id
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this department",
            )
    return dept


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
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    settings = get_settings()
    dept = await _require_active_department(session, code)

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
    write_document(data, storage_key, settings.rag_docs_dir)

    try:
        doc = await docs_repo.create_document(
            session, department_id=dept.id, title=title, source="upload",
            file_type=file_type, content_hash=docs_repo.content_hash_of(data),
            storage_key=storage_key, file_name=file.filename,
            uploaded_by=admin.id,
        )
    except docs_repo.DocumentConflict as exc:
        # Compensate: the bytes are already on disk and nothing will reference
        # them now. A duplicate upload must not leak a file.
        delete_document(storage_key, settings.rag_docs_dir)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, settings.rag_docs_dir)
        raise

    return await _accept(
        session, doc, storage_key=storage_key, docs_dir=settings.rag_docs_dir
    )


@router.post(
    "/{code}/documents/text",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_text_document(
    code: str,
    body: TextDocumentCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    """Typed-in knowledge: source='manual', no file_name, no storage_key file."""
    settings = get_settings()
    dept = await _require_active_department(session, code)

    content = body.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="content is empty"
        )

    # Stored as a .txt under the same tree so the worker has one read path.
    storage_key = mint_storage_key(dept.code, "typed.txt")
    data = content.encode("utf-8")
    write_document(data, storage_key, settings.rag_docs_dir)

    try:
        doc = await docs_repo.create_document(
            session, department_id=dept.id, title=body.title, source="manual",
            file_type="text", content_hash=docs_repo.content_hash_of(data),
            storage_key=storage_key, file_name=None, uploaded_by=admin.id,
        )
    except docs_repo.DocumentConflict as exc:
        delete_document(storage_key, settings.rag_docs_dir)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, settings.rag_docs_dir)
        raise

    return await _accept(
        session, doc, storage_key=storage_key, docs_dir=settings.rag_docs_dir
    )


@router.get("/{code}/documents", response_model=None)
async def list_department_documents(
    code: str,
    include_archived: bool = Query(False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Admins manage; members browse.

    A member sees only `ready` documents — a `pending` or `failed` one is not
    part of the corpus their answers can cite, and surfacing it just invites
    "why can't the assistant see this?". Admins see every non-archived document
    because managing failures is exactly their job, plus `?include_archived=`.

    `response_model=None` because the two roles genuinely return different
    shapes; FastAPI serializes whichever model is returned.
    """
    dept = await _require_department_access(session, user, code)

    if user.role != ROLE_ADMIN:
        if include_archived:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can list archived documents",
            )
        rows = await docs_repo.list_documents(session, dept.id, ready_only=True)
        return [DocumentOut.model_validate(d) for d in rows]

    rows = await docs_repo.list_documents(
        session, dept.id, include_archived=include_archived
    )
    return [DocumentAdminOut.model_validate(d) for d in rows]


@router.delete(
    "/{code}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def archive_department_document(
    code: str,
    document_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Archive: chunks removed so it stops being retrievable, row retained for
    audit. Not a delete — `documents.chunk_count` stays as the record."""
    dept = await _require_active_department(session, code)
    doc = await docs_repo.get_document(session, document_id)
    if doc is None or doc.department_id != dept.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        )
    await docs_repo.archive_document(session, document_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
