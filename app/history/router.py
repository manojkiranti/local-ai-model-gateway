"""Read/manage a user's chat sessions (all authed, all scoped to the caller).

Ownership is enforced everywhere: a session that isn't yours reads as 404 (we
never confirm it exists). This is where per-user scoping lands.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..users.models import User
from . import repository as repo
from .schemas import MessageOut, SessionDetail, SessionSummary

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get(
    "",
    response_model=list[SessionSummary],
    summary="List my chat sessions (newest-updated first)",
)
async def list_my_sessions(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SessionSummary]:
    rows = await repo.list_sessions(session, user_id=user.id)
    return [
        SessionSummary(
            id=s.id,
            title=s.title,
            created_at=s.created_at,
            updated_at=s.updated_at,
            message_count=count,
        )
        for s, count in rows
    ]


@router.get(
    "/{session_id}",
    response_model=SessionDetail,
    summary="Get one session with its full ordered message thread",
    responses={404: {"description": "Unknown session id (or not yours)."}},
)
async def get_my_session(
    session_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionDetail:
    chat_session = await repo.get_session_with_messages(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionDetail(
        id=chat_session.id,
        title=chat_session.title,
        created_at=chat_session.created_at,
        updated_at=chat_session.updated_at,
        messages=[MessageOut.model_validate(m) for m in chat_session.messages],
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a session and its messages (cascade)",
    responses={404: {"description": "Unknown session id (or not yours)."}},
)
async def delete_my_session(
    session_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    deleted = await repo.delete_session(session, session_id=session_id, user_id=user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
