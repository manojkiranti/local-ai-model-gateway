"""The department in effect for the current request.

Same mechanism as `app/files/store.py`'s file sink/source, and for the same
reason: retrieval must be scoped to a department that the *model* cannot choose.
The tool reads it from here, so `search_department_docs` has no department
parameter and a prompt injection has nothing to target.

Streaming gotcha, inherited from the file sink: for a streamed turn this MUST be
installed INSIDE the async generator Starlette iterates, not merely in the router
before returning the StreamingResponse — otherwise it is invisible while the
agent loop runs.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class DepartmentContext:
    """The authorized department for this turn. Frozen so nothing downstream can
    quietly repoint it after `resolve_department` has vetted it."""

    id: int
    code: str


# None -> no department (general chat); retrieval tools refuse to run.
_current: ContextVar[DepartmentContext | None] = ContextVar(
    "current_department", default=None
)


@contextmanager
def rag_context(ctx: DepartmentContext) -> Iterator[None]:
    """Install `ctx` as the active department for the enclosed block."""
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)


def current_department() -> DepartmentContext | None:
    """The active department, or None outside a department-scoped turn."""
    return _current.get()
