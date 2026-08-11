# read_document Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user upload a `.pdf`, `.docx`, `.txt`, `.md` or `.json` file, attach it to a chat turn, and have the model read its text through one line-paged tool.

**Architecture:** A new pure module `app/files/documents.py` normalizes any supported document to a list of text lines (parallel to the existing `readers.py` for spreadsheets). A tiny `app/files/ingest.py` dispatches "which family is this file" so neither the upload route nor turn-open branches on extension. A thin tool adapter `app/tools/local/read_document.py` holds all policy: owner-scoping, paging, header-first metadata, and the scanned-PDF error. No DB migration; documents reuse `generated_files` with `source=uploaded`.

**Tech Stack:** Python 3.10, `pypdf` (new), `python-docx` + `fpdf2` + `openpyxl` + Pillow (already installed), FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-11-read-document-design.md`

## Global Constraints

- **Use this project's venv for everything:** `.venv/bin/python`, `.venv/bin/pip`, `.venv/bin/pytest`. Never a sibling's.
- **New dependency is exactly one:** `pypdf>=6.0`. Do not add `PyMuPDF`, `pdfplumber`, `pdfminer.six`, or `pypdfium2`.
- **Docling must never be imported by the API.** No changes to `app/rag/`, the worker, `ingest_jobs`, or any Alembic migration in this plan.
- **OCR is out of scope.** A scanned PDF is *accepted at upload* and reported at read time; it is never OCR'd and never rejected on upload for lacking a text layer.
- **`app/files/documents.py` is pure** — no DB, no HTTP, no contextvars. It reports facts; the tool decides policy.
- **`app/tools/registry.py` never changes.** Registering a tool = a module plus one line in `LOCAL_TOOLS`.
- **Exact strings that tests assert on (copy verbatim):**
  - Scanned PDF: `ERROR: this PDF appears to contain scanned images with no text layer — OCR is not available yet.`
  - Blank page marker: `[page 4] (no extractable text — likely a scanned image)` (page number varies)
  - Spreadsheet pointer: `ERROR: this is a spreadsheet — use inspect_excel / read_excel instead.`
  - Unknown/foreign id: `ERROR: no such file (unknown id, or you don't own it).`
  - Password-protected: `ERROR: this PDF is password-protected — it cannot be read.`
  - Note the em dash `—` (U+2014) in all of the above, not a hyphen.
- **Metadata leads the tool result.** Page counts, `TRUNCATED:`, `PARTIAL:` and the scanned-page count go in the first lines, before the body. `agent/loop.py` cuts results at `MAX_TOOL_RESULT_CHARS` (8000) from the end.
- **Truncate on whole logical lines only.** The last emitted line is always complete, so the reported `start_line=N+1` resumes at exactly the first line the model did not receive.

---

## File Structure

| File | Responsibility |
|---|---|
| `requirements.txt` | Modify — add `pypdf>=6.0` |
| `app/files/documents.py` | **Create** — pure: any document → `DocumentText{kind, lines, pages, text_pages, pages_skipped}` + `summarize_document` |
| `app/files/ingest.py` | **Create** — format dispatch: `SPREADSHEET_EXTS`, `DOCUMENT_EXTS`, `UPLOAD_TYPES`, `summarize(path)` |
| `app/tools/local/read_document.py` | **Create** — the tool: owner-scoping, paging, header-first output, policy errors |
| `app/tools/local/__init__.py` | Modify — import + one `LOCAL_TOOLS` entry |
| `app/files/router.py` | Modify — allowlist, docx zip-bomb guard, threaded parse-check |
| `app/history/service.py` | Modify — one-line swap to `ingest.summarize` |
| `tests/test_documents.py` | **Create** — the pure reader, per format |
| `tests/test_read_document_tool.py` | **Create** — the tool fn, offline via the in-memory store |
| `tests/test_document_upload.py` | **Create** — upload route integration |
| `tests/test_document_eval.py` | **Create** — 8 deterministic labelled eval cases |
| `tests/test_excel_read_tools.py` | Modify — extend the description-routing lock |

---

### Task 1: `documents.py` — text formats (.txt, .md, .json)

Establishes the module, the `DocumentText` type and the dispatch spine. PDF and DOCX slot in afterwards.

**Files:**
- Modify: `requirements.txt`
- Create: `app/files/documents.py`
- Test: `tests/test_documents.py`

**Interfaces:**
- Consumes: `readers.ReadError` from `app/files/readers.py:34`
- Produces:
  - `DOCUMENT_EXTS: set[str]`, `MAX_PDF_PAGES: int = 500`
  - `class EncryptedDocument(ReadError)`
  - `@dataclass DocumentText{kind: str, lines: list[str], pages: int|None, text_pages: int|None, pages_skipped: int|None}`
  - `read_lines(path: Path) -> DocumentText`

- [ ] **Step 1: Add the dependency**

In `requirements.txt`, after the `python-docx>=1.1` line:

```
pypdf>=6.0
```

Then install it:

```bash
.venv/bin/pip install 'pypdf>=6.0'
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_documents.py`:

```python
"""Offline tests for the pure document reader (no DB, no HTTP)."""

from __future__ import annotations

import json

import pytest

from app.files import documents
from app.files.readers import ReadError


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_txt_splits_into_lines(tmp_path):
    p = _write(tmp_path, "a.txt", "first\nsecond\nthird")
    doc = documents.read_lines(p)
    assert doc.kind == "Text file"
    assert doc.lines == ["first", "second", "third"]
    assert doc.pages is None


def test_md_passes_through_verbatim(tmp_path):
    p = _write(tmp_path, "a.md", "# Title\n\n- one\n- two\n\n```py\nx = 1\n```")
    doc = documents.read_lines(p)
    assert doc.kind == "Markdown"
    assert "# Title" in doc.lines
    assert "x = 1" in doc.lines


def test_txt_with_undecodable_byte_does_not_crash(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_bytes(b"ok\n\xff\xfe bad bytes\n")
    doc = documents.read_lines(p)
    assert doc.lines[0] == "ok"
    assert len(doc.lines) == 2  # replacement chars, no exception


def test_json_is_pretty_printed(tmp_path):
    p = _write(tmp_path, "a.json", json.dumps({"a": {"b": [1, 2]}}))
    doc = documents.read_lines(p)
    assert doc.kind == "JSON"
    assert doc.lines[0] == "{"
    assert any(line.startswith('  "a"') for line in doc.lines)


def test_invalid_json_falls_back_to_raw_text(tmp_path):
    p = _write(tmp_path, "bad.json", '{"a": 1,,,}')
    doc = documents.read_lines(p)
    assert doc.kind == "JSON (unparsed)"
    assert doc.lines == ['{"a": 1,,,}']


def test_unsupported_extension_raises(tmp_path):
    p = _write(tmp_path, "a.rtf", "hi")
    with pytest.raises(ReadError):
        documents.read_lines(p)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_documents.py -v
```

Expected: collection error / FAIL — `ModuleNotFoundError: No module named 'app.files.documents'`.

- [ ] **Step 4: Write the implementation**

Create `app/files/documents.py`:

```python
"""Normalize an uploaded document into a flat list of text LINES.

Pure module — no DB, no HTTP. The upload route (parse check + summary) and the
`read_document` tool both go through here, so every supported format behaves
identically downstream.

Design rule: this module reports FACTS and raises only when a file genuinely
cannot be parsed. It makes no policy decisions — notably a scanned PDF returns
normally with `text_pages == 0`, and the tool decides that means "no OCR".
Caps on how much reaches the model live in the tool, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .readers import ReadError

DOCUMENT_EXTS = {".pdf", ".docx", ".txt", ".md", ".json"}

# A hard bound on extraction work for one PDF. Beyond this we stop and SAY so
# (see DocumentText.pages_skipped) rather than refusing the file.
MAX_PDF_PAGES = 500


class EncryptedDocument(ReadError):
    """The file needs a password we don't have (empty password already tried)."""


@dataclass
class DocumentText:
    kind: str
    lines: list[str]
    # PDF-only; None for every other format.
    pages: Optional[int] = None
    text_pages: Optional[int] = None       # pages READ that produced text
    pages_skipped: Optional[int] = None    # pages beyond MAX_PDF_PAGES


def _decode(path: Path) -> str:
    """Bytes -> str, never raising. utf-8-sig strips a BOM when present and is
    plain utf-8 otherwise; errors='replace' means a binary file renamed .txt
    degrades to mojibake instead of crashing the reader."""
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _read_text(path: Path, ext: str) -> DocumentText:
    kind = "Markdown" if ext == ".md" else "Text file"
    return DocumentText(kind=kind, lines=_decode(path).splitlines())


def _read_json(path: Path) -> DocumentText:
    text = _decode(path)
    try:
        parsed = json.loads(text)
    except ValueError:
        # Deliberately NOT an error: near-valid JSON is still readable, and the
        # kind tells the model it is looking at raw text.
        return DocumentText(kind="JSON (unparsed)", lines=text.splitlines())
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return DocumentText(kind="JSON", lines=pretty.splitlines())


def read_lines(path: Path) -> DocumentText:
    """Any supported document -> its text as lines. Raises ReadError for an
    unsupported extension or an unparseable file."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".json":
        return _read_json(path)
    if ext in (".txt", ".md"):
        return _read_text(path, ext)
    raise ReadError(f"unsupported document type '{ext}'")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_documents.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt app/files/documents.py tests/test_documents.py
git commit -m "feat(files): pure document reader for .txt/.md/.json

Adds app/files/documents.py, parallel to readers.py. Reports facts and makes
no policy decisions; caps live in the calling tool. pypdf added to
requirements ahead of the PDF reader."
```

---

### Task 2: `documents.py` — `.docx`

**Files:**
- Modify: `app/files/documents.py`
- Test: `tests/test_documents.py`

**Interfaces:**
- Consumes: `DocumentText`, `ReadError`, `read_lines` dispatch from Task 1
- Produces: `read_lines` now handles `.docx`; headings prefixed `# `, tables as `a | b | c` rows

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_documents.py`:

```python
def _make_docx(tmp_path, name="a.docx"):
    from docx import Document

    doc = Document()
    doc.add_heading("Eligibility", level=1)
    doc.add_paragraph("Applicants must be resident.")
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "name"
    table.cell(0, 1).text = "min"
    table.cell(0, 2).text = "max"
    table.cell(1, 0).text = "term"
    table.cell(1, 1).text = "1"
    table.cell(1, 2).text = "30"
    doc.add_paragraph("End matter.")
    p = tmp_path / name
    doc.save(str(p))
    return p


def test_docx_heading_body_and_table(tmp_path):
    doc = documents.read_lines(_make_docx(tmp_path))
    assert doc.kind == "Word document"
    assert "# Eligibility" in doc.lines
    assert "Applicants must be resident." in doc.lines
    assert "name | min | max" in doc.lines
    assert "term | 1 | 30" in doc.lines


def test_docx_preserves_document_order(tmp_path):
    doc = documents.read_lines(_make_docx(tmp_path))
    heading = doc.lines.index("# Eligibility")
    table_row = doc.lines.index("name | min | max")
    tail = doc.lines.index("End matter.")
    assert heading < table_row < tail


def test_corrupt_docx_raises_read_error(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(ReadError):
        documents.read_lines(p)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_documents.py -k docx -v
```

Expected: FAIL — `ReadError: unsupported document type '.docx'`.

- [ ] **Step 3: Write the implementation**

In `app/files/documents.py`, add after `_read_json`:

```python
def _iter_docx_blocks(doc):
    """Yield Paragraph and Table objects in DOCUMENT ORDER.

    python-docx exposes doc.paragraphs and doc.tables as separate flat lists,
    which loses their relative position — a table would drift to the end of the
    output. Walking the body XML is the only way to keep the reading order the
    author intended.
    """
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield Table(child, doc)


def _read_docx(path: Path) -> DocumentText:
    from docx import Document
    from docx.table import Table

    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - any docx/zip failure is a ReadError
        raise ReadError(f"could not read the Word document: {exc}") from exc

    lines: list[str] = []
    for block in _iter_docx_blocks(doc):
        if isinstance(block, Table):
            lines.append("")
            for row in block.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            lines.append("")
            continue
        text = block.text.strip()
        style = getattr(getattr(block, "style", None), "name", "") or ""
        lines.append(f"# {text}" if style.startswith("Heading") and text else text)
    return DocumentText(kind="Word document", lines=lines)
```

And in `read_lines`, add the branch above the `raise`:

```python
    if ext == ".docx":
        return _read_docx(path)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_documents.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/files/documents.py tests/test_documents.py
git commit -m "feat(files): read .docx into lines, preserving document order

Walks the body XML rather than doc.paragraphs + doc.tables, which are separate
flat lists that would float every table to the end of the output."
```

---

### Task 3: `documents.py` — `.pdf`

The core of the slice. A scanned PDF must return normally with `text_pages == 0`; only corrupt and password-protected files raise.

**Files:**
- Modify: `app/files/documents.py`
- Test: `tests/test_documents.py`

**Interfaces:**
- Consumes: `DocumentText`, `EncryptedDocument`, `ReadError`, `MAX_PDF_PAGES` from Task 1
- Produces: `read_lines` handles `.pdf`, populating `pages`, `text_pages`, `pages_skipped`; page markers `[page N]`; blank-page markers

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_documents.py`:

```python
from io import BytesIO


def _text_pdf_bytes(pages: list[str]) -> bytes:
    """A real PDF with a text layer, one page per string."""
    from fpdf import FPDF

    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, body)
    return bytes(pdf.output())


def _image_only_pdf_bytes(tmp_path, n_pages: int) -> bytes:
    """A PDF whose pages contain ONLY an image — i.e. what a scan looks like."""
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "block.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for _ in range(n_pages):
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=50)
    return bytes(pdf.output())


def _write_bytes(tmp_path, name: str, raw: bytes):
    p = tmp_path / name
    p.write_bytes(raw)
    return p


def test_pdf_marks_each_page_in_order(tmp_path):
    p = _write_bytes(tmp_path, "a.pdf", _text_pdf_bytes(["Alpha page", "Beta page", "Gamma page"]))
    doc = documents.read_lines(p)
    assert doc.kind == "PDF"
    assert doc.pages == 3
    assert doc.text_pages == 3
    assert doc.pages_skipped == 0
    assert doc.lines.index("[page 1]") < doc.lines.index("[page 2]") < doc.lines.index("[page 3]")
    assert any("Alpha page" in line for line in doc.lines)


def test_pdf_blank_page_gets_a_marker_not_silence(tmp_path):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "b.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, "Readable first page")
    pdf.add_page()                      # page 2: image only
    pdf.image(str(img), x=10, y=10, w=50)
    p = _write_bytes(tmp_path, "mixed.pdf", bytes(pdf.output()))

    doc = documents.read_lines(p)
    assert doc.pages == 2
    assert doc.text_pages == 1
    assert "[page 2] (no extractable text — likely a scanned image)" in doc.lines


def test_fully_scanned_pdf_returns_normally_with_zero_text_pages(tmp_path):
    """Policy (the OCR error) belongs to the tool, not the reader."""
    p = _write_bytes(tmp_path, "scan.pdf", _image_only_pdf_bytes(tmp_path, 3))
    doc = documents.read_lines(p)
    assert doc.pages == 3
    assert doc.text_pages == 0
    assert len(doc.lines) == 3  # one marker per page, nothing else


def test_pdf_page_cap_reports_what_it_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    p = _write_bytes(tmp_path, "long.pdf", _text_pdf_bytes(["one", "two", "three", "four"]))
    doc = documents.read_lines(p)
    assert doc.pages == 4
    assert doc.pages_skipped == 2
    assert "[page 2]" in doc.lines
    assert "[page 3]" not in doc.lines


def test_password_protected_pdf_raises_encrypted(tmp_path):
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(_text_pdf_bytes(["secret"])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buf = BytesIO()
    writer.write(buf)
    p = _write_bytes(tmp_path, "locked.pdf", buf.getvalue())

    with pytest.raises(documents.EncryptedDocument):
        documents.read_lines(p)


def test_corrupt_pdf_raises_read_error(tmp_path):
    p = _write_bytes(tmp_path, "broken.pdf", b"%PDF-1.4\nthis is not a pdf body")
    with pytest.raises(ReadError):
        documents.read_lines(p)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_documents.py -k pdf -v
```

Expected: FAIL — `ReadError: unsupported document type '.pdf'`.

- [ ] **Step 3: Write the implementation**

In `app/files/documents.py`, add after `_read_docx`:

```python
def _read_pdf(path: Path) -> DocumentText:
    """PDF -> lines, one '[page N]' marker per page.

    An empty page is NOT skipped: it emits an explicit marker, because a silent
    gap reads to the model as "there was nothing there" rather than "this page
    could not be extracted".
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Many real-world PDFs are encrypted with an EMPTY owner password
            # and open fine; only a genuine user password is a hard failure.
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001 - a failed decrypt is just "locked"
                opened = 0
            if not opened:
                raise EncryptedDocument("this PDF is password-protected")
        total = len(reader.pages)
    except EncryptedDocument:
        raise
    except Exception as exc:  # noqa: BLE001 - no pypdf exception escapes this module
        raise ReadError(f"could not read the PDF: {exc}") from exc

    limit = min(total, MAX_PDF_PAGES)
    lines: list[str] = []
    text_pages = 0
    for index in range(limit):
        try:
            raw = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not kill the document
            raw = ""
        page_lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
        if page_lines:
            text_pages += 1
            lines.append(f"[page {index + 1}]")
            lines.extend(page_lines)
        else:
            lines.append(
                f"[page {index + 1}] (no extractable text — likely a scanned image)"
            )
    return DocumentText(
        kind="PDF",
        lines=lines,
        pages=total,
        text_pages=text_pages,
        pages_skipped=total - limit,
    )
```

And in `read_lines`, add the branch above the `raise`:

```python
    if ext == ".pdf":
        return _read_pdf(path)
```

> Note: `_read_pdf` reads `MAX_PDF_PAGES` off the module at call time (not as a default argument), so the `monkeypatch.setattr` in the cap test takes effect.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_documents.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add app/files/documents.py tests/test_documents.py
git commit -m "feat(files): read PDFs with pypdf, marking every page

A scanned page emits an explicit marker rather than a silent gap, and a fully
scanned PDF returns text_pages=0 rather than raising — the OCR policy decision
belongs to the tool. Encrypted files try an empty password first."
```

---

### Task 4: `summarize_document`

**Files:**
- Modify: `app/files/documents.py`
- Test: `tests/test_documents.py`

**Interfaces:**
- Consumes: `read_lines`, `DocumentText`
- Produces: `@dataclass DocumentSummary{kind, lines, chars, pages, text_pages}` with `.text() -> str` and `.as_dict() -> dict`; `summarize_document(path: Path) -> DocumentSummary`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_documents.py`:

```python
def test_summary_text_for_each_kind(tmp_path):
    txt = _write(tmp_path, "a.txt", "one\ntwo\nthree")
    assert documents.summarize_document(txt).text() == "Text file, 3 lines"

    pdf = _write_bytes(tmp_path, "a.pdf", _text_pdf_bytes(["hello", "world"]))
    assert documents.summarize_document(pdf).text().startswith("PDF, 2 pages, ")

    docx = _make_docx(tmp_path, "s.docx")
    assert documents.summarize_document(docx).text().startswith("Word document, ")


def test_scanned_pdf_summary_says_so(tmp_path):
    p = _write_bytes(tmp_path, "scan.pdf", _image_only_pdf_bytes(tmp_path, 2))
    assert documents.summarize_document(p).text() == (
        "PDF, 2 pages, no extractable text (scanned)"
    )


def test_summary_and_reader_agree_on_kind(tmp_path):
    p = _write(tmp_path, "bad.json", "{oops")
    assert documents.summarize_document(p).kind == documents.read_lines(p).kind == "JSON (unparsed)"


def test_summary_as_dict_round_trips(tmp_path):
    p = _write(tmp_path, "a.md", "# hi\ntext")
    data = documents.summarize_document(p).as_dict()
    assert data["kind"] == "Markdown"
    assert data["lines"] == 2
    assert data["chars"] > 0
    assert data["pages"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_documents.py -k summary -v
```

Expected: FAIL — `AttributeError: module 'app.files.documents' has no attribute 'summarize_document'`.

- [ ] **Step 3: Write the implementation**

Append to `app/files/documents.py`:

```python
# --------------------------------------------------------------------------- #
# Public: compact summary (for the upload response + the chat attachment note)
# --------------------------------------------------------------------------- #
@dataclass
class DocumentSummary:
    kind: str
    lines: int
    chars: int
    pages: Optional[int] = None
    text_pages: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "lines": self.lines,
            "chars": self.chars,
            "pages": self.pages,
            "text_pages": self.text_pages,
        }

    def text(self) -> str:
        """One-line human/model summary, e.g. 'PDF, 12 pages, 340 lines'."""
        if self.pages is not None:
            page_word = "page" if self.pages == 1 else "pages"
            if not self.text_pages:
                return f"{self.kind}, {self.pages} {page_word}, no extractable text (scanned)"
            return f"{self.kind}, {self.pages} {page_word}, {self.lines} lines"
        line_word = "line" if self.lines == 1 else "lines"
        return f"{self.kind}, {self.lines} {line_word}"


def summarize_document(path: Path) -> DocumentSummary:
    """Structure summary of a document (raises ReadError on an unreadable file).

    Computed FROM read_lines — one parse — so the summary and what the read tool
    later returns can never disagree about kind or counts.
    """
    doc = read_lines(Path(path))
    return DocumentSummary(
        kind=doc.kind,
        lines=len(doc.lines),
        chars=sum(len(line) for line in doc.lines),
        pages=doc.pages,
        text_pages=doc.text_pages,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_documents.py -v
```

Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add app/files/documents.py tests/test_documents.py
git commit -m "feat(files): summarize_document, computed from read_lines

One parse means the upload summary and the read tool can never disagree about
kind or counts — invalid JSON reports 'JSON (unparsed)' in both places."
```

---

### Task 5: `ingest.py` — format dispatch

**Files:**
- Create: `app/files/ingest.py`
- Test: `tests/test_documents.py`

**Interfaces:**
- Consumes: `readers.summarize`, `documents.summarize_document`, media-type constants from `app/files/store.py:27-32`
- Produces: `SPREADSHEET_EXTS: set[str]`, `DOCUMENT_EXTS: set[str]`, `UPLOAD_TYPES: dict[str, str]`, `summarize(path: Path)` returning either summary type (both expose `.text()` / `.as_dict()`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_documents.py`:

```python
def test_ingest_dispatches_both_families(tmp_path):
    from app.files import ingest

    txt = _write(tmp_path, "a.txt", "one\ntwo")
    assert ingest.summarize(txt).text() == "Text file, 2 lines"

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["name", "amount"])
    wb.active.append(["a", 1])
    xlsx = tmp_path / "b.xlsx"
    wb.save(str(xlsx))
    assert ingest.summarize(xlsx).text().startswith("Excel, ")


def test_upload_types_cover_every_supported_extension():
    from app.files import ingest

    assert set(ingest.UPLOAD_TYPES) == ingest.SPREADSHEET_EXTS | ingest.DOCUMENT_EXTS
    assert ".xlsm" not in ingest.UPLOAD_TYPES  # macro-enabled stays out
    assert ingest.UPLOAD_TYPES[".pdf"] == "application/pdf"


def test_ingest_rejects_an_unknown_extension(tmp_path):
    from app.files import ingest

    p = _write(tmp_path, "a.rtf", "hi")
    with pytest.raises(ReadError):
        ingest.summarize(p)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_documents.py -k ingest -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.files.ingest'`.

- [ ] **Step 3: Write the implementation**

Create `app/files/ingest.py`:

```python
"""Which FAMILY is this uploaded file — spreadsheet or document?

One source of truth, so neither the upload route nor the turn-open path
branches on extension itself. Both summary types expose `.text()` and
`.as_dict()`, so callers never need to know which one they got back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import documents, readers
from .readers import ReadError
from .store import CSV_MEDIA_TYPE, DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE, XLSX_MEDIA_TYPE

SPREADSHEET_EXTS = {".xlsx", ".csv"}
DOCUMENT_EXTS = set(documents.DOCUMENT_EXTS)

# Upload allowlist: extension -> stored media type. `.xlsm` (macro-enabled) is
# deliberately absent.
UPLOAD_TYPES: dict[str, str] = {
    ".xlsx": XLSX_MEDIA_TYPE,
    ".csv": CSV_MEDIA_TYPE,
    ".pdf": PDF_MEDIA_TYPE,
    ".docx": DOCX_MEDIA_TYPE,
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
}

Summary = Union[readers.Summary, documents.DocumentSummary]


def summarize(path: Path) -> Summary:
    """Structure summary of any supported upload (raises ReadError otherwise)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in SPREADSHEET_EXTS:
        return readers.summarize(path)
    if ext in DOCUMENT_EXTS:
        return documents.summarize_document(path)
    raise ReadError(f"unsupported file type '{ext}'")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_documents.py -v
```

Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add app/files/ingest.py tests/test_documents.py
git commit -m "feat(files): ingest.py — one source of truth for file family

Upload and turn-open stop branching on extension; both summary types expose
text()/as_dict() so callers never need to know which they got."
```

---

### Task 6: The `read_document` tool

All policy lives here: owner-scoping, the scanned-PDF error, paging, header-first metadata, whole-line truncation.

**Files:**
- Create: `app/tools/local/read_document.py`
- Modify: `app/tools/local/__init__.py:10-26` (imports) and `:29-45` (`LOCAL_TOOLS`)
- Test: `tests/test_read_document_tool.py`

**Interfaces:**
- Consumes: `documents.read_lines`, `documents.EncryptedDocument`, `ingest.SPREADSHEET_EXTS`, `resolve_file` from `app/files/store.py:88`, `LocalToolSpec` from `app/tools/local/base.py:20`
- Produces: `SPEC: LocalToolSpec` named `read_document`; module constants `READ_DOC_MAX_LINES = 400`, `MODEL_RESULT_CAP = 8000`, `HEADER_BUDGET = 400`, `DOC_MAX_CHARS = 7600`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_read_document_tool.py`:

```python
"""Offline tests for read_document.

No DB: uses the in-memory fallback file store as the file source (resolve_file
falls back to file_store.get when no PostgresFileSource is installed). Writes a
real file to disk through the store, then drives the tool fn directly.
"""

from __future__ import annotations

import asyncio

import pytest

from app.files.store import PDF_MEDIA_TYPE, XLSX_MEDIA_TYPE, file_store
from app.tools.local import read_document


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _save(raw: bytes, filename: str, media_type: str = "text/plain; charset=utf-8") -> str:
    rec = asyncio.run(file_store.save(raw, filename=filename, media_type=media_type))
    return rec.id


def _read(args) -> str:
    return asyncio.run(read_document.SPEC.func(args))


def _text_pdf_bytes(pages: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, body)
    return bytes(pdf.output())


def _image_only_pdf_bytes(tmp_path, n_pages: int) -> bytes:
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "block.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for _ in range(n_pages):
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=50)
    return bytes(pdf.output())


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #
def test_missing_file_id_errors():
    assert _read({}).startswith("ERROR: 'file_id' is required")


def test_unknown_id_errors_without_distinguishing_foreign_from_missing():
    assert _read({"file_id": "nope"}) == (
        "ERROR: no such file (unknown id, or you don't own it)."
    )


def test_spreadsheet_id_points_at_the_excel_tools():
    fid = _save(b"a,b\n1,2\n", "book.xlsx", XLSX_MEDIA_TYPE)
    assert _read({"file_id": fid}) == (
        "ERROR: this is a spreadsheet — use inspect_excel / read_excel instead."
    )


def test_fully_scanned_pdf_returns_the_ocr_error(tmp_path):
    fid = _save(_image_only_pdf_bytes(tmp_path, 3), "scan.pdf", PDF_MEDIA_TYPE)
    assert _read({"file_id": fid}) == (
        "ERROR: this PDF appears to contain scanned images with no text layer "
        "— OCR is not available yet."
    )


def test_password_protected_pdf_has_its_own_error():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(_text_pdf_bytes(["secret"])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buf = BytesIO()
    writer.write(buf)
    fid = _save(buf.getvalue(), "locked.pdf", PDF_MEDIA_TYPE)
    assert _read({"file_id": fid}) == (
        "ERROR: this PDF is password-protected — it cannot be read."
    )


def test_corrupt_pdf_reports_a_read_error():
    fid = _save(b"%PDF-1.4\ngarbage", "broken.pdf", PDF_MEDIA_TYPE)
    assert _read({"file_id": fid}).startswith("ERROR: could not read the document")


# --------------------------------------------------------------------------- #
# Output shape
# --------------------------------------------------------------------------- #
def test_metadata_is_the_first_line():
    fid = _save("alpha\nbeta\ngamma".encode(), "a.txt")
    out = _read({"file_id": fid})
    assert out.splitlines()[0] == "Text file, 3 lines — showing lines 1–3 of 3."


def test_pdf_header_names_pages_and_body_carries_markers():
    fid = _save(_text_pdf_bytes(["Alpha", "Beta"]), "a.pdf", PDF_MEDIA_TYPE)
    out = _read({"file_id": fid})
    assert out.splitlines()[0].startswith("PDF, 2 pages, ")
    assert "[page 1]" in out
    assert "[page 2]" in out


def test_partially_scanned_pdf_counts_the_empty_pages_in_the_header(tmp_path):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "b.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, "Readable")
    pdf.add_page()
    pdf.image(str(img), x=10, y=10, w=50)
    fid = _save(bytes(pdf.output()), "mixed.pdf", PDF_MEDIA_TYPE)

    out = _read({"file_id": fid})
    assert "1 of 2 pages have no extractable text (likely scanned images)." in out
    assert "[page 2] (no extractable text — likely a scanned image)" in out


def test_start_line_past_the_end_says_how_long_the_document_is():
    fid = _save(b"one\ntwo", "a.txt")
    assert _read({"file_id": fid, "start_line": 99}) == (
        "ERROR: start_line=99 is past the end — this Text file has 2 lines."
    )


# --------------------------------------------------------------------------- #
# Paging + truncation (the correctness core)
# --------------------------------------------------------------------------- #
def test_line_window_is_honoured():
    body = "\n".join(f"line {i}" for i in range(1, 21))
    fid = _save(body.encode(), "a.txt")
    out = _read({"file_id": fid, "start_line": 5, "max_lines": 3})
    assert out.splitlines()[0] == "Text file, 20 lines — showing lines 5–7 of 20."
    assert "line 5" in out and "line 7" in out and "line 8" not in out


def test_truncation_note_names_the_exact_next_start_line():
    body = "\n".join(f"line {i}" for i in range(1, 21))
    fid = _save(body.encode(), "a.txt")
    out = _read({"file_id": fid, "max_lines": 4})
    assert "TRUNCATED: call read_document again with start_line=5 to continue." in out


def test_char_budget_truncates_on_whole_lines_and_reports_truthfully():
    """The header's start_line must equal the first line NOT delivered."""
    body = "\n".join("x" * 200 for _ in range(200))  # 40k chars, way over budget
    fid = _save(body.encode(), "big.txt")
    out = _read({"file_id": fid})

    assert len(out) <= read_document.MODEL_RESULT_CAP
    lines = out.splitlines()
    header, body_lines = lines[0], [ln for ln in lines[2:] if ln]
    assert "TRUNCATED" in lines[1]
    # every delivered line is COMPLETE, never a fragment
    assert all(ln == "x" * 200 for ln in body_lines)
    # and the promised continuation point is exactly one past what we delivered
    assert f"start_line={len(body_lines) + 1} to continue." in lines[1]
    assert f"showing lines 1–{len(body_lines)} of 200." in header


def test_paging_from_the_reported_start_line_loses_nothing():
    body = "\n".join(f"line {i}" for i in range(1, 31))
    fid = _save(body.encode(), "a.txt")
    first = _read({"file_id": fid, "max_lines": 10})
    second = _read({"file_id": fid, "start_line": 11, "max_lines": 10})
    assert "line 10" in first and "line 11" not in first
    assert "line 11" in second


def test_our_budget_stays_under_the_agent_loops_cap():
    """A regression lock: if agent.loop lowers its cap, this test fails loudly
    rather than the tool silently over-promising in its header."""
    from app.agent.loop import MAX_TOOL_RESULT_CHARS

    assert read_document.MODEL_RESULT_CAP == MAX_TOOL_RESULT_CHARS
    assert read_document.DOC_MAX_CHARS < MAX_TOOL_RESULT_CHARS
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_read_document_tool.py -v
```

Expected: collection error — `ImportError: cannot import name 'read_document'`.

- [ ] **Step 3: Write the implementation**

Create `app/tools/local/read_document.py`:

```python
"""Local tool: read_document — the text of ONE uploaded document.

Owner-scoped by file_id (see files/source.py). Handles .pdf/.docx/.txt/.md/
.json; a PDF's page boundaries appear as '[page N]' marker lines inside the
line stream, so there is only ever ONE paging unit.

Two deliberate differences from read_excel, both about truncation honesty:

  * METADATA LEADS. agent/loop.py cuts any tool result over
    MAX_TOOL_RESULT_CHARS from the END, which is exactly where read_excel puts
    its "call again with start_row=N" note. Leading metadata survives the cut.
  * WE TRUNCATE FIRST, on whole lines. If the loop cut the body instead, the
    header would promise "continue at line 401" while the model only ever saw
    line 90 — a silent hole that looks like a complete read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from ...files import documents, ingest, readers
from ...files.store import resolve_file
from .base import LocalToolSpec

READ_DOC_MAX_LINES = 400

# Must equal agent.loop.MAX_TOOL_RESULT_CHARS. NOT imported from there: the
# agent imports the tool registry, so a tools -> agent import is circular.
# tests/test_read_document_tool.py asserts the two agree.
MODEL_RESULT_CAP = 8000
HEADER_BUDGET = 400                              # room for the metadata block
DOC_MAX_CHARS = MODEL_RESULT_CAP - HEADER_BUDGET  # 7600


def _window(
    lines: list[str], start_line: int, max_lines: Optional[int]
) -> tuple[list[str], int, bool]:
    """Return (window, last_line_number, truncated).

    Truncation is on WHOLE lines: a line that would cross the budget is dropped
    entirely, so `last_line_number` is exactly what the model received and
    `last_line_number + 1` is exactly where it should resume.
    """
    start = max(1, start_line)
    index = start - 1
    cap = READ_DOC_MAX_LINES
    if max_lines is not None:
        cap = max(1, min(int(max_lines), READ_DOC_MAX_LINES))
    selected = lines[index : index + cap]

    out: list[str] = []
    used = 0
    for line in selected:
        cost = len(line) + 1  # + the newline that joins it
        if not out and cost > DOC_MAX_CHARS:
            # A single line longer than the entire budget. Emit it alone and
            # hard-cut, or the reader could never make progress past it.
            out.append(line[:DOC_MAX_CHARS] + " …[long line truncated]")
            break
        if used + cost > DOC_MAX_CHARS:
            break
        out.append(line)
        used += cost

    last = index + len(out)
    return out, last, last < len(lines)


def _header(doc: documents.DocumentText, start: int, last: int, truncated: bool) -> list[str]:
    total = len(doc.lines)
    if doc.pages is not None:
        page_word = "page" if doc.pages == 1 else "pages"
        head = (
            f"{doc.kind}, {doc.pages} {page_word}, {total} lines — "
            f"showing lines {start}–{last} of {total}."
        )
    else:
        head = f"{doc.kind}, {total} lines — showing lines {start}–{last} of {total}."
    out = [head]

    if truncated:
        out.append(
            f"TRUNCATED: call read_document again with start_line={last + 1} to continue."
        )
    if doc.pages_skipped:
        first_skipped = doc.pages - doc.pages_skipped + 1
        out.append(
            f"PARTIAL: pages {first_skipped}–{doc.pages} were not read "
            f"(limit {documents.MAX_PDF_PAGES} pages)."
        )
    if doc.pages is not None and doc.text_pages is not None:
        read_count = doc.pages - (doc.pages_skipped or 0)
        empty = read_count - doc.text_pages
        if empty > 0:
            out.append(
                f"{empty} of {read_count} pages have no extractable text "
                f"(likely scanned images)."
            )
    return out


async def _read_document(args: dict[str, Any]) -> str:
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return "ERROR: 'file_id' is required (the id of an uploaded document)."
    record = await resolve_file(file_id.strip())
    if record is None:
        return "ERROR: no such file (unknown id, or you don't own it)."

    if Path(record.path).suffix.lower() in ingest.SPREADSHEET_EXTS:
        return "ERROR: this is a spreadsheet — use inspect_excel / read_excel instead."

    try:
        start_line = int(args.get("start_line", 1) or 1)
    except (TypeError, ValueError):
        return "ERROR: 'start_line' must be an integer (1-based)."
    max_lines = args.get("max_lines")
    try:
        max_lines = int(max_lines) if max_lines is not None else None
    except (TypeError, ValueError):
        return "ERROR: 'max_lines' must be an integer."

    # Extraction is sync and CPU-bound (a big PDF is seconds) — off the loop.
    try:
        doc = await asyncio.to_thread(documents.read_lines, Path(record.path))
    except documents.EncryptedDocument:
        return "ERROR: this PDF is password-protected — it cannot be read."
    except readers.ReadError as exc:
        return f"ERROR: could not read the document ({exc})."

    # Policy: a PDF with pages but no text anywhere is a scan. Said explicitly,
    # because an empty body would read to the model as "the document is blank".
    if doc.pages and not doc.text_pages:
        return (
            "ERROR: this PDF appears to contain scanned images with no text layer "
            "— OCR is not available yet."
        )
    if not doc.lines:
        return f"{doc.kind}: this document is empty (0 lines)."
    if start_line > len(doc.lines):
        return (
            f"ERROR: start_line={start_line} is past the end — this {doc.kind} "
            f"has {len(doc.lines)} lines."
        )

    window, last, truncated = _window(doc.lines, start_line, max_lines)
    header = _header(doc, max(1, start_line), last, truncated)
    return "\n".join(header + [""] + window)


SPEC = LocalToolSpec(
    name="read_document",
    description=(
        "Read the text of a document the USER attached to THIS chat (.pdf, .docx, "
        ".txt, .md, .json) by its file_id. Page through it with 'start_line' "
        "(1-based) and 'max_lines'; the FIRST line of the result gives the total "
        "line count, and if the output was truncated the second line gives the "
        "exact start_line to continue from. In a PDF, page boundaries appear as "
        "'[page N]' marker lines, so you can cite the page a passage came from. "
        "For a spreadsheet (.xlsx/.csv) use inspect_excel / read_excel instead, "
        "and for any total or breakdown use aggregate_excel. For questions about "
        "company policy, circulars, entitlements or internal rules that the user "
        "did NOT attach to this chat, use search_department_docs — that searches "
        "the department's official corpus, while this reads one attached file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "Id of an uploaded/attached document.",
            },
            "start_line": {
                "type": "integer",
                "description": "1-based first line to return (default 1).",
            },
            "max_lines": {
                "type": "integer",
                "description": "Max lines to return this call (capped at 400).",
            },
        },
        "required": ["file_id"],
    },
    func=_read_document,
)
```

- [ ] **Step 4: Register the tool**

In `app/tools/local/__init__.py`, add `read_document,` to the import list (alphabetical, between `pdf,` and `read_excel,`):

```python
    pdf,
    read_document,
    read_excel,
```

and add its SPEC to `LOCAL_TOOLS`, after `read_excel.SPEC`:

```python
    read_excel.SPEC,
    read_document.SPEC,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_read_document_tool.py -v
```

Expected: 16 passed.

- [ ] **Step 6: Verify the tool is registered**

```bash
.venv/bin/python -c "
from app.tools.local import LOCAL_TOOLS
names = [t.name for t in LOCAL_TOOLS]
print(len(names), 'tools'); assert 'read_document' in names, names; print('registered')
"
```

Expected: `16 tools` then `registered`.

- [ ] **Step 7: Commit**

```bash
git add app/tools/local/read_document.py app/tools/local/__init__.py tests/test_read_document_tool.py
git commit -m "feat(tools): read_document — paged text of an attached document

Metadata leads the result because agent/loop.py truncates from the end, and
the tool truncates first on whole lines so the start_line it reports is
exactly the first line the model did not receive."
```

---

### Task 7: Wire uploads and turn-open

**Files:**
- Modify: `app/files/router.py:41-48` (imports/allowlist), `:88-93` (route docs), `:101-104` (extension check), `:130-138` (zip-bomb guard), `:140-144` (parse check), `:152` and `:162` (media type lookups), `:65-72` (`UploadResponse` docstring)
- Modify: `app/history/service.py:39-41`
- Modify: `tests/test_excel_upload_integration.py:151-155` — **this existing test breaks**, see Step 2
- Test: `tests/test_document_upload.py`

**Interfaces:**
- Consumes: `ingest.UPLOAD_TYPES`, `ingest.summarize`, `readers.ReadError` (which `EncryptedDocument` subclasses)
- Produces: `POST /v1/files` accepting all seven extensions

- [ ] **Step 1: Write the failing tests**

Create `tests/test_document_upload.py`. There are **no shared pytest fixtures** in this repo's integration tests — each one opens `TestClient(app)` itself and calls a local `_auth()` helper that `pytest.skip`s when Postgres is unreachable. That pattern is reproduced in full below (do not try to import it from the excel test):

```python
"""Upload-route integration for documents (.pdf/.docx/.txt/.md/.json), against
real Postgres. Skips cleanly if the DB is unreachable.

Mirrors the setup in test_excel_upload_integration.py: a TestClient per test and
a local _auth() that registers, logs in, and skips when Postgres is down.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from starlette.testclient import TestClient

from app.main import app

OWNER = "docup-owner@example.com"
PASSWORD = "supersecret123"

PDF_CT = "application/pdf"
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(client, headers, name, data, ctype):
    return client.post(
        "/v1/files", files={"file": (name, data, ctype)}, headers=headers
    )


def _text_pdf_bytes(pages):
    from fpdf import FPDF

    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, body)
    return bytes(pdf.output())


def _image_only_pdf_bytes(tmp_path, n_pages):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "block.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for _ in range(n_pages):
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=50)
    return bytes(pdf.output())


def test_pdf_upload_is_accepted():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "doc.pdf", _text_pdf_bytes(["hello", "world"]), PDF_CT)
        assert up.status_code == 201, up.text
        body = up.json()
        assert body["media_type"] == "application/pdf"
        assert body["summary"]["pages"] == 2
        assert body["source"] == "uploaded"


def test_scanned_pdf_uploads_successfully(tmp_path):
    """The OCR seam: a scan is a VALID file, so it must not be rejected here.
    read_document is where the user learns it has no text layer."""
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "scan.pdf", _image_only_pdf_bytes(tmp_path, 3), PDF_CT)
        assert up.status_code == 201, up.text
        summary = up.json()["summary"]
        assert summary["pages"] == 3
        assert summary["text_pages"] == 0


def test_txt_and_md_uploads_are_accepted():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "notes.txt", b"one\ntwo\n", "text/plain").status_code == 201
        assert _upload(client, owner, "notes.md", b"# hi\n", "text/markdown").status_code == 201


def test_docx_upload_is_accepted(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("hello")
    p = tmp_path / "a.docx"
    doc.save(str(p))
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "a.docx", p.read_bytes(), DOCX_CT)
        assert up.status_code == 201, up.text


def test_corrupt_pdf_is_rejected():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "broken.pdf", b"%PDF-1.4\ngarbage", PDF_CT)
        assert up.status_code == 400
        assert "could not read" in up.json()["detail"]


def test_password_protected_pdf_is_rejected():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(_text_pdf_bytes(["secret"])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buf = BytesIO()
    writer.write(buf)
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "locked.pdf", buf.getvalue(), PDF_CT)
        assert up.status_code == 400


def test_docx_zip_bomb_is_refused():
    """The .xlsx guard now covers .docx, which is also a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (200 * 1024 * 1024))
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "bomb.docx", buf.getvalue(), DOCX_CT)
        assert up.status_code == 400
        assert "too large" in up.json()["detail"]


def test_xlsm_is_still_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "macro.xlsm", b"anything", "application/octet-stream").status_code == 400


def test_rtf_is_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "a.rtf", b"{\\rtf1}", "application/rtf").status_code == 400
```

- [ ] **Step 2: Fix the existing test that this slice invalidates**

`tests/test_excel_upload_integration.py:151-155` asserts a `.txt` upload is rejected — which was true when the allowlist was spreadsheet-only, and is deliberately no longer true. Change its payload to a format that stays rejected, and say why:

```python
def test_upload_bad_extension_rejected():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        # .rtf, not .txt: .txt became a supported document format when
        # read_document landed. .rtf is still outside the allowlist.
        up = _upload(client, owner, "notes.rtf", b"{\\rtf1}", "application/rtf")
        assert up.status_code == 400
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_document_upload.py -v
```

Expected: FAIL — 400 "only .xlsx and .csv files are accepted" on the PDF/txt/docx cases. (If every test *skips*, Postgres is not running — start it before continuing, or this task has no coverage.)

- [ ] **Step 4: Update the upload route**

In `app/files/router.py`:

Add `asyncio` to the imports at the top (line 17 area):

```python
import asyncio
import os
import zipfile
```

Change the module import on line 41 and delete the `_UPLOAD_TYPES` constant on lines 46-48:

```python
from . import ingest, readers, repository as repo
from .store import file_store

router = APIRouter(prefix="/v1", tags=["files"])

_CHUNK = 64 * 1024
```

> The `CSV_MEDIA_TYPE` / `XLSX_MEDIA_TYPE` imports from `.store` are no longer needed here — `ingest.UPLOAD_TYPES` owns them now. Keep `file_store`.

Update `UploadResponse.summary`'s comment (line 71):

```python
    summary: dict  # spreadsheet: {kind, sheets, total_rows} | document: {kind, lines, chars, pages, text_pages}
```

Update the route decorator's `summary=` and the 400 description (lines 88-90):

```python
    summary="Upload a file the model can read (.xlsx/.csv/.pdf/.docx/.txt/.md/.json)",
    responses={
        400: {"description": "Bad extension, corrupt/encrypted file, or zip-bomb."},
```

Replace the extension check (lines 102-104):

```python
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ingest.UPLOAD_TYPES:
        raise _reject(
            None,
            400,
            "only .xlsx, .csv, .pdf, .docx, .txt, .md and .json files are accepted",
        )
```

Replace the zip-bomb guard (lines 130-138) — `.docx` is a zip too:

```python
    # 3) zip-bomb guard for the OOXML formats: refuse absurd expansion
    if ext in (".xlsx", ".docx"):
        try:
            with zipfile.ZipFile(dest) as zf:
                uncompressed = sum(i.file_size for i in zf.infolist())
        except zipfile.BadZipFile:
            raise _reject(dest, 400, f"file is not a valid {ext} document")
        if uncompressed > settings.upload_xlsx_max_uncompressed:
            raise _reject(dest, 400, "file expands too large to process safely")
```

Replace the parse check (lines 140-144). Note it now runs off the event loop, which also fixes the pre-existing inline `.xlsx` parse:

```python
    # 4) parse check + summary. Never evaluates formulas; never OCRs. A scanned
    # PDF passes here deliberately — it is a valid file, and read_document is
    # where the user is told it has no text layer. Bad file -> unlink + 400.
    try:
        summary = await asyncio.to_thread(ingest.summarize, dest)
    except readers.ReadError as exc:
        raise _reject(dest, 400, f"could not read the file ({exc})")
```

Replace both `_UPLOAD_TYPES[ext]` lookups (lines 152 and 162) with `ingest.UPLOAD_TYPES[ext]`.

Finally update the module docstring's first line (line 2):

```python
"""Generated- and uploaded-file routes (authed):
  POST /v1/files       — upload a file the model can read (spreadsheet or document)
```

- [ ] **Step 5: Update turn-open**

In `app/history/service.py`, change the import to include `ingest` and swap the summarize call on line 39:

```python
            summary = await asyncio.to_thread(ingest.summarize, row.path)
```

`EncryptedDocument` subclasses `ReadError`, so the existing `except readers.ReadError` on line 41 already covers it — an unreadable attachment still attaches with an empty summary and the tool reports the error.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_document_upload.py -v
```

Expected: 9 passed.

- [ ] **Step 7: Run the full suite for regressions**

```bash
.venv/bin/pytest -q
```

Expected: all pass. `tests/test_excel_upload_integration.py` and `tests/test_attachment_note.py` exercise the same code paths — if either fails, the allowlist or summarize swap is wrong.

- [ ] **Step 8: Commit**

```bash
git add app/files/router.py app/history/service.py tests/test_document_upload.py tests/test_excel_upload_integration.py
git commit -m "feat(files): accept documents on upload, parse off the event loop

Allowlist moves to ingest.UPLOAD_TYPES; the zip-bomb guard now covers .docx.
The parse check runs in a thread, which also takes the pre-existing inline
.xlsx parse off the loop. A scanned PDF is accepted deliberately — it is a
valid file, and read_document reports the missing text layer."
```

---

### Task 8: Routing lock + deterministic evals

**Files:**
- Modify: `tests/test_excel_read_tools.py:122` (extend the existing routing test)
- Create: `tests/test_document_eval.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7

- [ ] **Step 1: Extend the description-routing lock**

Read `tests/test_excel_read_tools.py:122` first to match its existing style, then append this test to that file:

```python
def test_descriptions_route_attached_documents_to_read_document():
    """Tool descriptions ARE the routing prompt. Without these cross-references
    the model picks search_department_docs for an attached PDF (answering from
    the corpus instead of the file in front of it) or read_document for a
    spreadsheet."""
    from app.tools.local import read_document, search_department_docs

    doc_desc = read_document.SPEC.description
    assert "read_excel" in doc_desc
    assert "aggregate_excel" in doc_desc
    assert "search_department_docs" in doc_desc
    assert "attached" in doc_desc.lower()

    # and the corpus tool keeps pointing spreadsheet totals elsewhere
    assert "aggregate_excel" in search_department_docs.SPEC.description
```

- [ ] **Step 2: Run it**

```bash
.venv/bin/pytest tests/test_excel_read_tools.py -v
```

Expected: all pass, including the new test.

- [ ] **Step 3: Write the eval suite**

Create `tests/test_document_eval.py`:

```python
"""Deterministic eval for read_document — 8 labelled cases.

The reader is not a model, so every case is a substring/format assertion and
the target is 8/8: any failure is a bug, not a regression in quality. See the
"Evaluation & Improvement" section of
docs/superpowers/specs/2026-08-11-read-document-design.md.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.files.store import PDF_MEDIA_TYPE, file_store
from app.tools.local import read_document


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _save(raw: bytes, filename: str, media_type: str = "text/plain; charset=utf-8") -> str:
    return asyncio.run(
        file_store.save(raw, filename=filename, media_type=media_type)
    ).id


def _read(**args) -> str:
    return asyncio.run(read_document.SPEC.func(args))


def _text_pdf_bytes(pages):
    from fpdf import FPDF

    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, body)
    return bytes(pdf.output())


# 1 — text PDF, page attribution
def test_eval_1_text_pdf_attributes_pages():
    fid = _save(_text_pdf_bytes(["Alpha section", "Beta section", "Gamma section"]),
                "policy.pdf", PDF_MEDIA_TYPE)
    out = _read(file_id=fid)
    assert out.splitlines()[0].startswith("PDF, 3 pages, ")
    for n, word in ((1, "Alpha"), (2, "Beta"), (3, "Gamma")):
        marker = out.index(f"[page {n}]")
        assert word in out[marker : marker + 200]


# 2 — mixed PDF: image-only pages marked, no error raised
def test_eval_2_mixed_pdf_marks_only_the_image_pages(tmp_path):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "b.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for i in range(4):
        pdf.add_page()
        if i in (1, 2):
            pdf.image(str(img), x=10, y=10, w=50)
        else:
            pdf.set_font("Helvetica", size=12)
            pdf.multi_cell(0, 10, f"Readable page {i + 1}")
    fid = _save(bytes(pdf.output()), "mixed.pdf", PDF_MEDIA_TYPE)

    out = _read(file_id=fid)
    assert not out.startswith("ERROR")
    assert "[page 2] (no extractable text — likely a scanned image)" in out
    assert "[page 3] (no extractable text — likely a scanned image)" in out
    assert "2 of 4 pages have no extractable text (likely scanned images)." in out


# 3 — fully scanned PDF: the distinct OCR error
def test_eval_3_scanned_pdf_returns_the_ocr_error(tmp_path):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "b.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for _ in range(2):
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=50)
    fid = _save(bytes(pdf.output()), "scan.pdf", PDF_MEDIA_TYPE)

    assert _read(file_id=fid) == (
        "ERROR: this PDF appears to contain scanned images with no text layer "
        "— OCR is not available yet."
    )


# 4 — docx heading + table
def test_eval_4_docx_heading_and_table(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_heading("Eligibility", level=1)
    doc.add_paragraph("Applicants must be resident.")
    table = doc.add_table(rows=2, cols=3)
    for col, value in enumerate(("name", "min", "max")):
        table.cell(0, col).text = value
    for col, value in enumerate(("term", "1", "30")):
        table.cell(1, col).text = value
    p = tmp_path / "policy.docx"
    doc.save(str(p))
    fid = _save(p.read_bytes(), "policy.docx")

    out = _read(file_id=fid)
    assert "# Eligibility" in out
    assert "name | min | max" in out
    assert "term | 1 | 30" in out


# 5 — markdown passthrough
def test_eval_5_markdown_passes_through():
    body = "# Title\n\n- one\n- two\n\n```python\nx = 1\n```\n"
    fid = _save(body.encode(), "notes.md")
    out = _read(file_id=fid)
    for line in ("# Title", "- one", "```python", "x = 1"):
        assert line in out


# 6 — txt paging, header truthful
def test_eval_6_txt_paging_reports_the_right_window():
    body = "\n".join(f"line {i}" for i in range(1, 51))
    fid = _save(body.encode(), "log.txt")
    out = _read(file_id=fid, start_line=21, max_lines=10)
    assert out.splitlines()[0] == "Text file, 50 lines — showing lines 21–30 of 50."
    assert "TRUNCATED: call read_document again with start_line=31 to continue." in out
    assert "line 21" in out and "line 30" in out
    assert "line 20" not in out and "line 31" not in out


# 7 — nested valid JSON, pretty-printed
def test_eval_7_json_is_pretty_printed():
    fid = _save(json.dumps({"loan": {"term": 30, "rates": [5.1, 5.4]}}).encode(), "a.json")
    out = _read(file_id=fid)
    assert out.splitlines()[0].startswith("JSON, ")
    assert '  "loan": {' in out
    assert '      5.1,' in out


# 8 — invalid JSON served raw
def test_eval_8_invalid_json_served_as_raw_text():
    fid = _save(b'{"a": 1,,,}', "bad.json")
    out = _read(file_id=fid)
    assert out.splitlines()[0].startswith("JSON (unparsed), ")
    assert '{"a": 1,,,}' in out
```

- [ ] **Step 4: Run the eval and record the baseline**

```bash
.venv/bin/pytest tests/test_document_eval.py -v
```

Expected: **8 passed** — that is the target and the recorded baseline. Any failure is a bug, not a quality regression.

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_document_eval.py tests/test_excel_read_tools.py
git commit -m "test(files): routing lock + 8-case deterministic eval for read_document

Baseline 8/8. The routing lock keeps read_document, read_excel and
search_department_docs cross-referencing each other — without it the model
answers an attached PDF from the department corpus."
```

---

## Verification (after all tasks)

- [ ] Full suite green: `.venv/bin/pytest -q`
- [ ] Eval at target: `.venv/bin/pytest tests/test_document_eval.py -q` → 8 passed
- [ ] Docling did not leak into the API: `.venv/bin/pytest tests/test_rag_parsing_docling.py -q`
- [ ] Tool count is 16 and `read_document` is registered:
      `.venv/bin/python -c "from app.tools.local import LOCAL_TOOLS; print(len(LOCAL_TOOLS))"`
- [ ] No migration was created: `git status --porcelain alembic/` is empty
- [ ] `app/rag/` untouched: `git diff --stat main -- app/rag/` is empty
- [ ] Update `CLAUDE.md` — add `.pdf/.docx/.txt/.md/.json` to the upload/endpoint notes, add a `read_document` bullet covering the metadata-first + whole-line truncation rule and the scanned-PDF seam, and note `documents.py`/`ingest.py` in the Layout section. Commit as `docs: read_document in CLAUDE.md`.
