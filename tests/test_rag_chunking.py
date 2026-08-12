"""Chunking. Pure — no IO, no models."""

import dataclasses

import pytest

from app.rag.chunking import Block, Chunk, chunk_table, chunk_text, merge_blocks, renumber


def test_short_text_is_one_chunk():
    chunks = chunk_text("a short policy note", max_chars=2000, overlap_chars=200)
    assert len(chunks) == 1
    assert chunks[0].content == "a short policy note"
    assert chunks[0].chunk_index == 0


def test_empty_or_whitespace_text_yields_no_chunks():
    assert chunk_text("   \n\t ", max_chars=100, overlap_chars=10) == []


def test_long_text_splits_and_respects_the_cap():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, max_chars=200, overlap_chars=20)
    assert len(chunks) > 1
    assert all(len(c.content) <= 200 for c in chunks)


def test_chunks_are_indexed_contiguously_from_zero():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_text(text, max_chars=100, overlap_chars=10)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_consecutive_chunks_overlap():
    """Overlap keeps a sentence spanning a boundary retrievable from either side."""
    text = " ".join(f"w{i}" for i in range(300))
    chunks = chunk_text(text, max_chars=120, overlap_chars=40)
    assert len(chunks) >= 2
    tail = chunks[0].content[-20:]
    assert tail.split()[-1] in chunks[1].content


def test_no_overlap_when_overlap_is_zero():
    text = "x" * 300
    chunks = chunk_text(text, max_chars=100, overlap_chars=0)
    assert "".join(c.content for c in chunks) == text


def test_overlap_larger_than_max_chars_does_not_loop_forever():
    """A misconfiguration must degrade, not hang."""
    text = "y" * 500
    chunks = chunk_text(text, max_chars=50, overlap_chars=500)
    assert 0 < len(chunks) < 100


def test_section_is_carried_onto_every_chunk():
    chunks = chunk_text("a b c", max_chars=10, overlap_chars=0,
                        section="Leave Policy > Annual")
    assert all(c.section == "Leave Policy > Annual" for c in chunks)


def test_table_chunks_repeat_the_header_row():
    """Each chunk must be self-describing — a bare row of values is useless
    when it is the only thing retrieved."""
    headers = ["Employee", "Department", "Days"]
    rows = [[f"Person {i}", "HR", str(i)] for i in range(50)]
    chunks = chunk_table(headers, rows, sheet_name="Leave", max_chars=200)
    assert len(chunks) > 1
    for c in chunks:
        assert "Employee" in c.content
        assert "Department" in c.content


def test_table_chunks_name_their_sheet_and_are_typed():
    chunks = chunk_table(["A"], [["1"]], sheet_name="Balances", max_chars=500)
    assert "Balances" in chunks[0].content
    assert chunks[0].element_type == "table"


def test_table_with_no_rows_yields_nothing():
    assert chunk_table(["A", "B"], [], sheet_name="Empty", max_chars=500) == []


def test_a_single_row_wider_than_the_cap_is_still_emitted():
    """Losing data silently is worse than exceeding the cap."""
    wide = ["z" * 900]
    chunks = chunk_table(["A"], [wide], sheet_name="S", max_chars=100)
    assert len(chunks) == 1
    assert "z" * 900 in chunks[0].content


def test_renumber_makes_indices_contiguous_across_concatenated_groups():
    a = chunk_text("one", max_chars=50, overlap_chars=0)
    b = chunk_text("two", max_chars=50, overlap_chars=0)
    merged = renumber(a + b)
    assert [c.chunk_index for c in merged] == [0, 1]


def test_chunk_is_immutable():
    c = Chunk(content="x", chunk_index=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.content = "y"


def _b(text, section="S1", page=None, kind="text"):
    return Block(text=text, section=section, page_number=page, element_type=kind)


def test_merge_joins_consecutive_blocks_in_the_same_section():
    out = merge_blocks([_b("alpha"), _b("beta"), _b("gamma")], max_chars=2000)
    assert len(out) == 1
    assert out[0].text == "alpha\nbeta\ngamma"
    assert out[0].section == "S1"


def test_merge_flushes_when_the_heading_path_changes():
    out = merge_blocks([_b("a", "S1"), _b("b", "S2")], max_chars=2000)
    assert [c.text for c in out] == ["a", "b"]
    assert [c.section for c in out] == ["S1", "S2"]


def test_merge_flushes_when_the_page_changes():
    """page_number is citation-bearing — search_department_docs renders it into
    the citation the model is told to cite, so a merged block must not span
    pages or it attributes a clause to a page it is not on."""
    out = merge_blocks([_b("a", page=1), _b("b", page=2)], max_chars=2000)
    assert [c.text for c in out] == ["a", "b"]
    assert [c.page_number for c in out] == [1, 2]


def test_merge_does_not_flush_when_every_page_is_none():
    """The DOCX case: a .docx has no fixed pages, so Docling gives no page_no.
    If None-vs-None counted as a change, every DOCX block would flush and
    merging would do nothing at all."""
    out = merge_blocks([_b("a"), _b("b"), _b("c")], max_chars=2000)
    assert len(out) == 1


def test_merge_flushes_at_max_chars():
    out = merge_blocks([_b("x" * 60), _b("y" * 60)], max_chars=100)
    assert [len(c.text) for c in out] == [60, 60]


def test_a_table_stands_alone_and_forces_a_flush():
    out = merge_blocks(
        [_b("intro"), _b("| a | b |", kind="table"), _b("outro")], max_chars=2000
    )
    assert [c.text for c in out] == ["intro", "| a | b |", "outro"]
    assert out[1].element_type == "table"


def test_merge_skips_blank_blocks():
    out = merge_blocks([_b("a"), _b("   "), _b("b")], max_chars=2000)
    assert len(out) == 1
    assert out[0].text == "a\nb"


def test_merged_block_keeps_the_first_blocks_metadata():
    out = merge_blocks([_b("a", "S1", 3), _b("b", "S1", 3)], max_chars=2000)
    assert (out[0].section, out[0].page_number, out[0].element_type) == ("S1", 3, "text")


def test_a_block_longer_than_max_chars_survives_whole():
    """chunk_text splits it downstream; merge must not drop it."""
    out = merge_blocks([_b("x" * 500)], max_chars=100)
    assert len(out) == 1 and len(out[0].text) == 500


def test_merge_of_nothing_is_nothing():
    assert merge_blocks([], max_chars=2000) == []
