# RAG Ingest Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Docling-parsed documents produce coherent retrievable passages instead of layout fragments, and stop front matter outranking real prose.

**Architecture:** `_parse_with_docling` changes shape from "chunk each element" to "collect blocks → merge → filter → chunk". Two new pure helpers in `chunking.py` do the work; `parsing.py` gains a front-matter skip; `retrieval.py` exposes the per-channel ranks its SQL already computes; a small `reingest` command replays existing documents through the worker.

**Tech Stack:** Python 3.10, SQLAlchemy 2 async, Postgres + pgvector, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-08-12-rag-ingest-quality-design.md`

## Global Constraints

- **Use this project's venv:** `.venv/bin/python`, `.venv/bin/pytest`. Never a sibling project's.
- **No new dependency.** No Alembic migration — no schema change is required.
- **Docling must never be imported at module scope.** `test_docling_is_not_imported_at_module_scope` is a subprocess test that locks this; all new logic is pure and must not import Docling at all.
- **`app/rag/chunking.py` stays pure** — no IO, no model calls, no config import.
- **Scope is the Docling path only.** Do not change `chunk_table`, `_parse_spreadsheet`, or the `text` file branch. Applying the tiny-body filter globally would make a legitimately short `.txt` filter to zero chunks and raise `ParseError`.
- **Table handling is preserved exactly.** A Docling table is exported via `item.export_to_markdown(document)` and passed to `chunk_text` — it does NOT use `chunk_table`, which is spreadsheet-only. Tables gain exactly two properties: they are merge boundaries, and they are exempt from the tiny-body filter.
- **New parameters must be OPTIONAL with defaults.** `parse_to_chunks` is called from 15 sites in `tests/test_rag_parsing.py` and `tests/test_rag_parsing_docling.py` via an `OPTS` dict; required params would break all of them.
- **Exact config values:**
  - `rag_chunk_min_body_chars: int = 40`
  - `rag_skip_sections: str = "table of contents,contents,index"`
- **Merge flush conditions, all four:** section change, page change, table, `max_chars` exceeded.
- **Order is load-bearing: merge FIRST, filter SECOND.** Filtering first deletes real content (a 45-char glossary definition is short only because merging hasn't happened yet).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/rag/chunking.py` | Modify — add `Block`, `merge_blocks`, `drop_small_blocks` (all pure) |
| `app/config.py` | Modify — two settings + one derived property |
| `app/rag/parsing.py` | Modify — front-matter skip helpers; rewire `_parse_with_docling` |
| `app/rag/retrieval.py` | Modify — select `dense_rank`/`lexical_rank`, add to `RetrievedChunk` |
| `app/rag/reingest.py` | **Create** — `python -m app.rag.reingest` backfill command |
| `tests/test_rag_chunking.py` | Modify — cover `merge_blocks`, `drop_small_blocks` |
| `tests/test_rag_parsing.py` | Modify — cover the skip helpers |
| `tests/test_rag_parsing_docling.py` | Modify — chunk-count assertions change (premise legitimately invalidated) |
| `tests/test_rag_retrieval_integration.py` | Modify — per-channel ranks populated |
| `tests/test_rag_reingest_integration.py` | **Create** — backfill command |
| `tests/test_rag_retrieval_eval.py` | **Create** — 6 labelled queries, live/skipped |

---

### Task 1: `Block` and `merge_blocks`

**Files:**
- Modify: `app/rag/chunking.py`
- Test: `tests/test_rag_chunking.py`

**Interfaces:**
- Consumes: existing `Chunk`, `replace` (already imported from dataclasses)
- Produces:
  - `@dataclass(frozen=True) Block{text: str, section: str | None = None, page_number: int | None = None, element_type: str = "text"}`
  - `merge_blocks(blocks: Sequence[Block], *, max_chars: int) -> list[Block]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rag_chunking.py` (add `Block, merge_blocks` to the existing import from `app.rag.chunking`):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_rag_chunking.py -k merge -v
```

Expected: collection error — `ImportError: cannot import name 'Block'`.

- [ ] **Step 3: Write the implementation**

In `app/rag/chunking.py`, add after the `Chunk` dataclass:

```python
@dataclass(frozen=True)
class Block:
    """One parsed element BEFORE it becomes a Chunk.

    Docling emits a Block per layout element — a heading, a list bullet, a
    stray page number. `merge_blocks` is what turns a run of those back into a
    passage.
    """

    text: str
    section: str | None = None
    page_number: int | None = None
    element_type: str = "text"


def merge_blocks(blocks: Sequence[Block], *, max_chars: int) -> list[Block]:
    """Join consecutive blocks that belong to the same passage.

    Without this, chunking one Docling element at a time produced 559 chunks
    averaging 181 characters for a single policy document — half of them under
    60 — so real prose was buried under fragments and `max_chars` never meant
    anything. Measured 2026-08-12; see the design spec.

    Flushes when the passage's identity changes (`section` or `page_number`),
    when a table is involved, or when the budget would be exceeded.
    """
    merged: list[Block] = []
    buffer: list[Block] = []

    def flush() -> None:
        nonlocal buffer
        if buffer:
            merged.append(
                replace(buffer[0], text="\n".join(b.text for b in buffer))
            )
            buffer = []

    for block in blocks:
        if not block.text.strip():
            continue
        # A table stands alone: a markdown grid spliced into surrounding
        # sentences is unreadable to the model AND to the lexical channel.
        if block.element_type == "table":
            flush()
            merged.append(block)
            continue
        if buffer:
            head = buffer[0]
            size = sum(len(b.text) + 1 for b in buffer)
            if (
                block.section != head.section
                or block.page_number != head.page_number
                or size + len(block.text) + 1 > max_chars
            ):
                flush()
        buffer.append(block)

    flush()
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_rag_chunking.py -v
```

Expected: all pass (the 15 pre-existing plus 10 new).

- [ ] **Step 5: Commit**

```bash
git add app/rag/chunking.py tests/test_rag_chunking.py
git commit -m "feat(rag): merge_blocks — join Docling elements into passages

Chunking one layout element at a time produced 559 chunks averaging 181 chars
for one policy document, so rag_chunk_max_chars never meant anything. Flushes
on section change, page change, table, or budget — page because page_number is
citation-bearing and a merged block carries only one."
```

---

### Task 2: `drop_small_blocks`

**Files:**
- Modify: `app/rag/chunking.py`
- Test: `tests/test_rag_chunking.py`

**Interfaces:**
- Consumes: `Block` from Task 1
- Produces: `drop_small_blocks(blocks: Sequence[Block], *, min_body_chars: int) -> list[Block]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rag_chunking.py` (add `drop_small_blocks` to the import):

```python
def test_drop_small_removes_layout_debris():
    out = drop_small_blocks([_b("th"), _b("2026"), _b("x" * 200)], min_body_chars=40)
    assert [len(c.text) for c in out] == [200]


def test_drop_small_keeps_a_real_glossary_definition_at_the_default():
    """The reason the default is 40 and not 60: this 45-char definition is real
    content that a coarser floor would delete."""
    body = "means Assets Liability Committee of the Bank."
    assert len(body) == 45
    assert drop_small_blocks([_b(body)], min_body_chars=40) == [_b(body)]


def test_drop_small_exempts_tables_at_any_size():
    """A small table is real content; its information density is not
    proportional to its character count."""
    tiny_table = _b("| a |", kind="table")
    assert drop_small_blocks([tiny_table], min_body_chars=40) == [tiny_table]


def test_drop_small_measures_the_stripped_body():
    assert drop_small_blocks([_b("  ab  ")], min_body_chars=5) == []


def test_drop_small_boundary_is_inclusive():
    assert drop_small_blocks([_b("x" * 40)], min_body_chars=40) != []
    assert drop_small_blocks([_b("x" * 39)], min_body_chars=40) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_rag_chunking.py -k drop_small -v
```

Expected: `ImportError: cannot import name 'drop_small_blocks'`.

- [ ] **Step 3: Write the implementation**

In `app/rag/chunking.py`, add after `merge_blocks`:

```python
def drop_small_blocks(
    blocks: Sequence[Block], *, min_body_chars: int
) -> list[Block]:
    """Remove orphaned fragments. MUST run AFTER `merge_blocks`.

    The order is load-bearing. Run this first and it deletes real content: the
    45-character body "means Assets Liability Committee of the Bank." is a
    glossary definition that is short only because the term it defines is a
    separate Docling element. After merging, anything still this small is
    layout debris — a page number, a stray "th" from "279th".

    Tables are exempt at any size: a small table is real content, and its
    information density is not proportional to its character count.
    """
    return [
        b
        for b in blocks
        if b.element_type == "table" or len(b.text.strip()) >= min_body_chars
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_rag_chunking.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/rag/chunking.py tests/test_rag_chunking.py
git commit -m "feat(rag): drop_small_blocks, applied after merging

Order is load-bearing: filtering before merging deletes real content, because
a glossary definition is orphaned from its term until the merge runs. Tables
are exempt at any size."
```

---

### Task 3: Config settings and the front-matter skip helpers

**Files:**
- Modify: `app/config.py` (settings near `rag_chunk_overlap_chars:104-105`; property near `fetch_url_allowed_hosts:167-169`)
- Modify: `app/rag/parsing.py`
- Test: `tests/test_rag_parsing.py`

**Interfaces:**
- Consumes: `Settings._csv` (a `@staticmethod` at `config.py:142`)
- Produces:
  - `Settings.rag_chunk_min_body_chars: int`, `Settings.rag_skip_sections: str`, `Settings.rag_skipped_sections -> set[str]`
  - `parsing._normalize_heading(text: str) -> str`
  - `parsing._is_skipped_section(section: str | None, skip: set[str]) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rag_parsing.py`:

```python
def test_normalize_heading_folds_case_whitespace_and_trailing_punctuation():
    from app.rag.parsing import _normalize_heading

    assert _normalize_heading("  Table   of  Contents :  ") == "table of contents"
    assert _normalize_heading("CONTENTS.") == "contents"


def test_front_matter_is_skipped_by_first_segment():
    from app.rag.parsing import _is_skipped_section

    skip = {"table of contents", "contents", "index"}
    assert _is_skipped_section("Table of Contents", skip)
    assert _is_skipped_section("Table of Contents > 5.2.5 Assurance of Limits", skip)


def test_a_legitimate_index_section_is_not_skipped():
    """First-segment-only is the guard: a policy document's own
    'Index of Limits' under a real chapter must stay indexed."""
    from app.rag.parsing import _is_skipped_section

    skip = {"table of contents", "contents", "index"}
    assert not _is_skipped_section("Chapter 3 > Index of Limits", skip)
    assert not _is_skipped_section("Chapter 4: Investment Products", skip)


def test_skip_is_inert_with_no_section_or_empty_set():
    from app.rag.parsing import _is_skipped_section

    assert not _is_skipped_section(None, {"contents"})
    assert not _is_skipped_section("Contents", set())


def test_settings_expose_the_skip_list_normalized():
    from app.config import Settings

    s = Settings(rag_skip_sections="Table of Contents, Contents ,Index")
    assert s.rag_skipped_sections == {"table of contents", "contents", "index"}
    assert s.rag_chunk_min_body_chars == 40
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_rag_parsing.py -k "skip or normalize or settings_expose" -v
```

Expected: `ImportError: cannot import name '_normalize_heading'`.

- [ ] **Step 3: Add the settings**

In `app/config.py`, after `rag_chunk_overlap_chars: int = 200`:

```python
    # Applied AFTER merging, so it only ever sees orphaned fragments. 40 is an
    # empirically chosen default for the current corpus, NOT a universally safe
    # threshold — the smallest real content observed was a 45-char glossary
    # definition, and post-merge bodies average ~1481 chars. Re-check the
    # body-length distribution before changing it for a different corpus.
    rag_chunk_min_body_chars: int = 40
    # Front matter, matched against the FIRST segment of a chunk's heading path.
    rag_skip_sections: str = "table of contents,contents,index"
```

And with the other properties (near `fetch_url_allowed_hosts`):

```python
    @property
    def rag_skipped_sections(self) -> set[str]:
        return {s.lower() for s in self._csv(self.rag_skip_sections)}
```

- [ ] **Step 4: Add the parsing helpers**

In `app/rag/parsing.py`, add `import re` to the stdlib imports, then add these near `_heading_path`:

```python
def _normalize_heading(text: str) -> str:
    """Casefold, collapse internal whitespace, drop trailing punctuation."""
    return re.sub(r"\s+", " ", text).strip().strip(".:;—-").strip().casefold()


def _is_skipped_section(section: str | None, skip: set[str]) -> bool:
    """True when a chunk's heading path starts with front matter.

    Matches the FIRST segment only, deliberately: that catches
    "Table of Contents" and "Table of Contents > 5.2.5 …" while leaving a
    legitimate "Chapter 3 > Index of Limits" indexed. Matching any segment
    would delete exactly the content most worth keeping in a policy document.
    """
    if not section or not skip:
        return False
    return _normalize_heading(section.split(" > ", 1)[0]) in skip
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_rag_parsing.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/rag/parsing.py tests/test_rag_parsing.py
git commit -m "feat(rag): front-matter skip helpers and their settings

Matches the FIRST heading-path segment only, so 'Table of Contents > 5.2.5' is
skipped while 'Chapter 3 > Index of Limits' stays indexed."
```

---

### Task 4: Rewire `_parse_with_docling`

**Files:**
- Modify: `app/rag/parsing.py:196-262`
- Modify: `app/rag/worker.py:134-139`
- Test: `tests/test_rag_parsing_docling.py`

**Interfaces:**
- Consumes: `Block`, `merge_blocks`, `drop_small_blocks` (Tasks 1-2); `_is_skipped_section` (Task 3)
- Produces: `parse_to_chunks(path, file_type, *, max_chars, overlap_chars, min_body_chars: int = 0, skip_sections: set[str] | None = None)` — **both new params optional**, so the 15 existing call sites keep working

- [ ] **Step 1: Rewrite the walk**

In `app/rag/parsing.py`, replace the body of the `for item, _tree_level in document.iterate_items():` loop's tail (the part from `if label == "table":` through `collected.extend(...)`) so the loop only COLLECTS, and add merge/filter/chunk after it. The full replacement for `_parse_with_docling`'s collection section:

```python
    blocks: list[Block] = []

    for item, _tree_level in document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "") or ""
        prov = getattr(item, "prov", None) or []
        page = prov[0].page_no if prov else None

        if label in ("section_header", "title"):
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            level = getattr(item, "level", 1) or 1
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
            continue  # the heading itself is carried into following chunks

        section = _heading_path(headings)
        # Front matter never reaches the index: a Table of Contents lists every
        # heading in the document, so it matches almost any structural query,
        # and ts_rank_cd favours short text — it outranked real prose 7 slots
        # out of 12. Measured 2026-08-12; see the design spec.
        if _is_skipped_section(section, skip_sections or set()):
            continue

        if label == "table":
            try:
                text = item.export_to_markdown(document).strip()
            except Exception:  # noqa: BLE001 - a malformed table is not fatal
                text = ""
        else:
            text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        blocks.append(
            Block(
                text=text,
                section=section,
                page_number=page,
                element_type=_ELEMENT_TYPES.get(label, "text"),
            )
        )

    # Merge BEFORE filtering: a short block is often real content orphaned from
    # its neighbours by Docling's element split, and only merging can tell the
    # difference. See drop_small_blocks.
    blocks = drop_small_blocks(
        merge_blocks(blocks, max_chars=max_chars), min_body_chars=min_body_chars
    )

    collected: list[Chunk] = []
    for block in blocks:
        pieces = chunk_text(
            block.text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            section=block.section,
            page_number=block.page_number,
            element_type=block.element_type,
        )
        collected.extend(_with_context(pieces, block.section))

    if not collected:
        raise ParseError(
            "document contained only front matter or fragments — nothing to index"
        )
    return collected
```

Update the imports at the top of `parsing.py`:

```python
from .chunking import (
    Block,
    Chunk,
    chunk_table,
    chunk_text,
    drop_small_blocks,
    merge_blocks,
    renumber,
)
```

Update `_parse_with_docling`'s signature to accept the two new parameters:

```python
def _parse_with_docling(
    path: Path,
    *,
    max_chars: int,
    overlap_chars: int,
    min_body_chars: int = 0,
    skip_sections: set[str] | None = None,
) -> list[Chunk]:
```

> Note: the pre-existing scanned-PDF `ParseError` ("document produced no text — a scanned PDF needs OCR…") is REPLACED by the message above only in the all-filtered case. Keep BOTH: raise the scanned message when `blocks` is empty *before* merge/filter, and the front-matter message when it is empty *after*. An admin must be able to tell a scan from a TOC-only file.

- [ ] **Step 2: Thread the parameters through `parse_to_chunks`**

```python
def parse_to_chunks(
    path: Path,
    file_type: str,
    *,
    max_chars: int,
    overlap_chars: int,
    min_body_chars: int = 0,
    skip_sections: set[str] | None = None,
) -> list[Chunk]:
    """Dispatch on `file_type`, returning contiguously indexed chunks.

    `min_body_chars`/`skip_sections` apply to the Docling path ONLY — the text
    and spreadsheet branches already produce whole-body or row-buffered chunks,
    and a global filter would reduce a legitimately short .txt to zero chunks.
    """
```

and pass them only in the `pdf`/`docx` branch:

```python
    elif file_type in ("pdf", "docx"):
        chunks = _parse_with_docling(
            path,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            min_body_chars=min_body_chars,
            skip_sections=skip_sections,
        )
```

- [ ] **Step 3: Wire the worker**

In `app/rag/worker.py:134-139`:

```python
    return parse_to_chunks(
        path,
        snap.file_type,
        max_chars=settings.rag_chunk_max_chars,
        overlap_chars=settings.rag_chunk_overlap_chars,
        min_body_chars=settings.rag_chunk_min_body_chars,
        skip_sections=settings.rag_skipped_sections,
    )
```

- [ ] **Step 4: Add the integration tests**

Append to `tests/test_rag_parsing_docling.py`:

```python
def test_docling_output_is_merged_not_fragmented(docx_file):
    """The regression this whole change exists for: one chunk per layout
    element gave 181-char average chunks."""
    chunks = parse_to_chunks(docx_file, "docx", **OPTS)
    bodies = [len(c.content) - len(c.section or "") for c in chunks]
    assert max(bodies) > 200, f"still fragmented: {bodies}"


def test_front_matter_is_not_indexed(docx_toc_file):
    chunks = parse_to_chunks(
        docx_toc_file, "docx",
        max_chars=2000, overlap_chars=200,
        skip_sections={"table of contents"},
    )
    assert all("Table of Contents" not in (c.section or "") for c in chunks)
    assert chunks, "the real content should survive"
```

Add this fixture beside the existing `docx_file` fixture (same module scope, same construction style):

```python
@pytest.fixture(scope="module")
def docx_toc_file(tmp_path_factory):
    """A document whose front matter looks exactly like the real failure: a
    Table of Contents listing headings, then the real chapter."""
    from docx import Document

    doc = Document()
    doc.add_heading("Table of Contents", level=1)
    for line in (
        "4.1 Investment in Government Securities    12",
        "4.7 Other Investments    20",
        "5.2.5 Assurance of Investment Limits    24",
    ):
        doc.add_paragraph(line)
    doc.add_heading("Chapter 4: Investment Products", level=1)
    doc.add_paragraph(
        "The Bank may invest in permitted shares, debentures and bonds of "
        "institutions approved by the Board, subject to the single-obligor "
        "limits set out in Chapter 5. Each proposal is assessed for credit "
        "quality, tenor and liquidity before any commitment is made."
    )
    path = tmp_path_factory.mktemp("docling_toc") / "policy_toc.docx"
    doc.save(path)
    return path
```

- [ ] **Step 5: Run the Docling tests and fix invalidated assertions**

```bash
.venv/bin/pytest tests/test_rag_parsing_docling.py -v
```

Some existing assertions in this file pin chunk counts or per-chunk content from the OLD one-element-per-chunk behaviour. Those premises are legitimately invalidated by merging. For each failure: confirm the new value is correct for merged output, then update the assertion and note it in your report. **Do not weaken an assertion to make it pass** — if a test checked that headings appear in content, it must still check that.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/pytest -q
```

- [ ] **Step 7: Commit**

```bash
git add app/rag/parsing.py app/rag/worker.py tests/test_rag_parsing_docling.py
git commit -m "feat(rag): collect, merge, filter, then chunk Docling output

_parse_with_docling chunked each layout element, so rag_chunk_max_chars was an
upper bound per element and never a target. Now it collects Blocks, merges
within a heading path and page, drops orphans, and only then chunks. Front
matter is skipped outright. New params default to inert so the existing
call sites are unaffected."
```

---

### Task 5: Per-channel ranks in retrieval

**Files:**
- Modify: `app/rag/retrieval.py` (the `fused` CTE, the final `SELECT`, `RetrievedChunk`, the row mapping at :159-175)
- Test: `tests/test_rag_retrieval_integration.py`

**Interfaces:**
- Produces: `RetrievedChunk.dense_rank: int | None`, `RetrievedChunk.lexical_rank: int | None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_retrieval_integration.py`. That file already provides everything you need — read it first: `_sql(fn)` (a decorator that builds a throwaway `NullPool` engine per call), `_skip_if_no_db()`, `_unit(slot)` for deterministic vectors, and **`_search(dept, qtext, qvec, limit=10)`**. Reuse `_search`; do not invent a new helper. Mirror the seeding an existing test in that file does, then:

```python
def test_results_carry_the_rank_from_each_channel():
    """Diagnostics: when retrieval returns the wrong passage, these say WHICH
    channel surfaced it. Both ranks are already computed to drive RRF; before
    this they were never selected out, so diagnosing a bad result meant
    reproducing the query by hand."""
    _skip_if_no_db()
    dept, _docs = _seed_corpus()          # follow the seeding an existing test uses
    rows = _search(dept, "investment limits", _unit(0), limit=10)
    assert rows
    for r in rows:
        # RRF only returns a row if at least one channel found it.
        assert r.dense_rank is not None or r.lexical_rank is not None
        assert r.dense_rank is None or r.dense_rank >= 1
        assert r.lexical_rank is None or r.lexical_rank >= 1


def test_a_chunk_only_one_channel_found_has_none_for_the_other():
    """A lexical-only hit must not be reported as if the dense channel ranked
    it — that would make the diagnostics lie about attribution."""
    _skip_if_no_db()
    dept, _docs = _seed_corpus()
    # A term present in the text but far from the query vector's slot.
    rows = _search(dept, "zzzqxq", _unit(3), limit=10)
    for r in rows:
        if r.lexical_score is None:
            assert r.lexical_rank is None
        if r.dense_distance is None:
            assert r.dense_rank is None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/bin/pytest tests/test_rag_retrieval_integration.py -k rank -v
```

Expected: `AttributeError: 'RetrievedChunk' object has no attribute 'dense_rank'`.

- [ ] **Step 3: Select the ranks**

In `app/rag/retrieval.py`, the `fused` CTE — add two columns:

```sql
fused AS (
    SELECT COALESCE(d.id, l.id) AS id,
           d.distance      AS dense_distance,
           l.lexical_score AS lexical_score,
           d.rank          AS dense_rank,
           l.rank          AS lexical_rank,
           COALESCE(1.0 / (:rrf_k + d.rank), 0)
         + COALESCE(1.0 / (:rrf_k + l.rank), 0) AS rrf_score
      FROM dense d
      FULL OUTER JOIN lexical l USING (id)
)
```

The final `SELECT` — add two lines after `fused.lexical_score  AS lexical_score,`:

```sql
       fused.dense_rank     AS dense_rank,
       fused.lexical_rank   AS lexical_rank,
```

- [ ] **Step 4: Add the dataclass fields and mapping**

In `RetrievedChunk`, after `lexical_score`:

```python
    # Which rank each channel gave this chunk, or None if that channel did not
    # return it at all. Diagnostics only — never rendered into the tool result.
    # These make a bad retrieval attributable to a channel from stored data
    # instead of a hand-built reproduction.
    dense_rank: int | None
    lexical_rank: int | None
```

In the row mapping:

```python
            dense_rank=(None if r["dense_rank"] is None else int(r["dense_rank"])),
            lexical_rank=(
                None if r["lexical_rank"] is None else int(r["lexical_rank"])
            ),
```

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/pytest tests/test_rag_retrieval_integration.py tests/test_search_department_docs.py -v
```

Expected: pass. `search_department_docs` must be unaffected — the new fields are not rendered into the tool result.

- [ ] **Step 6: Commit**

```bash
git add app/rag/retrieval.py tests/test_rag_retrieval_integration.py
git commit -m "feat(rag): expose per-channel ranks on retrieval results

The SQL already computed dense.rank and lexical.rank to drive RRF; they were
never selected out, so diagnosing a bad retrieval meant reproducing the query
by hand. Diagnostics only — not rendered to the model."
```

---

### Task 6: The re-ingest command

**Files:**
- Create: `app/rag/reingest.py`
- Test: `tests/test_rag_reingest_integration.py`

**Interfaces:**
- Consumes: `jobs.enqueue(session, *, document_id) -> IngestJob` (raises `JobConflict`), `models.Document`, `models.STATUS_ARCHIVED`
- Produces: `reingest(session, *, department_code: str | None, dry_run: bool) -> dict[str, int]` returning `{"queued": n, "skipped": n, "total": n}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rag_reingest_integration.py`. Each call builds a throwaway `NullPool` engine — the app's module-level `engine` pools connections bound to the first event loop, and every `asyncio.run` makes a new one, so reusing it dies with "Event loop is closed". This mirrors `_sql` in `tests/test_rag_retrieval_integration.py`.

```python
"""Integration tests for the re-ingest backfill command (real Postgres)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import jobs as jobs_repo
from app.rag.models import Department, Document, IngestJob
from app.rag.reingest import reingest


def _run(coro_fn):
    """Run `coro_fn(session)` on a fresh NullPool engine + session."""

    async def _go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")


def _seed(session, *, status="ready"):
    """One department + one document in the given status. Returns (code, doc_id)."""
    code = f"ri{uuid.uuid4().hex[:8]}"
    dept = Department(code=code, name="Reingest Test", is_active=True)
    session.add(dept)
    return code, dept


def test_reingest_queues_every_non_archived_document():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        doc = Document(
            id=uuid.uuid4().hex, department_id=dept.id, title="t",
            file_type="docx", status="ready", storage_key="k", content_hash=uuid.uuid4().hex,
        )
        session.add(doc)
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["total"] == 1
    assert stats["queued"] + stats["skipped"] == stats["total"]
    assert stats["queued"] == 1


def test_dry_run_queues_nothing():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        doc = Document(
            id=uuid.uuid4().hex, department_id=dept.id, title="t",
            file_type="docx", status="ready", storage_key="k", content_hash=uuid.uuid4().hex,
        )
        session.add(doc)
        await session.commit()
        stats = await reingest(session, department_code=code, dry_run=True)
        n = (
            await session.execute(
                select(IngestJob).where(IngestJob.document_id == doc.id)
            )
        ).scalars().all()
        return stats, len(n)

    stats, job_count = _run(go)
    assert stats["total"] == 1
    assert job_count == 0


def test_department_filter_restricts_the_set():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        session.add(Document(
            id=uuid.uuid4().hex, department_id=dept.id, title="t",
            file_type="docx", status="ready", storage_key="k", content_hash=uuid.uuid4().hex,
        ))
        await session.commit()
        one = await reingest(session, department_code=code, dry_run=True)
        every = await reingest(session, department_code=None, dry_run=True)
        return one, every

    one, every = _run(go)
    assert one["total"] == 1
    assert every["total"] >= one["total"]


def test_a_document_with_an_active_job_is_skipped_not_raised():
    """JobConflict is expected traffic, not an error: ux_ingest_jobs_active_document
    is a PARTIAL unique index over queued|running, so a document already being
    ingested will pick up the new chunker anyway."""

    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        doc = Document(
            id=uuid.uuid4().hex, department_id=dept.id, title="t",
            file_type="docx", status="ready", storage_key="k", content_hash=uuid.uuid4().hex,
        )
        session.add(doc)
        await session.commit()
        await jobs_repo.enqueue(session, document_id=doc.id)
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["skipped"] == 1
    assert stats["queued"] == 0


def test_archived_documents_are_not_requeued():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        session.add(Document(
            id=uuid.uuid4().hex, department_id=dept.id, title="t",
            file_type="docx", status="archived", storage_key="k", content_hash=uuid.uuid4().hex,
        ))
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["total"] == 0
```

> Check `app/rag/models.py` for `Document`'s actual required columns before running — if the constructor above is missing a non-nullable field, add it rather than making the column nullable.

- [ ] **Step 2: Run to verify they fail**

```bash
.venv/bin/pytest tests/test_rag_reingest_integration.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.rag.reingest'`.

- [ ] **Step 3: Write the command**

Create `app/rag/reingest.py`:

```python
"""Re-queue already-ingested documents so they pick up a new chunker.

Chunking changes only affect documents parsed after the change. This replays
existing ones through the SAME worker path a fresh upload uses: it enqueues an
ingest job and stops. `replace_chunks` is already atomic and re-checks status
under a row lock, and a failed re-ingest of a `ready` document leaves it
`ready` with its previous chunks intact — so this is safe to run on a live
corpus.

    .venv/bin/python -m app.rag.reingest [--department CODE] [--dry-run]

The worker must be running for anything to actually happen.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings
from . import jobs as jobs_repo
from .models import STATUS_ARCHIVED, Department, Document

log = logging.getLogger("rag.reingest")


async def reingest(
    session: AsyncSession, *, department_code: str | None, dry_run: bool
) -> dict[str, int]:
    """Queue an ingest for every non-archived document. Returns a summary."""
    stmt = select(Document).where(Document.status != STATUS_ARCHIVED)
    if department_code:
        stmt = stmt.join(Department, Department.id == Document.department_id).where(
            Department.code == department_code
        )
    documents = list((await session.execute(stmt)).scalars())

    queued = skipped = 0
    for doc in documents:
        if dry_run:
            log.info("would queue %s (%s)", doc.id, doc.title)
            continue
        try:
            await jobs_repo.enqueue(session, document_id=doc.id)
            await session.commit()
            queued += 1
        except jobs_repo.JobConflict:
            # Expected traffic: an ingest is already queued or running for this
            # document. Skipping is correct — that job will use the new chunker.
            skipped += 1

    return {"queued": queued, "skipped": skipped, "total": len(documents)}


async def _main() -> None:  # pragma: no cover - process entrypoint
    parser = argparse.ArgumentParser(description="Re-queue documents for ingestion.")
    parser.add_argument("--department", default=None, help="Department code to limit to.")
    parser.add_argument("--dry-run", action="store_true", help="Show, don't queue.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            stats = await reingest(
                session, department_code=args.department, dry_run=args.dry_run
            )
    finally:
        await engine.dispose()

    verb = "would queue" if args.dry_run else "queued"
    log.info(
        "%s %d of %d document(s); %d skipped (already active)",
        verb, stats["queued"] if not args.dry_run else stats["total"],
        stats["total"], stats["skipped"],
    )
    if not args.dry_run:
        log.info("the ingest worker must be running for these to be processed")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
```

- [ ] **Step 4: Run the tests**

```bash
.venv/bin/pytest tests/test_rag_reingest_integration.py -v
```

Expected: all pass. If they all SKIP, Postgres is not reachable — start it; skips are not evidence.

- [ ] **Step 5: Commit**

```bash
git add app/rag/reingest.py tests/test_rag_reingest_integration.py
git commit -m "feat(rag): reingest command to backfill a chunker change

Replays existing documents through the same worker path a fresh upload uses.
JobConflict is counted as skipped, not raised — a document with an active job
will already use the new chunker."
```

---

### Task 7: Retrieval eval and documentation

**Files:**
- Create: `tests/test_rag_retrieval_eval.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-6

- [ ] **Step 1: Write the eval**

Create `tests/test_rag_retrieval_eval.py`. It needs the embedding model and a real corpus, so it skips when either is unavailable — mirroring `tests/test_rag_embedding_live.py` (read that file first for the skip pattern):

```python
"""Retrieval quality eval — 6 labelled queries.

Baseline measured 2026-08-12, BEFORE this change, for "areas of investment"
at top_k=12 against the nrb department: 2/12 passages substantive, 7/12 Table
of Contents, correct chapter first appearing at rank 10.

Target after: >=10/12 substantive, expected section within the top 5.

Substantive = not front matter, and body (content minus the prepended heading
path) at least 120 chars. Skips when the embedding model or corpus is absent.
"""

CASES = [
    ("areas of investment", "Chapter 4"),
    ("what are the investment limits", "Chapter 5"),
    ("who approves an investment", "Chapter 5"),
    ("investment in government securities", "Chapter 4"),
    ("core considerations before investing", "Chapter 2"),
    ("how is the policy reviewed", "Chapter 7"),
]


def test_retrieval_returns_substantive_passages():
    for query, expected_section in CASES:
        rows = _search(query, top_k=12)
        substantive = [r for r in rows if _is_substantive(r)]
        assert len(substantive) >= 10, (
            f"{query!r}: only {len(substantive)}/12 substantive; "
            f"channels: {[(r.dense_rank, r.lexical_rank) for r in rows[:3]]}"
        )
        top5 = [r.section or "" for r in rows[:5]]
        assert any(expected_section in s for s in top5), (
            f"{query!r}: expected {expected_section} in top 5, got {top5}"
        )
```

with these helpers in the same file:

```python
import asyncio

import pytest

from app.config import get_settings
from app.ollama.client import OllamaClient
from app.rag.embedding import embed_texts
from app.rag.retrieval import search_chunks

DEPARTMENT = "nrb"
MIN_BODY = 120


def _is_substantive(row) -> bool:
    """Front matter and orphan fragments do not count as an answer."""
    section = row.section or ""
    if section.split(" > ", 1)[0].strip().casefold() in {
        "table of contents", "contents", "index"
    }:
        return False
    body = len(row.content) - len(section)
    return body >= MIN_BODY


def _search(query: str, *, top_k: int):
    """Same construction as the live tool (search_department_docs.py:139-162)."""

    async def _go():
        from sqlalchemy import text as sql_text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        s = get_settings()
        engine = create_async_engine(s.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        sql_text("SELECT id FROM departments WHERE code = :c"),
                        {"c": DEPARTMENT},
                    )
                ).fetchall()
            if not rows:
                pytest.skip(f"no {DEPARTMENT} department in this database")
            dept_id = rows[0][0]
        finally:
            await engine.dispose()

        client = OllamaClient(s.ollama_base_url, s.ollama_timeout)
        try:
            vec = (
                await embed_texts(
                    client, [query], mode="query",
                    model=s.rag_embed_model, dim=s.rag_embed_dim, batch_size=1,
                )
            )[0]
        finally:
            await client.aclose()

        return await search_chunks(
            department_id=dept_id, query_text=query, query_vector=vec,
            limit=top_k, candidate_pool=s.rag_candidate_pool,
            rrf_k=s.rag_rrf_k, ef_search=s.rag_hnsw_ef_search,
        )

    try:
        return asyncio.run(_go())
    except pytest.skip.Exception:
        raise
    except Exception as exc:  # noqa: BLE001 - embedding model or DB absent
        pytest.skip(f"live retrieval unavailable: {type(exc).__name__}: {exc}")
```

The failure message in the test already prints the per-channel ranks — that is what Task 5's fields are for, and it is the difference between "retrieval got worse" and "the lexical channel surfaced front matter again".

- [ ] **Step 2: Run the eval**

```bash
.venv/bin/pytest tests/test_rag_retrieval_eval.py -v
```

If it skips, note that in your report. If it runs and fails, record the actual numbers — that is data, not necessarily a bug, and the controller decides.

- [ ] **Step 3: Update CLAUDE.md**

Add to the RAG conventions section, in the established voice (a claim plus the reason it exists):

- `_parse_with_docling` collects `Block`s, then **merges before filtering** — filtering first deletes real content, because a glossary definition is orphaned from its term until the merge runs.
- Merge flushes on section change, **page change** (page_number is citation-bearing for PDFs; DOCX has none so it never fires), table, or `max_chars`.
- Front matter is skipped on the **first** heading-path segment only, so `Chapter 3 > Index of Limits` survives.
- `rag_chunk_min_body_chars=40` is empirically chosen for this corpus, not universal.
- After any chunker change, run `python -m app.rag.reingest` — existing documents keep their old chunks otherwise.

- [ ] **Step 4: Full suite and commit**

```bash
.venv/bin/pytest -q
git add tests/test_rag_retrieval_eval.py CLAUDE.md
git commit -m "test(rag): retrieval quality eval + document the ingest pipeline

Baseline 2/12 substantive; target >=10/12 with the expected section in the
top 5. Failure messages carry the per-channel ranks so a regression says which
channel surfaced the bad passage."
```

---

## Verification (after all tasks)

- [ ] Full suite green: `.venv/bin/pytest -q`
- [ ] Docling still absent from the API import graph: `.venv/bin/pytest tests/test_rag_parsing_docling.py -q`
- [ ] No migration created: `git status --porcelain alembic/` is empty
- [ ] No new dependency: `git diff main -- requirements.txt requirements-worker.txt` is empty
- [ ] **Backfill and re-measure.** With the worker running (`.venv/bin/python -m app.rag.worker`), run `.venv/bin/python -m app.rag.reingest --department nrb`, wait for jobs to finish, then re-run the eval. Record the before/after numbers against the 2/12 baseline — that measurement is the point of the whole change.
