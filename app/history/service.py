"""Turn orchestration for /v1/chat.

`open_turn` implements the common front half of a turn: resolve or create the
session (verifying ownership), persist the user message IMMEDIATELY (its own
commit — so a later model failure never loses what the user typed), and return
the rebuilt context (prior clean turns + the new user message) for the model.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..files import ingest, readers
from ..files import repository as files_repo
from ..rag.access import resolve_department
from ..rag.context import DepartmentContext
from ..users.models import User
from . import context as ctx
from . import repository as repo
from .models import ChatSession

logger = logging.getLogger(__name__)


def _log_budget(session_id, tail, selection, budget) -> None:
    """Why this is logged, not merely computed: `context_window_tokens` is a
    duplicate of a value set on the Ollama service that this process cannot read
    back. If the two disagree, every symptom looks like a healthy turn. This
    line is the only place the disagreement becomes visible, and it is the
    dataset the estimator's constants get calibrated against — compare it with
    the server's reported usage.prompt_tokens.

    That comparison is NOT automatic on every live turn: the turn path
    (`agent/loop.py`) always talks to Ollama over the SSE `/v1/chat/completions`
    stream (for both `stream:true` and `stream:false` clients — `run_turn` just
    drains the same generator), and Ollama's stream carries no `usage` field
    without `stream_options.include_usage`, which is unverified against the
    production server (no GPU-box access — see CLAUDE.md §19.1) and is
    therefore NOT turned on speculatively; faking a number here would be worse
    than not logging one. `app.ollama.client.OllamaClient.chat()` +
    `normalize_usage` DO surface the real `usage.prompt_tokens` for the
    non-streaming completion path, and that is exactly the method the design
    doc's own calibration measurement used directly against Ollama. Use that
    path (a one-off script, not a live turn) to recalibrate the estimator
    constants; see the design doc's Evaluation section for the worked example.
    """
    spent = sum(ctx.estimate_message_tokens(m) for m in selection.messages)
    logger.info(
        "context session=%s read=%d selected=%d est_tokens=%d budget=%d truncated=%s",
        session_id, len(tail), len(selection.messages), spent, budget,
        selection.truncated,
    )


async def _resolve_attachments(
    session: AsyncSession, *, user_id: int, file_ids: list[str] | None
) -> list[dict] | None:
    """Verify the caller owns each attached file and summarize it. Returns the
    persisted attachment records [{id, filename, summary}] or None. Raises 404
    for an unknown/foreign id (never leak another user's file)."""
    if not file_ids:
        return None
    records: list[dict] = []
    for fid in file_ids:
        row = await files_repo.get_owned_file(session, file_id=fid, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"attached file not found: {fid}")
        try:
            summary = await asyncio.to_thread(ingest.summarize, row.path)
            summary_text = summary.text()
        except readers.ReadError:
            summary_text = ""  # still attach it; the read tool will report the error
        records.append({"id": row.id, "filename": row.filename, "summary": summary_text})
    return records


async def open_turn(
    session: AsyncSession,
    *,
    user: "User",
    session_id: str | None,
    message: str,
    file_ids: list[str] | None = None,
    department: str | None = None,
) -> tuple[ChatSession, list[dict[str, str]], "DepartmentContext | None"]:
    """Returns (chat_session, context_messages, department_ctx), user row committed.

    context_messages = prior clean turns + (optional attachment note) + the new
    user message, ready to hand to the model. Raises 404 if a given session_id
    isn't owned by this user, or if an attached file_id isn't owned by them.

    `department` is the tab code from the request. It is REQUIRED only to open a
    new department chat; on an existing bound session it is an optional
    consistency check and the session's own `department_id` is the source of
    truth. `resolve_department` owns that contract (403/404/409) — see rag.access.
    """
    user_id = user.id
    # Verify + summarize attachments BEFORE persisting anything (so a bad id is a
    # clean 404 and doesn't leave a half-written turn).
    attachments = await _resolve_attachments(session, user_id=user_id, file_ids=file_ids)

    if session_id:
        # The session ROW only — its messages are read separately and bounded.
        chat_session = await repo.get_owned_session(
            session, session_id=session_id, user_id=user_id
        )
        if chat_session is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Re-checked on EVERY turn, which is what makes a revoked grant take
        # effect on the next turn rather than at token expiry.
        dept_ctx = await resolve_department(session, user, department, chat_session)
        settings = get_settings()
        tail = await repo.get_context_tail(
            session,
            session_id=session_id,
            user_id=user_id,
            max_messages=settings.context_max_messages,
        )
        budget = ctx.budget_for(settings)
        selection = ctx.select_turns(tail, budget)
        # A new upload supersedes every earlier one, so the replayed notes are
        # demoted and only the note appended below stays active.
        context = ctx.build_context_messages(
            selection.messages,
            pending_attachments=bool(attachments),
            truncated=selection.truncated,
            pinned_attachments=selection.pinned_attachments,
        )
        _log_budget(session_id, tail, selection, budget)
    else:
        # chat_session=None tells resolve_department this is a NEW session, which
        # MAY open in a department — unlike an existing general chat, which may
        # not be adopted into one.
        dept_ctx = await resolve_department(session, user, department, None)
        chat_session = await repo.create_session(
            session,
            user_id=user_id,
            title=repo.make_title(message),
            department_id=dept_ctx.id if dept_ctx else None,
        )
        context = []

    await repo.add_user_message(
        session, session_id=chat_session.id, content=message, attachments=attachments
    )
    await session.commit()  # user message persisted immediately

    if attachments:
        # ROLE MATTERS, and it is not cosmetic: measured against qwen3.5:35b-a3b
        # with the 16 tool schemas loaded, a `system` note produced a tool call
        # 3/12 times versus 12/12 as `user`. The model READS a system note fine
        # (asked directly, it returns the id every time) but treats it as
        # background rather than something to act on, and asks the user for an id
        # it already has. Stronger imperative wording barely moved it (2/6) —
        # only the role did. See build_context_messages, which replays notes for
        # the same reason.
        context.append(
            {"role": "user", "content": ctx.format_attachment_note(attachments)}
        )
    context.append({"role": "user", "content": message})
    return chat_session, context, dept_ctx
