"""Reassembling a corpus document from its stored chunks (`app/rag/reassemble.py`).

Pure module: no DB, no model. It takes the rows `document_chunks` holds and puts
the document back together as lines, which is what `read_department_doc` reads.
"""

from __future__ import annotations

from app.rag.reassemble import ChunkText, to_lines


def c(index, content, *, page=None, section=None):
    return ChunkText(chunk_index=index, content=content, page_number=page, section=section)


def test_chunks_are_joined_in_index_order_not_the_order_given():
    lines = to_lines([c(2, "second"), c(0, "first"), c(1, "middle")], overlap=0)
    assert lines == ["first", "middle", "second"]


def test_the_repeated_heading_prefix_is_stripped():
    """`parsing._attach_headings` prepends the heading path to every chunk's
    CONTENT (tsv indexes content alone), so a naive join repeats the heading at
    every boundary. The `section` column says exactly what to strip."""
    chunks = [
        c(0, "Leave Policy\n\nStaff accrue leave monthly.", section="Leave Policy"),
        c(1, "Leave Policy\n\nUnused leave lapses in Chaitra.", section="Leave Policy"),
    ]
    assert to_lines(chunks, overlap=0) == [
        "Leave Policy",
        "",
        "Staff accrue leave monthly.",
        "Unused leave lapses in Chaitra.",
    ]


def test_a_heading_is_emitted_again_when_it_CHANGES():
    chunks = [
        c(0, "A\n\nfirst body", section="A"),
        c(1, "B\n\nsecond body", section="B"),
    ]
    assert to_lines(chunks, overlap=0) == ["A", "", "first body", "B", "", "second body"]


def test_overlapping_text_is_not_duplicated():
    """Chunking overlaps by RAG_CHUNK_OVERLAP_CHARS (200 by default), so
    consecutive chunks genuinely share text. Joining naively prints it twice."""
    chunks = [c(0, "alpha beta gamma delta"), c(1, "gamma delta epsilon zeta")]
    assert to_lines(chunks, overlap=20) == ["alpha beta gamma delta epsilon zeta"]


def test_text_that_merely_LOOKS_repeated_is_kept():
    """De-overlap must never delete real content. The search is bounded by the
    configured overlap, so a long genuine repetition is left alone."""
    chunks = [c(0, "the rate is 5%"), c(1, "the rate is 5% and that is final")]
    out = " ".join(to_lines(chunks, overlap=3))
    assert out.count("the rate is 5%") == 2


def test_a_page_change_emits_a_page_marker():
    """Same '[page N]' convention read_document uses, so there is ONE paging
    unit and a citation can name the page."""
    chunks = [c(0, "on one", page=1), c(1, "on two", page=2)]
    assert to_lines(chunks, overlap=0) == ["[page 1]", "on one", "[page 2]", "on two"]


def test_the_same_page_is_marked_once():
    chunks = [c(0, "first", page=3), c(1, "second", page=3)]
    assert to_lines(chunks, overlap=0) == ["[page 3]", "first", "second"]


def test_chunks_without_pages_get_no_markers():
    assert to_lines([c(0, "plain text")], overlap=0) == ["plain text"]


def test_no_chunks_is_no_lines():
    assert to_lines([], overlap=0) == []
