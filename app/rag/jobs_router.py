"""Ingest job progress. Separate router because the path is not under
/v1/departments — a job id is enough to identify the work."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..users.models import User
from . import jobs as jobs_repo
from .schemas import IngestJobOut

router = APIRouter(prefix="/v1/ingest-jobs", tags=["departments"])


@router.get("/{job_id}", response_model=IngestJobOut)
async def get_ingest_job(
    job_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IngestJobOut:
    job = await jobs_repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown ingest job"
        )
    return IngestJobOut.model_validate(job)
