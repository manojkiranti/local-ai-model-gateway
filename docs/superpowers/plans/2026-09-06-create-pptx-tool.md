# create_pptx Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `create_pptx` local tool so the chat model can produce a PowerPoint deck (title slide + bullet/table slides) and return a `/v1/files/{id}` download link, exactly as `create_docx` does for Word.

**Architecture:** One new module `app/tools/local/pptx.py` exporting `SPEC`, following `docx.py` line for line: pure `_validate` returning either a tuple or an `ERROR:` string, a sync `_build_pptx_bytes` run in `asyncio.to_thread`, and `file_store.save` for the per-user file sink. The content model is slide-based (`slides[]` of `{title?, bullets?, table?}`), reusing create_docx's `table` shape. The engine (`registry.py`) is untouched; registration is one line in `LOCAL_TOOLS`.

**Tech Stack:** Python 3.10, `python-pptx>=1.0` (already in `.venv` as 1.0.2, must be pinned in `requirements.txt`), pytest.

**Spec:** Design agreed in chat 2026-09-06 (bounded path, no separate spec file). Summary of the agreed design:

- Create only. No `.pptx` upload/read support in this plan.
- Deck-level `title` (optional title slide), `subtitle` (optional, title slide only), `slides` (required, non-empty), `filename` (default `presentation.pptx`, suffix forced).
- Each slide: at least one of `title`, `bullets` (array of strings, one level), `table` (`{headers?, rows[][]}`, same shape and same validation as create_docx). Bullets render above the table when both are present.
- Caps are module constants, never tool arguments: `MAX_SLIDES = 50`, `MAX_BULLETS_PER_SLIDE = 20`. Exceeding either returns an `ERROR:` string.
- Default python-pptx template. No images, themes, or speaker notes.
- Return string shape: `Created PowerPoint presentation '<name>' (<size> bytes, <n> slide(s)). Download it at: GET /v1/files/<id>`.

## Global Constraints

- Use THIS project's venv: `.venv/bin/python`, `.venv/bin/pytest`. Never a sibling's.
- Tools return strings; errors are `ERROR: ...` strings returned to the loop, never exceptions.
- Files go through `file_store.save(data, filename=, media_type=)` from `app/files/store.py`. Nothing else writes files.
- No new tool argument may raise a resource cap (`MAX_SLIDES`, `MAX_BULLETS_PER_SLIDE` are constants).
- The tool description is the routing prompt. After editing any description, run `scripts/eval_rag_routing.py` (Task 5).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `app/files/store.py` (modify, near line 32) | Add `PPTX_MEDIA_TYPE` beside `DOCX_MEDIA_TYPE`. |
| `app/tools/local/pptx.py` (create) | The tool: `_validate`, `_add_bullets`, `_add_table`, `_build_pptx_bytes`, `_create_pptx`, `SPEC`. |
| `app/tools/local/__init__.py` (modify) | Import `pptx` and append `pptx.SPEC` after `docx.SPEC`. |
| `requirements.txt` (modify, line 15) | Pin `python-pptx>=1.0`. |
| `tests/test_create_pptx.py` (create) | Offline tests: validation, real `.pptx` bytes, text present, Unicode, caps, media type. |
| `CLAUDE.md` (modify) | Tool list line + `LOCAL_TOOLS` count note. |

---

### Task 1: Media type constant and dependency pin

**Files:**
- Modify: `app/files/store.py:32`
- Modify: `requirements.txt:15`

**Interfaces:**
- Produces: `PPTX_MEDIA_TYPE: str` in `app.files.store`, imported by Task 2 and Task 3.

- [ ] **Step 1: Add the constant**

In `app/files/store.py`, directly after the `DOCX_MEDIA_TYPE` line (line 32), add:

```python
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
```

- [ ] **Step 2: Pin the dependency**

In `requirements.txt`, after `python-docx>=1.1` add:

```
python-pptx>=1.0
```

- [ ] **Step 3: Verify the import resolves**

Run: `.venv/bin/python -c "from app.files.store import PPTX_MEDIA_TYPE; import pptx; print(PPTX_MEDIA_TYPE, pptx.__version__)"`
Expected: prints the media type and `1.0.2` (or later).

- [ ] **Step 4: Commit**

```bash
git add app/files/store.py requirements.txt
git commit -m "chore(files): add PPTX media type and pin python-pptx

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Validation (pure) with tests

**Files:**
- Create: `app/tools/local/pptx.py`
- Create: `tests/test_create_pptx.py`

**Interfaces:**
- Produces: `_validate(args: dict) -> tuple[str, str, list[dict], str] | str` returning `(title, subtitle, slides, filename)` or an `ERROR:` string; constants `MAX_SLIDES = 50`, `MAX_BULLETS_PER_SLIDE = 20`.

- [ ] **Step 1: Write the failing validation tests**

Create `tests/test_create_pptx.py`:

```python
"""Offline tests for the create_pptx local tool (python-pptx -> .pptx).

No network: calls the tool fn directly against a temp-configured fallback file
store and asserts (a) a valid .pptx (zip w/ ppt/presentation.xml) is produced
and stored, (b) deck title / slide titles / bullets / table cells land in the
deck, (c) full Unicode survives, (d) validation returns friendly ERROR strings,
and (e) the slide/bullet caps refuse rather than build.
"""

import asyncio
import zipfile

import pytest

from app.files.store import PPTX_MEDIA_TYPE, file_store
from app.tools.local import pptx as pptx_tool


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _run(args):
    return asyncio.run(pptx_tool.SPEC.func(args))


def _link_id(result: str) -> str:
    assert "Download it at: GET /v1/files/" in result, result
    return result.split("/v1/files/")[1].strip().split()[0]


def _slide_texts(result: str) -> list[str]:
    """Reopen the stored file with python-pptx and return every text run, in order."""
    from pptx import Presentation

    record = file_store.get(_link_id(result))
    assert record is not None
    assert record.media_type == PPTX_MEDIA_TYPE
    with zipfile.ZipFile(record.path) as zf:
        assert "ppt/presentation.xml" in zf.namelist()  # it's a real .pptx
    prs = Presentation(record.path)
    texts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.extend(p.text for p in shape.text_frame.paragraphs)
            if shape.has_table:
                for row in shape.table.rows:
                    texts.extend(cell.text for cell in row.cells)
    return texts


# ---- validation -------------------------------------------------------------


def test_missing_slides_is_error():
    assert _run({}).startswith("ERROR: 'slides' is required")
    assert _run({"slides": []}).startswith("ERROR: 'slides' is required")


def test_slide_not_object_is_error():
    assert _run({"slides": ["just text"]}).startswith("ERROR: slides[0] must be an object")


def test_empty_slide_is_error():
    assert _run({"slides": [{}]}).startswith("ERROR: slides[0] needs at least one of")


def test_bullets_must_be_array_of_strings():
    assert _run({"slides": [{"bullets": "one"}]}).startswith("ERROR: slides[0].bullets must be an array")
    assert _run({"slides": [{"bullets": [1, 2]}]}).startswith("ERROR: slides[0].bullets must be an array")


def test_bad_table_is_error():
    assert _run({"slides": [{"table": "x"}]}).startswith("ERROR: slides[0].table must be an object")
    assert _run({"slides": [{"table": {"rows": []}}]}).startswith("ERROR: slides[0].table.rows must be")
    assert _run({"slides": [{"table": {"rows": ["a"]}}]}).startswith("ERROR: slides[0].table.rows[0] must be")
    assert _run({"slides": [{"table": {"rows": [["a"]], "headers": "h"}}]}).startswith(
        "ERROR: slides[0].table.headers must be"
    )


def test_too_many_slides_is_refused():
    slides = [{"title": f"s{i}"} for i in range(pptx_tool.MAX_SLIDES + 1)]
    result = _run({"slides": slides})
    assert result.startswith("ERROR:") and str(pptx_tool.MAX_SLIDES) in result


def test_too_many_bullets_is_refused():
    bullets = [f"b{i}" for i in range(pptx_tool.MAX_BULLETS_PER_SLIDE + 1)]
    result = _run({"slides": [{"title": "x", "bullets": bullets}]})
    assert result.startswith("ERROR: slides[0].bullets") and str(pptx_tool.MAX_BULLETS_PER_SLIDE) in result
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_create_pptx.py -v`
Expected: collection error `ModuleNotFoundError`/`ImportError` on `app.tools.local.pptx`.

- [ ] **Step 3: Write the module with validation and a stub builder**

Create `app/tools/local/pptx.py`:

```python
"""Local tool: create_pptx (structured slide deck -> .pptx download link).

Slide-based content model — a deck's unit is a slide, so the model supplies
`slides[]` of {title?, bullets?, table?} plus an optional deck `title`/`subtitle`
that becomes a title slide. The `table` shape is create_docx's exactly, so the
model reuses what it already knows. Rendered with python-pptx on its default
template (full Unicode, no fonts or system libraries). A file tool, so it flows
through the per-user file sink like create_docx/create_pdf.

Caps are module constants, not arguments: the model must not be able to raise
the limit that bounds the file it asks the process to build.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any

from ...files.store import PPTX_MEDIA_TYPE, file_store
from .base import LocalToolSpec

MAX_SLIDES = 50
MAX_BULLETS_PER_SLIDE = 20

_DEFAULT_FILENAME = "presentation.pptx"


def _validate(args: dict[str, Any]) -> tuple[str, str, list[dict], str] | str:
    """Return (title, subtitle, slides, filename) on success, or an ERROR: string."""
    raw_slides = args.get("slides")
    if not isinstance(raw_slides, list) or not raw_slides:
        return "ERROR: 'slides' is required and must be a non-empty array of {title?, bullets?, table?}."
    if len(raw_slides) > MAX_SLIDES:
        return f"ERROR: too many slides ({len(raw_slides)}); the limit is {MAX_SLIDES}. Split the deck."

    for idx, slide in enumerate(raw_slides):
        if not isinstance(slide, dict):
            return f"ERROR: slides[{idx}] must be an object with title/bullets/table."
        bullets = slide.get("bullets")
        table = slide.get("table")
        if not (slide.get("title") or bullets or table is not None):
            return f"ERROR: slides[{idx}] needs at least one of 'title', 'bullets', or 'table'."
        if bullets is not None:
            if not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets):
                return f"ERROR: slides[{idx}].bullets must be an array of strings."
            if len(bullets) > MAX_BULLETS_PER_SLIDE:
                return (
                    f"ERROR: slides[{idx}].bullets has {len(bullets)} items; "
                    f"the limit is {MAX_BULLETS_PER_SLIDE} per slide. Split across slides."
                )
        if table is not None:
            if not isinstance(table, dict):
                return f"ERROR: slides[{idx}].table must be an object with 'rows' (and optional 'headers')."
            rows = table.get("rows")
            if not isinstance(rows, list) or not rows:
                return f"ERROR: slides[{idx}].table.rows must be a non-empty array of rows."
            for ridx, row in enumerate(rows):
                if not isinstance(row, (list, tuple)):
                    return f"ERROR: slides[{idx}].table.rows[{ridx}] must be an array of cell values."
            headers = table.get("headers")
            if headers is not None and not isinstance(headers, list):
                return f"ERROR: slides[{idx}].table.headers must be an array of column names."

    title = str(args.get("title") or "")
    subtitle = str(args.get("subtitle") or "")
    filename = str(args.get("filename") or _DEFAULT_FILENAME)
    if not filename.lower().endswith(".pptx"):
        filename += ".pptx"
    return title, subtitle, raw_slides, filename


def _build_pptx_bytes(title: str, subtitle: str, slides: list[dict]) -> bytes:
    """Render the deck with python-pptx. Sync — run in a thread. (Filled in Task 3.)"""
    raise NotImplementedError


async def _create_pptx(args: dict[str, Any]) -> str:
    validated = _validate(args)
    if isinstance(validated, str):  # an ERROR: message
        return validated
    title, subtitle, slides, filename = validated

    try:
        data = await asyncio.to_thread(_build_pptx_bytes, title, subtitle, slides)
    except Exception as exc:  # noqa: BLE001 - report back, don't raise into the loop
        return f"ERROR: failed to build PPTX: {exc}"

    record = await file_store.save(data, filename=filename, media_type=PPTX_MEDIA_TYPE)
    # Same string shape as create_excel/create_pdf/create_docx so the frontend parses it identically.
    return (
        f"Created PowerPoint presentation '{record.filename}' "
        f"({record.size} bytes, {len(slides)} slide(s)). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_pptx",
    description=(
        "Create a PowerPoint (.pptx) slide deck and return a download link. Use this "
        "when the user asks for a presentation, slides, or a deck. Provide 'slides' "
        "(array of {title?, bullets?, table?}) and optionally a deck 'title' and "
        "'subtitle' (rendered as a title slide) and a 'filename'. Each slide may have "
        "a 'title', 'bullets' (array of short strings, one level) and/or a 'table' "
        "({headers?, rows[][]}). Full Unicode is supported. Keep decks under "
        f"{MAX_SLIDES} slides and {MAX_BULLETS_PER_SLIDE} bullets per slide. For a "
        "document rather than slides use create_docx or create_pdf."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Optional deck title (makes a title slide)."},
            "subtitle": {"type": "string", "description": "Optional subtitle for the title slide."},
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Optional slide title."},
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional bullet points, one string each.",
                        },
                        "table": {
                            "type": "object",
                            "properties": {
                                "headers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional column headers.",
                                },
                                "rows": {
                                    "type": "array",
                                    "items": {"type": "array"},
                                    "description": "Rows of cell values; each row is an array.",
                                },
                            },
                            "required": ["rows"],
                            "description": "Optional simple table.",
                        },
                    },
                },
                "description": "Slides in order; each needs at least a title, bullets, or a table.",
            },
            "filename": {
                "type": "string",
                "description": "Output file name, e.g. 'review.pptx' (default 'presentation.pptx').",
            },
        },
        "required": ["slides"],
    },
    func=_create_pptx,
)
```

- [ ] **Step 4: Run the validation tests to verify they pass**

Run: `.venv/bin/pytest tests/test_create_pptx.py -v`
Expected: all 7 tests PASS (none of them reach the builder).

- [ ] **Step 5: Commit**

```bash
git add app/tools/local/pptx.py tests/test_create_pptx.py
git commit -m "feat(tools): create_pptx validation and schema

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Rendering with python-pptx

**Files:**
- Modify: `app/tools/local/pptx.py` (replace the `_build_pptx_bytes` stub; add `_add_bullets`, `_add_table`)
- Modify: `tests/test_create_pptx.py` (append rendering tests)

**Interfaces:**
- Consumes: `_validate` from Task 2, `PPTX_MEDIA_TYPE` from Task 1.
- Produces: `_build_pptx_bytes(title: str, subtitle: str, slides: list[dict]) -> bytes`.

- [ ] **Step 1: Append the failing rendering tests**

Append to `tests/test_create_pptx.py`:

```python
# ---- rendering --------------------------------------------------------------


def test_title_slide_and_bullets_present():
    result = _run(
        {
            "title": "Quarterly Review",
            "subtitle": "Finance",
            "slides": [
                {"title": "Highlights", "bullets": ["Revenue up", "Costs flat"]},
                {"bullets": ["A slide with no title"]},
            ],
        }
    )
    assert "3 slide(s)" not in result  # the count reports CONTENT slides only
    assert "2 slide(s)" in result
    texts = _slide_texts(result)
    assert "Quarterly Review" in texts and "Finance" in texts
    assert "Highlights" in texts
    assert "Revenue up" in texts and "Costs flat" in texts
    assert "A slide with no title" in texts


def test_no_deck_title_means_no_title_slide():
    from pptx import Presentation

    result = _run({"slides": [{"title": "Only"}]})
    record = file_store.get(_link_id(result))
    assert len(Presentation(record.path).slides) == 1


def test_table_slide_present():
    result = _run(
        {
            "slides": [
                {
                    "title": "Numbers",
                    "table": {"headers": ["Month", "Sales"], "rows": [["Jan", 10], ["Feb", 20]]},
                }
            ]
        }
    )
    texts = _slide_texts(result)
    assert "Numbers" in texts
    assert "Month" in texts and "Sales" in texts and "Jan" in texts and "20" in texts


def test_bullets_and_table_on_one_slide():
    result = _run(
        {
            "slides": [
                {
                    "title": "Both",
                    "bullets": ["point one"],
                    "table": {"rows": [["a", "b"]]},
                }
            ]
        }
    )
    texts = _slide_texts(result)
    assert "point one" in texts and "a" in texts and "b" in texts


def test_ragged_table_rows_are_padded():
    result = _run({"slides": [{"table": {"headers": ["x", "y", "z"], "rows": [["1"], ["1", "2", "3", "4"]]}}]})
    assert result.startswith("Created"), result
    texts = _slide_texts(result)
    assert "4" in texts  # the widest row sets the column count


def test_full_unicode_preserved():
    result = _run({"title": "Café 🚀 “quotes”", "slides": [{"bullets": ["नेपाल राष्ट्र बैंक", "smile 😀"]}]})
    texts = _slide_texts(result)
    assert "Café 🚀 “quotes”" in texts
    assert "नेपाल राष्ट्र बैंक" in texts and "smile 😀" in texts


def test_filename_suffix_is_forced():
    result = _run({"slides": [{"title": "x"}], "filename": "deck"})
    assert "'deck.pptx'" in result
    result = _run({"slides": [{"title": "x"}]})
    assert "'presentation.pptx'" in result
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_create_pptx.py -v -k "present or title_slide or unicode or padded or suffix or one_slide"`
Expected: FAIL with `ERROR: failed to build PPTX: ` (the `NotImplementedError` is caught and reported).

- [ ] **Step 3: Implement the renderer**

In `app/tools/local/pptx.py`, replace the `_build_pptx_bytes` stub with the three functions below. Layout indexes are python-pptx's default template: 0 = Title Slide, 1 = Title and Content, 5 = Title Only.

```python
_LAYOUT_TITLE = 0
_LAYOUT_TITLE_AND_CONTENT = 1
_LAYOUT_TITLE_ONLY = 5


def _add_bullets(slide, bullets: list[str]) -> None:
    """Fill the content placeholder (index 1 on the Title-and-Content layout)."""
    body = slide.placeholders[1].text_frame
    body.clear()  # leaves one empty paragraph
    for i, text in enumerate(bullets):
        paragraph = body.paragraphs[0] if i == 0 else body.add_paragraph()
        paragraph.text = text
        paragraph.level = 0


def _add_table(slide, table: dict, top_emu: int) -> None:
    from pptx.util import Emu

    headers = table.get("headers")
    rows = table["rows"]
    ncols = max([len(headers or [])] + [len(r) for r in rows]) or 1
    nrows = len(rows) + (1 if headers else 0)

    prs_width = slide.part.package.presentation_part.presentation.slide_width
    margin = Emu(int(prs_width * 0.05))
    width = Emu(prs_width - 2 * margin)
    row_h = Emu(370_000)  # ~1 cm; python-pptx sizes rows to content anyway
    shape = slide.shapes.add_table(nrows, ncols, margin, Emu(top_emu), width, Emu(row_h * nrows))
    grid = shape.table

    r = 0
    if headers:
        for c in range(ncols):
            grid.cell(0, c).text = str(headers[c]) if c < len(headers) else ""
        r = 1
    for row in rows:
        for c in range(ncols):
            grid.cell(r, c).text = str(row[c]) if c < len(row) else ""
        r += 1


def _build_pptx_bytes(title: str, subtitle: str, slides: list[dict]) -> bytes:
    """Render the deck with python-pptx. Sync — run in a thread."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()

    if title:
        ts = prs.slides.add_slide(prs.slide_layouts[_LAYOUT_TITLE])
        ts.shapes.title.text = title
        ts.placeholders[1].text = subtitle  # empty string leaves the placeholder blank

    for spec in slides:
        bullets = spec.get("bullets") or []
        table = spec.get("table")
        layout = _LAYOUT_TITLE_AND_CONTENT if bullets else _LAYOUT_TITLE_ONLY
        slide = prs.slides.add_slide(prs.slide_layouts[layout])
        slide.shapes.title.text = str(spec.get("title") or "")

        table_top = int(prs.slide_height * 0.25)
        if bullets:
            _add_bullets(slide, bullets)
            if table is not None:
                # Shrink the bullet box to the upper part so the table fits below it.
                body = slide.placeholders[1]
                body.top = Emu(int(prs.slide_height * 0.22))
                body.height = Emu(int(prs.slide_height * 0.30))
                table_top = int(prs.slide_height * 0.55)
        if table is not None:
            _add_table(slide, table, table_top)

    buffer = BytesIO()
    prs.save(buffer)
    return buffer.getvalue()
```

- [ ] **Step 4: Run the whole test file**

Run: `.venv/bin/pytest tests/test_create_pptx.py -v`
Expected: all 14 tests PASS.

- [ ] **Step 5: Eyeball one real deck**

Run:
```bash
.venv/bin/python - <<'EOF'
import asyncio
from app.files.store import file_store
from app.tools.local import pptx
file_store.configure("/tmp/claude-1000/-home-manoj-newlaptop-projects-python-local-ai-model-gateway/78cd678d-7d40-49a2-85ec-cb2b4fb3a237/scratchpad")
print(asyncio.run(pptx.SPEC.func({"title": "Demo", "subtitle": "NIC", "slides": [
  {"title": "Points", "bullets": ["one", "two"], "table": {"headers": ["a","b"], "rows": [[1,2]]}}]})))
EOF
```
Open the printed file with LibreOffice (`libreoffice --headless --convert-to pdf <path>` then view) and confirm the table sits below the bullets and nothing overflows the slide. Adjust the `0.22/0.30/0.55` fractions only if it does.

- [ ] **Step 6: Commit**

```bash
git add app/tools/local/pptx.py tests/test_create_pptx.py
git commit -m "feat(tools): render create_pptx decks with python-pptx

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Register the tool

**Files:**
- Modify: `app/tools/local/__init__.py`
- Modify: `tests/test_create_pptx.py` (append one test)

- [ ] **Step 1: Write the failing registration test**

Append to `tests/test_create_pptx.py`:

```python
# ---- registration -----------------------------------------------------------


def test_tool_is_registered_once():
    from app.tools.local import LOCAL_TOOLS

    names = [t.name for t in LOCAL_TOOLS]
    assert names.count("create_pptx") == 1
    assert names.index("create_pptx") == names.index("create_docx") + 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_create_pptx.py::test_tool_is_registered_once -v`
Expected: FAIL, `create_pptx` not in list.

- [ ] **Step 3: Register**

In `app/tools/local/__init__.py`, add `pptx,` to the `from . import (...)` block (alphabetically, after `pdf,`), and in `LOCAL_TOOLS` insert `pptx.SPEC,` directly after `docx.SPEC,`.

- [ ] **Step 4: Run the new test plus the existing tool-suite tests**

Run: `.venv/bin/pytest tests/test_create_pptx.py tests/test_create_docx.py tests/test_tool_filter.py tests/test_excel_read_tools.py -v`
Expected: all PASS. If `test_tool_filter.py` or any other test asserts an exact tool count, update that count in the test to 21 and note it in the commit message.

- [ ] **Step 5: Check the schema loads through the registry and the HTTP list**

Run: `.venv/bin/python -c "from app.tools.local import LOCAL_TOOLS; import json; s=[t for t in LOCAL_TOOLS if t.name=='create_pptx'][0]; print(len(json.dumps(s.parameters)), 'chars of schema')"`
Expected: prints a size in the same range as `create_docx` (roughly 900 to 1300 chars).

- [ ] **Step 6: Commit**

```bash
git add app/tools/local/__init__.py tests/
git commit -m "feat(tools): register create_pptx in LOCAL_TOOLS

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Routing eval and docs

**Files:**
- Modify: `CLAUDE.md` (the `files/` layout bullet and the `LOCAL_TOOLS` count in the `num_ctx` gotcha)
- Possibly modify: `app/tools/local/pptx.py` description only, if the eval regresses.

- [ ] **Step 1: Run the routing eval, with and without the new tool**

Requires the model server reachable per `docs/server-and-models.md`. Run both:

```bash
MCP_SERVER_URL= EVAL_REPEAT=3 .venv/bin/python scripts/eval_rag_routing.py
MCP_SERVER_URL= EVAL_REPEAT=3 DROP_TOOLS=create_pptx .venv/bin/python scripts/eval_rag_routing.py
```

Expected: the pass counts per case are equal across the two runs (within `BORDERLINE`). If a case drops only when `create_pptx` is present, shorten or reword the description's first sentence and re-run; do not touch other tools' descriptions in this plan. If the server is unreachable, record that the eval was NOT run in the commit message and in the summary to the user; do not claim it passed.

- [ ] **Step 2: Update CLAUDE.md**

In the `## Layout` section, the `files/` bullet ends with `feeds create_excel/html/chart/pdf/csv/docx and inspect_excel/...`. Change it to `feeds create_excel/html/chart/pdf/csv/docx/pptx and inspect_excel/...`.

In the `num_ctx` gotcha, replace the sentence beginning `` `LOCAL_TOOLS` is now **17** `` so it reads:

```
`LOCAL_TOOLS` is now **21** (`read_document`, `read_image`, `edit_excel`,
`nepali_date`, `read_department_doc`, `get_nrb_forex` and `create_pptx` landed
after that measurement) — the token figure has not been re-measured since, so
treat it as a floor, not the current count.
```

Add one bullet under `## Conventions / gotchas`, after the `create_docx`/`create_pdf`-adjacent `edit_excel` bullet:

```
- **`create_pptx` is slide-based, not section-based, and its caps are constants.**
  A deck's unit is a slide, so the schema is `slides[] of {title?, bullets?, table?}`
  (the `table` shape is create_docx's, reused on purpose) with an optional deck
  `title`/`subtitle` that becomes a title slide. `MAX_SLIDES`/`MAX_BULLETS_PER_SLIDE`
  are module constants — the `edit_excel` cell-cap rule — so the model cannot raise
  the bound on the file it asks the process to build. Layout indexes 0/1/5 are the
  default python-pptx template's; a custom template would renumber them.
```

- [ ] **Step 3: Run the full offline suite**

Run: `.venv/bin/pytest -q -x --ignore=tests/test_rag_reingest_integration.py`
Expected: passes (the ignored file is the known-broken test recorded in CLAUDE.md). Compare the skip count to `git stash; .venv/bin/pytest -q | tail -1; git stash pop` if anything looks off; skips must not have grown.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md app/tools/local/pptx.py
git commit -m "docs: record create_pptx tool and re-run routing eval

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** create-only (no upload changes anywhere) ✔; slide model with `title/subtitle/slides/filename` (Task 2) ✔; table shape reused (Task 2/3) ✔; bullets above table (Task 3) ✔; caps as constants with ERROR (Task 2) ✔; default template, no images/notes (Task 3) ✔; return string shape (Task 2) ✔; `PPTX_MEDIA_TYPE` (Task 1) ✔; registration after `create_docx` (Task 4) ✔; routing eval + CLAUDE.md (Task 5) ✔.
- **Placeholders:** none. The only deferred body is the Task 2 stub, which Task 3 replaces with full code.
- **Type consistency:** `_validate` returns `(title, subtitle, slides, filename)` and `_create_pptx` unpacks four; `_build_pptx_bytes(title, subtitle, slides)` matches its call in `_create_pptx`; `_add_table(slide, table, top_emu)` matches its call.
