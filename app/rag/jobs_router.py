"""Ingest job progress. Separate router because the path is not under
/v1/departments — a job id is enough to identify the work."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..users.models import ROLE_ADMIN, User
from . import documents as docs_repo
from . import jobs as jobs_repo
from . import repository as repo
from .permissions import LEVEL_EDITOR, allows
from .schemas import IngestJobOut

router = APIRouter(prefix="/v1/ingest-jobs", tags=["departments"])


@router.get("/{job_id}", response_model=IngestJobOut)
async def get_ingest_job(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IngestJobOut:
    """Editor of the job's department, or a global admin.

    Not a nicety: `POST /v1/departments/{code}/documents` hands the uploader a
    `job_id`, so gating this on global admin shipped the feature broken — an
    editor could upload and then be refused progress on their own upload.

    Every refusal here is **404**, never 403. A job id maps to a document, so
    confirming that this one exists would leak the corpus's shape to someone who
    cannot see it — the same rule the download route follows at document
    granularity.
    """
    job = await jobs_repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown ingest job"
        )

    if user.role != ROLE_ADMIN:
        doc = await docs_repo.get_document(session, job.document_id)
        level = None
        if doc is not None:
            # Deliberately NOT gated on the department being active: this job is
            # the record of work on a document this editor owns, and a department
            # soft-disabled mid-ingest should not turn their progress view into a
            # 404 they cannot interpret.
            level = await repo.get_department_level(
                session, user_id=user.id, department_id=doc.department_id
            )
        if not allows(level, LEVEL_EDITOR):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown ingest job"
            )

    return IngestJobOut.model_validate(job)
