"""Read/manage a user's chat sessions (all authed, all scoped to the caller).

Ownership is enforced everywhere: a session that isn't yours reads as 404 (we
never confirm it exists). This is where per-user scoping lands.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..rag.sources import with_download_urls
from ..users.models import User
from . import repository as repo
from .cursors import BadCursor
from .schemas import MessageOut, SessionDetail, SessionPage, SessionSummary

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get(
    "",
    response_model=SessionPage,
    summary="List my chat sessions, newest-updated first (paginated)",
    responses={400: {"description": "Malformed cursor."}},
)
async def list_my_sessions(
    limit: int = Query(repo.DEFAULT_PAGE_LIMIT, ge=1, le=repo.MAX_PAGE_LIMIT),
    cursor: str | None = Query(None, description="Opaque; from a prior next_cursor."),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionPage:
    try:
        rows, next_cursor = await repo.list_sessions_page(
            session, user_id=user.id, limit=limit, cursor=cursor
        )
    except BadCursor as exc:
        # 400, never a silent page one: a client stuck re-reading the first
        # page looks like "history is broken" and is invisible server-side.
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc
    return SessionPage(
        items=[
            SessionSummary(
                id=s.id,
                title=s.title,
                created_at=s.created_at,
                updated_at=s.updated_at,
                message_count=count,
            )
            for s, count in rows
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/{session_id}",
    response_model=SessionDetail,
    summary="Get one session with its full ordered message thread",
    responses={404: {"description": "Unknown session id (or not yours)."}},
)
async def get_my_session(
    session_id: str,
    request: Request,
    limit: int = Query(repo.DEFAULT_PAGE_LIMIT, ge=1, le=repo.MAX_PAGE_LIMIT),
    cursor: str | None = Query(None, description="Opaque; walks older messages."),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionDetail:
    try:
        chat_session, rows, next_cursor = await repo.get_thread_page(
            session, session_id=session_id, user_id=user.id,
            limit=limit, before_seq=cursor,
        )
    except BadCursor as exc:
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc
    if chat_session is None:
        raise HTTPException(status_code=404, detail="session not found")
    # The trace stays in the database for audit either way; EXPOSE_TRACE=false
    # only stops it being replayed to the client, so reloading an old thread
    # can't resurrect a "how it worked" panel the live turn didn't show.
    expose_trace = request.app.state.settings.expose_trace
    messages = []
    for m in rows:
        out = MessageOut.model_validate(m)
        if not expose_trace:
            out.trace = None
        # Sources are replayed regardless of EXPOSE_TRACE — they are part of the
        # answer, not part of the diagnostics. `download_url` is computed here
        # because it is never persisted.
        out.sources = with_download_urls(out.sources)
        messages.append(out)
    return SessionDetail(
        id=chat_session.id,
        title=chat_session.title,
        created_at=chat_session.created_at,
        updated_at=chat_session.updated_at,
        messages=messages,
        next_cursor=next_cursor,
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
