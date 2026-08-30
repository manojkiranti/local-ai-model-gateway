"""Offline tests for the read_department_doc tool.

Drives the tool fn directly with its data access stubbed, so the access rules
and the rendering are provable with no database — the reason app/rag/permissions
and app/rag/ranking are pure. The reassembly itself is tested in
tests/test_document_reassembly.py.
"""

from __future__ import annotations

import asyncio

import pytest

from app.rag.context import DepartmentContext, rag_context
from app.rag.reassemble import ChunkText
from app.tools.local import read_department_doc as tool


class _Doc:
    def __init__(self, title="Leave Policy", pages=None, origin=None):
        self.title = title
        self.pages = pages
        self.origin = origin


def _call(args, *, department="hr", doc=_Doc(), chunks=None, routes=None, monkeypatch=None):
    async def _fetch(document_id, department_id):
        if doc is None:
            return None
        return doc, (chunks if chunks is not None else []), (routes or [])

    monkeypatch.setattr(tool, "_fetch_document", _fetch)
    ctx = DepartmentContext(id=1, code=department) if department else None

    async def _run():
        if ctx is None:
            return await tool.SPEC.func(args)
        with rag_context(ctx):
            return await tool.SPEC.func(args)

    return asyncio.run(_run())


def _chunks(*bodies, page=None, section=None):
    return [
        ChunkText(chunk_index=i, content=b, page_number=page, section=section)
        for i, b in enumerate(bodies)
    ]


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
def test_outside_a_department_it_refuses(monkeypatch):
    out = _call({"document_id": "d1"}, department=None, monkeypatch=monkeypatch)
    assert "ERROR" in out or "department" in out.lower()


def test_an_unknown_document_is_one_message_that_does_not_leak_the_corpus(monkeypatch):
    """Unknown id, another department's document, and one that is not `ready`
    must be indistinguishable: at document granularity existence is the secret,
    the same rule the download route follows with its blanket 404."""
    out = _call({"document_id": "nope"}, doc=None, monkeypatch=monkeypatch)
    assert out.startswith("ERROR:")
    assert "not in this department" not in out
    assert "archived" not in out.lower()


def test_the_document_id_is_required(monkeypatch):
    out = _call({}, monkeypatch=monkeypatch)
    assert out.startswith("ERROR:")
    assert "document_id" in out


def test_there_is_no_department_parameter():
    """Scope comes from rag_context, never from the model — so a prompt
    injection has nothing to target. Same rule as search_department_docs."""
    assert "department" not in tool.SPEC.parameters["properties"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_it_returns_the_document_text_with_a_metadata_header(monkeypatch):
    out = _call(
        {"document_id": "d1"},
        chunks=_chunks("Staff accrue leave monthly."),
        monkeypatch=monkeypatch,
    )
    assert "Leave Policy" in out
    assert "Staff accrue leave monthly." in out


def test_a_document_with_no_chunks_says_so_rather_than_returning_nothing(monkeypatch):
    """A ready document with no chunks is a real state (an empty or
    image-only file). Silence would read as an empty document."""
    out = _call({"document_id": "d1"}, chunks=[], monkeypatch=monkeypatch)
    assert "no indexed text" in out.lower()


def test_long_documents_page_and_announce_where_to_resume(monkeypatch):
    out = _call(
        {"document_id": "d1", "max_lines": 2},
        chunks=_chunks(*[f"line {i}" for i in range(10)]),
        monkeypatch=monkeypatch,
    )
    assert "TRUNCATED" in out
    assert "start_line=3" in out


def test_paging_continues_from_the_promised_line(monkeypatch):
    kw = dict(chunks=_chunks(*[f"line {i}" for i in range(10)]), monkeypatch=monkeypatch)
    first = _call({"document_id": "d1", "max_lines": 2}, **kw)
    second = _call({"document_id": "d1", "start_line": 3, "max_lines": 2}, **kw)
    assert "line 0" in first and "line 2" not in first
    assert "line 2" in second and "line 0" not in second


# --------------------------------------------------------------------------- #
# NRB recovered text
# --------------------------------------------------------------------------- #
def test_machine_recovered_text_carries_the_verify_caveat(monkeypatch):
    """Recovered NRB text is unverified (§15 — conversion correctness is still
    unmeasured), so reading it in full must warn exactly as a citation does."""
    from app.rag.sources import VERIFY_NOTE

    out = _call(
        {"document_id": "d1"},
        doc=_Doc(origin="nrb"),
        chunks=_chunks("कार्यालय"),
        routes=["ocr"],
        monkeypatch=monkeypatch,
    )
    assert VERIFY_NOTE in out


def test_native_text_carries_no_caveat(monkeypatch):
    """Over-warning trains the reader to ignore the warning (§29.2) — native
    NRB text is exact and gets no caveat."""
    from app.rag.sources import VERIFY_NOTE

    out = _call(
        {"document_id": "d1"},
        doc=_Doc(origin="nrb"),
        chunks=_chunks("exact text"),
        routes=["native"],
        monkeypatch=monkeypatch,
    )
    assert VERIFY_NOTE not in out


def test_the_caveat_is_the_shared_constant_not_a_second_copy():
    """One constant, now three readers (search_department_docs, sources, here).
    A second copy drifts, and then two surfaces contradict each other."""
    import inspect

    from app.rag import sources

    assert "VERIFY_NOTE" in inspect.getsource(tool)
    assert sources.VERIFY_NOTE not in inspect.getsource(tool)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def test_the_search_tool_points_at_this_one():
    """Without the cross-reference the model's path to 'summarise that circular'
    is another search with different words, or read_document with an invented
    file_id. Same rule as aggregate_excel's."""
    from app.tools.local import search_department_docs

    assert "read_department_doc" in search_department_docs.SPEC.description


def test_this_tool_names_the_search_tool_as_where_ids_come_from():
    assert "search_department_docs" in tool.SPEC.description
