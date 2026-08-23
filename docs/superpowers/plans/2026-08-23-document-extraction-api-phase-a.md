# Document Extraction API — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `POST /v1/extract` — an API-key endpoint that turns an uploaded
PDF, DOCX, TXT, MD, JSON, XLSX, CSV or image into text plus structure — and
factor the per-route policies `/v1/ocr` invented into a module the next
endpoint can reuse.

**Architecture:** One pure dispatcher (`app/publicapi/extraction.py`) fans an
uploaded file out to the extractors that already exist and are already tested
(`app/files/documents.py`, `readers.py`, `image_ocr.py`), returning one
`ExtractedText` record. A thin router streams the upload, calls the dispatcher
in a worker thread, and serialises. Nothing in Phase A calls a language model.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, pytest.
Python 3.10 via this project's own `.venv`.

**Spec:** `docs/superpowers/specs/2026-08-23-document-extraction-api-design.md`
— read §1, §3, §6 and §7 before starting. This plan implements Phase A only.

## Global Constraints

- **Use this project's venv for everything**: `.venv/bin/python`,
  `.venv/bin/pytest`, `.venv/bin/alembic`. Never a sibling project's.
- **Integration tests need `DATABASE_URL`.** Run them as
  `set -a && . ./.env && set +a && .venv/bin/pytest ...`. Without it they SKIP
  rather than fail — compare the skip count, not just the pass count.
- **`alembic heads` must stay ONE.** Current head is `53c2ce388596`. A new
  migration sits ON it, never beside it. `tests/test_alembic_lineage.py` fails
  otherwise.
- **The API image must never gain an OCR or docling import at module scope.**
  `tests/test_image_ocr_import_boundary.py` and
  `tests/test_ocr_api_boundaries.py` check this by subprocess. `image_ocr` may
  be imported (it lazy-imports rapidocr); `rapidocr`, `onnxruntime`, `cv2` and
  `docling` may not.
- **Everything new is behind `EXTERNAL_API_ENABLED`**, which is read ONCE at
  process start. A deployment with it false must be byte-identical to today.
- **No document bytes, no extracted text, no field values are ever persisted.**
  A row in `api_key_usage` and nothing else. The temp file is unlinked in
  `finally` on EVERY path, 4xx included.
- **Scope vocabulary is closed in two places** — `policy.ALL_SCOPES` and the
  `ck_api_keys_scopes` CHECK. Adding a scope means editing both.
- **Commit after each task.** Message style: `feat(publicapi): …`,
  `refactor(publicapi): …`, `docs: …`.

---

### Task 1: The `document:read` scope

**Files:**
- Modify: `app/apikeys/policy.py:34-37`
- Create: `alembic/versions/<rev>_document_read_scope.py`
- Test: `tests/test_apikey_policy.py`, `tests/test_apikey_admin_integration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.apikeys.policy.SCOPE_DOCUMENT_READ` (the string
  `"document:read"`), and `ALL_SCOPES` becomes
  `frozenset({"ocr:read", "document:read"})`. Task 6 passes
  `SCOPE_DOCUMENT_READ` to `require_api_client`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_apikey_policy.py`:

```python
def test_document_read_is_in_the_closed_vocabulary():
    from app.apikeys import policy

    assert policy.SCOPE_DOCUMENT_READ == "document:read"
    assert policy.SCOPE_DOCUMENT_READ in policy.ALL_SCOPES
    assert policy.SCOPE_OCR_READ in policy.ALL_SCOPES


def test_a_key_holding_only_ocr_read_is_refused_document_read():
    from app.apikeys import policy

    facts = policy.KeyFacts(
        is_active=True, expires_at=None, scopes=(policy.SCOPE_OCR_READ,)
    )
    refusal = policy.scope_refusal(facts, required=policy.SCOPE_DOCUMENT_READ)
    assert refusal is not None
    assert "document:read" in refusal


def test_a_scope_outside_the_vocabulary_satisfies_nothing():
    from app.apikeys import policy

    facts = policy.KeyFacts(is_active=True, expires_at=None, scopes=("made:up",))
    assert policy.scope_refusal(facts, required=policy.SCOPE_OCR_READ) is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/pytest tests/test_apikey_policy.py -q`
Expected: FAIL — `AttributeError: module 'app.apikeys.policy' has no attribute 'SCOPE_DOCUMENT_READ'`

- [ ] **Step 3: Add the scope**

In `app/apikeys/policy.py`, extend the `__all__` list with
`"SCOPE_DOCUMENT_READ"` and replace the two constant lines:

```python
SCOPE_OCR_READ = "ocr:read"
SCOPE_DOCUMENT_READ = "document:read"

# Closed vocabulary, mirroring `ck_api_keys_scopes`. Adding a scope means
# editing BOTH — that duplication is deliberate: the CHECK stops a typo being
# stored, this set stops one being honoured.
ALL_SCOPES = frozenset({SCOPE_OCR_READ, SCOPE_DOCUMENT_READ})
```

- [ ] **Step 4: Run it and watch it pass**

Run: `.venv/bin/pytest tests/test_apikey_policy.py -q`
Expected: PASS

- [ ] **Step 5: Generate the migration by hand (do NOT autogenerate)**

Alembic cannot diff a CHECK constraint's text reliably. Create
`alembic/versions/b7e1c4d92a03_document_read_scope.py`:

```python
"""add the document:read scope to ck_api_keys_scopes

Revision ID: b7e1c4d92a03
Revises: 53c2ce388596
Create Date: 2026-08-23

`ck_api_keys_scopes` enumerates the vocabulary as a literal, so a new scope is
a schema change, not a config change. That is the point: the CHECK stops a
typo'd scope being STORED while `policy.ALL_SCOPES` stops one being HONOURED.
The literal below must stay in the same sorted order `app/apikeys/models.py`
generates it in (`sorted(ALL_SCOPES)`), or a future autogenerate run will
propose a spurious diff.
"""

from alembic import op

revision = "b7e1c4d92a03"
down_revision = "53c2ce388596"
branch_labels = None
depends_on = None

_NEW = "scopes <@ ARRAY['document:read', 'ocr:read']::text[]"
_OLD = "scopes <@ ARRAY['ocr:read']::text[]"


def upgrade() -> None:
    op.drop_constraint("ck_api_keys_scopes", "api_keys", type_="check")
    op.create_check_constraint("ck_api_keys_scopes", "api_keys", _NEW)


def downgrade() -> None:
    # A key already holding document:read would violate the old CHECK, so strip
    # it first. Revoking a capability on downgrade is correct; leaving a row
    # that the constraint forbids is not.
    op.execute(
        "UPDATE api_keys SET scopes = array_remove(scopes, 'document:read')"
    )
    op.drop_constraint("ck_api_keys_scopes", "api_keys", type_="check")
    op.create_check_constraint("ck_api_keys_scopes", "api_keys", _OLD)
```

- [ ] **Step 6: Apply it and confirm the lineage is still linear**

```bash
set -a && . ./.env && set +a
.venv/bin/alembic upgrade head
.venv/bin/alembic heads          # must print exactly ONE line, ending "(head)"
.venv/bin/pytest tests/test_alembic_lineage.py -q
```
Expected: one head (`b7e1c4d92a03`), lineage test PASS.

- [ ] **Step 7: Prove the CHECK actually accepts the new scope**

Append to `tests/test_apikey_admin_integration.py` (it already has `_client`
and `_admin_token(client)` — NOT `_admin_headers`, which does not exist in that
file; build the header inline as every other test there does, e.g.
`headers={"Authorization": f"Bearer {_admin_token(client)}"}`):

```python
def test_a_key_can_be_minted_with_the_document_read_scope():
    with _client() as client:
        resp = client.post(
            "/v1/api-keys",
            json={"name": "extract-test", "scopes": ["document:read"]},
            headers={"Authorization": f"Bearer {_admin_token(client)}"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["scopes"] == ["document:read"]


def test_a_typod_scope_is_still_rejected_before_it_reaches_the_check():
    with _client() as client:
        resp = client.post(
            "/v1/api-keys",
            json={"name": "bad-scope", "scopes": ["document:reed"]},
            headers={"Authorization": f"Bearer {_admin_token(client)}"},
        )
        assert resp.status_code == 422, resp.text
```

Run: `set -a && . ./.env && set +a && .venv/bin/pytest tests/test_apikey_admin_integration.py -q`
Expected: PASS, and the skip count is 0.

- [ ] **Step 8: Commit**

```bash
git add app/apikeys/policy.py alembic/versions/b7e1c4d92a03_document_read_scope.py \
        tests/test_apikey_policy.py tests/test_apikey_admin_integration.py
git commit -m "feat(apikeys): add the document:read scope, in both places that close the vocabulary"
```

---

### Task 2: `extraction.py` — one dispatcher, bytes to `ExtractedText`

**Files:**
- Create: `app/publicapi/extraction.py`
- Test: `tests/test_extraction_dispatch.py`

**Interfaces:**
- Consumes: `app.files.ingest.{SPREADSHEET_EXTS, DOCUMENT_EXTS, IMAGE_EXTS}`,
  `app.files.documents.read_lines`, `app.files.readers.{inspect_workbook,
  load_table, ReadError}`, `app.files.images.summarize_image`,
  `app.files.image_ocr.{ocr_image, DEFAULT_LANG}`.
- Produces:
  - `ExtractedText` (frozen dataclass) with fields `kind: str`, `route: str`,
    `lines: tuple[str, ...]`, `line_confidences: tuple[float, ...] | None`,
    `sheets: tuple[Sheet, ...]`, `pages: int | None`, `text_pages: int | None`,
    `pages_skipped: int | None`, `partial: bool`, and properties
    `authoritative: bool` and `is_scanned_pdf: bool`.
  - `Sheet` (frozen dataclass): `name: str`, `headers: tuple[str, ...]`,
    `rows: tuple[tuple[str, ...], ...]`, `total_rows: int`, `truncated: bool`.
  - `read_any(path: Path, *, lang: str | None = None, ocr: Callable | None = None) -> ExtractedText`
  - `EXTRACT_EXTS: frozenset[str]`, `OCR_ROUTE = "ocr"`, `NATIVE_ROUTE = "native"`.
  - Task 3 serialises `ExtractedText`; Task 6 calls `read_any`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extraction_dispatch.py`:

```python
"""The extraction dispatcher: which engine runs, and what it reports.

No DB, no HTTP, and — deliberately — no OCR stack. The image branch takes an
injectable `ocr` callable so the DISPATCH decision is testable everywhere,
including an environment built without INSTALL_OCR. Whether the real engine
produces good text is `tests/test_image_ocr_eval.py`'s job, not this file's.
"""

import json

import pytest

from app.publicapi import extraction


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_a_txt_file_is_native_and_authoritative(tmp_path):
    path = _write(tmp_path, "a.txt", "first line\nsecond line\n")
    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert out.authoritative is True
    assert list(out.lines) == ["first line", "second line"]
    assert out.sheets == ()


def test_a_json_file_is_native(tmp_path):
    path = _write(tmp_path, "a.json", json.dumps({"k": "v"}))
    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert any("k" in line for line in out.lines)


def test_a_csv_returns_sheets_not_lines(tmp_path):
    path = _write(tmp_path, "a.csv", "name,amount\nalice,10\nbob,20\n")
    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert out.lines == ()
    assert len(out.sheets) == 1
    sheet = out.sheets[0]
    assert list(sheet.headers) == ["name", "amount"]
    assert [list(r) for r in sheet.rows] == [["alice", "10"], ["bob", "20"]]
    assert sheet.total_rows == 2


def test_a_native_pdf_reports_its_pages(tmp_path):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(40, 10, "Gross Pay 87500")
    path = tmp_path / "a.pdf"
    pdf.output(str(path))

    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert out.pages == 1
    assert out.text_pages == 1
    assert out.is_scanned_pdf is False
    assert any("87500" in line for line in out.lines)


def test_a_pdf_with_no_text_layer_is_reported_as_a_FACT_not_an_exception():
    # The dispatcher reports; the ROUTER decides it is a 422. Same seam as
    # documents.py vs the read_document tool.
    out = extraction.ExtractedText(
        kind="PDF", route=extraction.NATIVE_ROUTE, pages=3, text_pages=0
    )
    assert out.is_scanned_pdf is True


def test_an_image_routes_to_ocr_and_is_never_authoritative(tmp_path):
    from PIL import Image

    path = tmp_path / "a.png"
    Image.new("RGB", (30, 12), "white").save(path)

    class _FakeResult:
        lines = ("Account No 1234",)
        scores = (0.87,)

    calls = []

    def _fake_ocr(p, *, lang):
        calls.append((p, lang))
        return _FakeResult()

    out = extraction.read_any(path, ocr=_fake_ocr)
    assert calls and calls[0][1] == "devanagari"
    assert out.route == extraction.OCR_ROUTE
    assert out.authoritative is False
    assert list(out.lines) == ["Account No 1234"]
    assert list(out.line_confidences) == [0.87]


def test_an_unsupported_extension_raises_ReadError(tmp_path):
    from app.files.readers import ReadError

    path = _write(tmp_path, "a.exe", "nope")
    with pytest.raises(ReadError):
        extraction.read_any(path)


def test_the_accepted_extension_set_is_the_union_of_the_three_families():
    from app.files import ingest

    assert extraction.EXTRACT_EXTS == frozenset(
        ingest.SPREADSHEET_EXTS | ingest.DOCUMENT_EXTS | ingest.IMAGE_EXTS
    )
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest tests/test_extraction_dispatch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.publicapi.extraction'`

- [ ] **Step 3: Write the dispatcher**

Create `app/publicapi/extraction.py`:

```python
"""Uploaded bytes -> one `ExtractedText`, for `POST /v1/extract`.

Every engine reached from here already exists and is already tested — this
module chooses between them and normalises what they return. It holds no
policy of its own beyond one rule:

  **`route` decides `authoritative`, and nothing else does.** Text read from a
  document's own text layer is exact; text read by an OCR engine is not. The
  serialiser (`extract_schemas.py`) omits the caveat entirely for a native
  route, because over-warning trains a reader to ignore the warning — the
  §29.2 rule from docs/nrb-integration.md, applied to a second surface.

It reports FACTS and raises nothing but `ReadError`. A PDF with no text layer
is not an error here: it comes back with `text_pages == 0` and the ROUTER
turns that into a 422. That is the same seam `app/files/documents.py` already
has with the `read_document` tool, and it is why the scanned-vs-empty
distinction survives.

The `ocr` parameter is injectable so the DISPATCH decision stays testable in a
build without the OCR stack. Nothing else in this module needs one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..files import documents, image_ocr, images, ingest, readers

__all__ = [
    "OCR_ROUTE",
    "NATIVE_ROUTE",
    "EXTRACT_EXTS",
    "Sheet",
    "ExtractedText",
    "read_any",
]

OCR_ROUTE = "ocr"
NATIVE_ROUTE = "native"

EXTRACT_EXTS = frozenset(
    ingest.SPREADSHEET_EXTS | ingest.DOCUMENT_EXTS | ingest.IMAGE_EXTS
)


@dataclass(frozen=True)
class Sheet:
    """One worksheet. Spreadsheets are the one input that is not a line stream;
    flattening them would discard the structure a caller most wants."""

    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    total_rows: int
    truncated: bool


@dataclass(frozen=True)
class ExtractedText:
    kind: str
    route: str
    lines: tuple[str, ...] = ()
    line_confidences: Optional[tuple[float, ...]] = None
    sheets: tuple[Sheet, ...] = ()
    pages: Optional[int] = None
    text_pages: Optional[int] = None
    pages_skipped: Optional[int] = None
    partial: bool = False

    @property
    def authoritative(self) -> bool:
        """True only for text read from the document's own text layer."""
        return self.route == NATIVE_ROUTE

    @property
    def is_scanned_pdf(self) -> bool:
        """Pages exist and none of them yielded text. A FACT, not an error."""
        return bool(self.pages) and self.text_pages == 0


def read_any(
    path: Path,
    *,
    lang: Optional[str] = None,
    ocr: Optional[Callable[..., object]] = None,
) -> ExtractedText:
    """Dispatch on extension. Raises `readers.ReadError` for anything else."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in ingest.SPREADSHEET_EXTS:
        return _read_spreadsheet(path)
    if ext in ingest.DOCUMENT_EXTS:
        return _read_document(path)
    if ext in ingest.IMAGE_EXTS:
        return _read_image(path, lang=lang, ocr=ocr or image_ocr.ocr_image)
    raise readers.ReadError(f"unsupported file type '{ext}'")


def _read_document(path: Path) -> ExtractedText:
    doc = documents.read_lines(path)
    return ExtractedText(
        kind=doc.kind,
        route=NATIVE_ROUTE,
        lines=tuple(doc.lines),
        pages=doc.pages,
        text_pages=doc.text_pages,
        pages_skipped=doc.pages_skipped,
        # A PDF over MAX_PDF_PAGES lost pages. `read_document` reports the same
        # fact for the same reason: a silent cut reads as a complete document.
        partial=bool(doc.pages_skipped),
    )


def _read_spreadsheet(path: Path) -> ExtractedText:
    sheets: list[Sheet] = []
    truncated_any = False
    for info in readers.inspect_workbook(path):
        table = readers.load_table(path, sheet=info.sheet_name)
        truncated_any = truncated_any or table.truncated
        sheets.append(
            Sheet(
                name=table.sheet_name,
                headers=tuple(table.headers),
                rows=tuple(tuple(row) for row in table.rows),
                total_rows=table.total_rows,
                truncated=table.truncated,
            )
        )
    kind = "Excel" if path.suffix.lower() == ".xlsx" else "CSV"
    return ExtractedText(
        kind=kind, route=NATIVE_ROUTE, sheets=tuple(sheets), partial=truncated_any
    )


def _read_image(
    path: Path, *, lang: Optional[str], ocr: Callable[..., object]
) -> ExtractedText:
    # summarize_image owns the decoded-PIXEL cap and the decoder allowlist on
    # the SNIFFED format, and both run before any full decode. Never skip it.
    summary = images.summarize_image(path)
    chosen = (lang or image_ocr.DEFAULT_LANG).strip()
    result = ocr(path, lang=chosen)
    return ExtractedText(
        kind=summary.kind,
        route=OCR_ROUTE,
        lines=tuple(result.lines),
        line_confidences=tuple(result.scores),
        # Frame 1 only — a multi-frame .tif is a scanner's normal output and
        # page 2's text silently vanishes otherwise (measured).
        partial=summary.frames > 1,
    )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `.venv/bin/pytest tests/test_extraction_dispatch.py -q`
Expected: 8 passed

- [ ] **Step 5: Prove importing it loads no OCR stack**

Run:
```bash
.venv/bin/python -c "
import sys, importlib
importlib.import_module('app.publicapi.extraction')
bad = [m for m in ('rapidocr','onnxruntime','cv2','docling') if m in sys.modules]
assert not bad, bad
print('clean')"
```
Expected: `clean`

- [ ] **Step 6: Commit**

```bash
git add app/publicapi/extraction.py tests/test_extraction_dispatch.py
git commit -m "feat(publicapi): the extraction dispatcher — bytes to one ExtractedText"
```

---

### Task 3: The response schemas, and the caveat that disappears

**Files:**
- Create: `app/publicapi/extract_schemas.py`
- Test: `tests/test_extract_schemas.py`
- Modify: `tests/test_ocr_api_boundaries.py:17` (the caveat test gains a third reader)

**Interfaces:**
- Consumes: `extraction.{ExtractedText, Sheet, OCR_ROUTE, NATIVE_ROUTE}` (Task 2),
  `app.files.image_ocr.OCR_CAVEAT`.
- Produces: `ExtractResponse` (Pydantic model), and
  `build_extract_response(extracted: ExtractedText, request_id: str) -> ExtractResponse`.
  Task 6 returns it as the route's `response_model`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract_schemas.py`:

```python
"""The extract response envelope.

The rule under test is the one that differs from /v1/ocr: a NATIVE source has
an exact text layer, so it carries no caveat at all — the key is absent, not
null. Over-warning trains a reader to ignore the warning, which costs you the
warning on the page that needed it (docs/nrb-integration.md §29.2).
"""

from app.files import image_ocr
from app.publicapi import extraction
from app.publicapi.extract_schemas import build_extract_response


def _native():
    return extraction.ExtractedText(
        kind="DOCX", route=extraction.NATIVE_ROUTE, lines=("hello", "world")
    )


def _ocrd():
    return extraction.ExtractedText(
        kind="PNG image",
        route=extraction.OCR_ROUTE,
        lines=("Account No 1234",),
        line_confidences=(0.87,),
    )


def test_a_native_source_omits_the_caveat_KEY_entirely():
    dumped = build_extract_response(_native(), "req1").model_dump()
    assert dumped["source"]["authoritative"] is True
    assert "caveat" not in dumped["source"]


def test_an_ocr_source_carries_the_caveat_and_is_not_authoritative():
    dumped = build_extract_response(_ocrd(), "req2").model_dump()
    assert dumped["source"]["authoritative"] is False
    assert dumped["source"]["caveat"] == image_ocr.OCR_CAVEAT


def test_text_is_the_lines_joined_and_lines_carry_confidence():
    resp = build_extract_response(_ocrd(), "req3")
    assert resp.text == "Account No 1234"
    assert resp.lines[0].confidence == 0.87


def test_a_native_line_has_no_confidence_because_there_is_nothing_uncertain():
    resp = build_extract_response(_native(), "req4")
    assert resp.text == "hello\nworld"
    assert all(line.confidence is None for line in resp.lines)


def test_a_spreadsheet_serialises_sheets_and_an_empty_text():
    extracted = extraction.ExtractedText(
        kind="CSV",
        route=extraction.NATIVE_ROUTE,
        sheets=(
            extraction.Sheet(
                name="Sheet1",
                headers=("name", "amount"),
                rows=(("alice", "10"),),
                total_rows=1,
                truncated=False,
            ),
        ),
    )
    resp = build_extract_response(extracted, "req5")
    assert resp.text == ""
    assert resp.lines == []
    assert resp.sheets[0].headers == ["name", "amount"]
    assert resp.sheets[0].rows == [["alice", "10"]]


def test_page_facts_survive_and_null_pages_are_not_dropped():
    extracted = extraction.ExtractedText(
        kind="PDF",
        route=extraction.NATIVE_ROUTE,
        lines=("x",),
        pages=12,
        text_pages=11,
        pages_skipped=0,
    )
    dumped = build_extract_response(extracted, "req6").model_dump()
    assert dumped["source"]["pages"] == 12
    assert dumped["source"]["text_pages"] == 11
    # Only `caveat` is ever dropped. A genuinely-null page count stays null,
    # because "not a paged format" is a fact worth transmitting.
    csv_dump = build_extract_response(
        extraction.ExtractedText(kind="CSV", route=extraction.NATIVE_ROUTE), "r"
    ).model_dump()
    assert "pages" in csv_dump["source"] and csv_dump["source"]["pages"] is None


def test_nothing_in_the_schemas_compares_a_confidence_to_a_literal():
    # §16.6 declines to invent a threshold from an orthography measurement.
    # Same AST rule the OCR schemas are already held to.
    import ast
    import pathlib

    src = pathlib.Path("app/publicapi/extract_schemas.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Compare):
            text = ast.dump(node)
            assert "confidence" not in text, f"threshold comparison: {text}"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest tests/test_extract_schemas.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.publicapi.extract_schemas'`

- [ ] **Step 3: Write the schemas**

Create `app/publicapi/extract_schemas.py`:

```python
"""The `POST /v1/extract` response envelope.

One thing here is not incidental: **`caveat` is absent, not null, for a native
source.** `/v1/ocr` ships an unconditional caveat because it only ever sees
images. This endpoint reads DOCX and XLSX too, whose text layers are exact —
warning about those trains a reader to ignore the warning, and then it is
missing on the OCR'd page that needed it (docs/nrb-integration.md §29.2, the
same rule `app/rag/sources.py` follows for native NRB text).

The wording itself is `image_ocr.OCR_CAVEAT` — the SAME constant `read_image`
renders into chat and `/v1/ocr` publishes. Three readers now, still one
constant: a second copy drifts, and then two surfaces disagree about the
wording and a reader cannot tell which to believe.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from ..files.image_ocr import OCR_CAVEAT
from .extraction import NATIVE_ROUTE, ExtractedText

__all__ = ["ExtractResponse", "build_extract_response"]


class ExtractLine(BaseModel):
    text: str
    confidence: float | None = Field(
        default=None,
        description=(
            "Per-line OCR confidence, present only for an OCR'd source and "
            "null for a native one (there is nothing uncertain to report). "
            "Reported, never enforced — this measures orthographic "
            "well-formedness, not correctness, so nothing here compares it to "
            "a threshold."
        ),
    )


class ExtractSheet(BaseModel):
    name: str
    headers: list[str]
    rows: list[list[str]]
    total_rows: int
    truncated: bool = Field(
        description="True when more rows exist in the sheet than were returned."
    )


class ExtractSource(BaseModel):
    route: str = Field(
        description='"native" (the document\'s own text layer) or "ocr" (machine-read).'
    )
    authoritative: bool = Field(
        description="True only for a native route. Never true for OCR'd text."
    )
    caveat: str | None = Field(
        default=None,
        description=(
            "Present ONLY when the text was machine-read. Absent entirely for "
            "a native source — see this module's docstring."
        ),
    )
    pages: int | None = None
    text_pages: int | None = None
    pages_skipped: int | None = None
    partial: bool = Field(
        default=False,
        description=(
            "True when something was skipped: a multi-frame image beyond frame "
            "1, a PDF beyond the page cap, or a truncated sheet."
        ),
    )

    @model_serializer(mode="wrap")
    def _drop_absent_caveat(self, handler):
        """Remove `caveat` when there is none — a null caveat still reads as a
        field that exists and might one day be filled. Only this key is
        dropped: `pages: null` on a CSV is a fact worth transmitting."""
        data = handler(self)
        if data.get("caveat") is None:
            data.pop("caveat", None)
        return data


class ExtractResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "kind": "PDF",
                "text": "PAYSLIP\nEmployee: Ramesh Shrestha\nGross Pay: 87,500.00",
                "lines": [
                    {"text": "PAYSLIP", "confidence": None},
                    {"text": "Employee: Ramesh Shrestha", "confidence": None},
                ],
                "sheets": [],
                "source": {
                    "route": "native",
                    "authoritative": True,
                    "pages": 1,
                    "text_pages": 1,
                    "pages_skipped": 0,
                    "partial": False,
                },
                "request_id": "3f9a2e7c1b4d4a8e9f0c2d3e4f5a6b7c",
            }
        }
    )

    kind: str = Field(description='Human format name, e.g. "PDF", "Excel", "PNG image".')
    text: str = Field(
        description="Lines joined with newlines. Empty for a spreadsheet — see `sheets`."
    )
    lines: list[ExtractLine]
    sheets: list[ExtractSheet] = Field(
        description="Populated for .xlsx/.csv only; empty for every other format."
    )
    source: ExtractSource
    request_id: str = Field(
        description=(
            "Echoed from `X-Request-Id`. Present on a 200 only — every error "
            "path raises before that header is sent."
        )
    )


def build_extract_response(
    extracted: ExtractedText, request_id: str
) -> ExtractResponse:
    confidences = extracted.line_confidences
    lines = [
        ExtractLine(
            text=text,
            confidence=confidences[i] if confidences is not None else None,
        )
        for i, text in enumerate(extracted.lines)
    ]
    return ExtractResponse(
        kind=extracted.kind,
        text="\n".join(extracted.lines),
        lines=lines,
        sheets=[
            ExtractSheet(
                name=s.name,
                headers=list(s.headers),
                rows=[list(r) for r in s.rows],
                total_rows=s.total_rows,
                truncated=s.truncated,
            )
            for s in extracted.sheets
        ],
        source=ExtractSource(
            route=extracted.route,
            authoritative=extracted.authoritative,
            caveat=None if extracted.route == NATIVE_ROUTE else OCR_CAVEAT,
            pages=extracted.pages,
            text_pages=extracted.text_pages,
            pages_skipped=extracted.pages_skipped,
            partial=extracted.partial,
        ),
        request_id=request_id,
    )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `.venv/bin/pytest tests/test_extract_schemas.py -q`
Expected: 7 passed

- [ ] **Step 5: Extend the one-constant test to its third reader**

In `tests/test_ocr_api_boundaries.py`, replace the body of
`test_the_caveat_is_one_constant_with_two_readers` (line 17) with a version
that also names the new module, and rename it:

```python
def test_the_caveat_is_one_constant_with_three_readers():
    """`read_image` (chat), `/v1/ocr` and `/v1/extract` all render the SAME
    sentence. A second copy drifts, and then two surfaces disagree about the
    wording and a reader cannot tell which to believe."""
    from app.files import image_ocr
    from app.publicapi import extract_schemas, schemas

    assert schemas.CAVEAT is image_ocr.OCR_CAVEAT
    assert extract_schemas.OCR_CAVEAT is image_ocr.OCR_CAVEAT
    assert image_ocr.OCR_CAVEAT in image_ocr.header_for("devanagari")
```

**Do this as an ADDITION, not a rewrite.** Before editing, print the original:

```bash
sed -n '17,30p' tests/test_ocr_api_boundaries.py
```

Keep every assertion it already makes, verbatim — they cover the chat reader
and `/v1/ocr`, and this task is not changing either. Add exactly two lines: the
`extract_schemas` import and the `assert extract_schemas.OCR_CAVEAT is
image_ocr.OCR_CAVEAT`. Rename the function to `..._with_three_readers` and
update its docstring. If the original asserts the chat reader through some
helper other than `image_ocr.header_for`, keep that call unchanged — the name
above is a guess at it and the original is authoritative.

- [ ] **Step 6: Run the boundary suite**

Run: `.venv/bin/pytest tests/test_ocr_api_boundaries.py -q`
Expected: 12 passed

- [ ] **Step 7: Commit**

```bash
git add app/publicapi/extract_schemas.py tests/test_extract_schemas.py \
        tests/test_ocr_api_boundaries.py
git commit -m "feat(publicapi): the extract response envelope, with the caveat absent on native text"
```

---

### Task 4: `_route.py` — factor the five policies out, and move `/v1/ocr` onto them

**Files:**
- Create: `app/publicapi/_route.py`
- Modify: `app/publicapi/ocr_router.py`
- Test: `tests/test_publicapi_route_helpers.py`

**This is the highest-risk task in the plan, and its safety net is that
`/v1/ocr`'s existing tests must pass UNCHANGED.** Do not edit
`tests/test_ocr_api_integration.py` in this task. If a test needs changing,
the refactor changed behaviour and is wrong.

**Interfaces:**
- Consumes: `app.apikeys.dependencies.ApiClient`,
  `app.apikeys.repository.record_usage`, `app.apikeys.throttle.RateLimiter`.
- Produces:
  - `UsageRecorder(session, *, client, route)` with `.request_id: str`,
    `.key_id: str`, `.elapsed_ms: int`, and
    `async finish(status_code, detail=None, *, bytes_in=0, headers=None, **columns) -> None`.
  - `StreamedUpload` (dataclass): `.path: Path`, `.size: int`, `.exceeded: bool`.
  - `async stream_to_temp(upload, *, prefix, suffix, max_bytes) -> StreamedUpload`.
  - `async enforce_rate_limit(limiter, recorder) -> None`.
  - Task 6 uses all three.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_publicapi_route_helpers.py`:

```python
"""The per-route policies every external endpoint shares.

These lived inline in a 322-line ocr_router.py. Endpoint three is where
hand-copying them starts silently going wrong, so they moved here first.
"""

import asyncio
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.publicapi import _route


@dataclass
class _FakeClient:
    key_id: str = "key-1"
    name: str = "test"
    scopes: tuple = ("ocr:read",)


class _FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeUpload:
    """Mimics the two methods of UploadFile that stream_to_temp uses."""

    def __init__(self, payload: bytes, chunk: int = 7):
        self._data = payload
        self._pos = 0
        self._chunk = chunk

    async def read(self, n):
        out = self._data[self._pos : self._pos + min(n, self._chunk)]
        self._pos += len(out)
        return out


def test_finish_writes_exactly_one_usage_row_and_commits():
    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="POST /x")
        await rec.finish(200, None, bytes_in=99)
        return session

    session = asyncio.run(go())
    assert len(session.added) == 1
    assert session.commits == 1
    row = session.added[0]
    assert row.status_code == 200 and row.bytes_in == 99
    assert row.route == "POST /x" and row.api_key_id == "key-1"


def test_finish_raises_when_given_a_detail_and_still_records_first():
    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="POST /x")
        with pytest.raises(HTTPException) as exc:
            await rec.finish(413, "too big", bytes_in=5)
        return session, exc.value

    session, exc = asyncio.run(go())
    assert exc.status_code == 413 and exc.detail == "too big"
    assert len(session.added) == 1, "the row must be written BEFORE the raise"


def test_finish_passes_extra_columns_through():
    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="POST /x")
        await rec.finish(200, None, bytes_in=1, width=40, height=20, lines_out=3)
        return session.added[0]

    row = asyncio.run(go())
    assert (row.width, row.height, row.lines_out) == (40, 20, 3)


def test_a_request_id_is_minted_per_recorder():
    a = _route.UsageRecorder(_FakeSession(), client=_FakeClient(), route="r")
    b = _route.UsageRecorder(_FakeSession(), client=_FakeClient(), route="r")
    assert a.request_id != b.request_id
    assert len(a.request_id) == 32


def test_stream_to_temp_writes_the_bytes_and_reports_the_size(tmp_path):
    async def go():
        up = _FakeUpload(b"hello world, this is a body")
        return await _route.stream_to_temp(
            up, prefix="t-", suffix=".txt", max_bytes=1000
        )

    streamed = asyncio.run(go())
    try:
        assert streamed.exceeded is False
        assert streamed.size == 27
        assert streamed.path.read_bytes() == b"hello world, this is a body"
    finally:
        streamed.path.unlink(missing_ok=True)


def test_stream_to_temp_stops_at_the_cap_and_still_returns_a_path_to_unlink():
    async def go():
        up = _FakeUpload(b"x" * 500)
        return await _route.stream_to_temp(
            up, prefix="t-", suffix=".bin", max_bytes=100
        )

    streamed = asyncio.run(go())
    try:
        assert streamed.exceeded is True
        assert streamed.size > 100
        # The path exists even on the over-cap path: the caller unlinks in
        # `finally`, and a None here would leak the partial file.
        assert streamed.path.exists()
    finally:
        streamed.path.unlink(missing_ok=True)


def test_enforce_rate_limit_is_a_no_op_when_the_bucket_allows():
    from app.apikeys.throttle import RateLimiter

    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="r")
        await _route.enforce_rate_limit(RateLimiter(per_minute=60, burst=5), rec)
        return session

    session = asyncio.run(go())
    assert session.added == []


def test_enforce_rate_limit_429s_with_retry_after_and_records_a_row():
    from app.apikeys.throttle import RateLimiter

    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="r")
        limiter = RateLimiter(per_minute=60, burst=1)
        await _route.enforce_rate_limit(limiter, rec)      # consumes the token
        with pytest.raises(HTTPException) as exc:
            await _route.enforce_rate_limit(limiter, rec)
        return session, exc.value

    session, exc = asyncio.run(go())
    assert exc.status_code == 429
    assert exc.detail == "Rate limit exceeded for this API key"
    assert int(exc.headers["Retry-After"]) >= 1
    assert len(session.added) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest tests/test_publicapi_route_helpers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.publicapi._route'`

- [ ] **Step 3: Write the helpers**

Create `app/publicapi/_route.py`:

```python
"""The per-request policies every external API route shares.

These were invented inline in `ocr_router.py` and each of them exists because
getting it wrong is silent:

  * **A usage row is written before the response is raised, on every
    attributable path.** It is the only evidence of what a key did. Writing it
    after the raise means never writing it.
  * **`X-Request-Id` is minted here and belongs on the 200 only.** Every error
    path raises an `HTTPException`, and FastAPI builds that response from the
    exception — a header set on the success `Response` never reaches the
    client. Do not tell a caller to quote an id on a failure.
  * **An upload is streamed and counted, never `await file.read()` whole.** The
    cap has to bite before the bytes are all in memory.
  * **The temp path is returned even when the cap was exceeded**, so the
    caller's `finally` can unlink it. Returning None there leaks the partial
    file, and we told the caller we do not keep their uploads.
  * **The rate limit is checked before touching disk.**

Kept deliberately small. This is a policy toolbox, not a framework: a route
still reads top to bottom.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..apikeys.dependencies import ApiClient
from ..apikeys.repository import record_usage
from ..apikeys.throttle import RateLimiter

__all__ = [
    "UsageRecorder",
    "StreamedUpload",
    "stream_to_temp",
    "enforce_rate_limit",
    "CHUNK_BYTES",
]

CHUNK_BYTES = 1024 * 1024


class UsageRecorder:
    """One per request: mints the request id, times the call, writes the row."""

    def __init__(
        self, session: AsyncSession, *, client: ApiClient, route: str
    ) -> None:
        self._session = session
        self._client = client
        self._route = route
        self._started = time.monotonic()
        self.request_id = uuid4().hex

    @property
    def key_id(self) -> str:
        return self._client.key_id

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    async def finish(
        self,
        status_code: int,
        detail: str | None = None,
        *,
        bytes_in: int = 0,
        headers: dict[str, str] | None = None,
        **columns,
    ) -> None:
        """Record the row, commit, then raise if `detail` was given.

        `**columns` passes route-specific measurements straight through to
        `record_usage` (`width`/`height`/`lines_out` for OCR; nothing for a
        text extract, whose columns stay NULL).
        """
        await record_usage(
            self._session,
            api_key_id=self._client.key_id,
            route=self._route,
            status_code=status_code,
            bytes_in=bytes_in,
            duration_ms=self.elapsed_ms,
            **columns,
        )
        await self._session.commit()
        if detail is not None:
            raise HTTPException(
                status_code=status_code, detail=detail, headers=headers
            )


@dataclass(frozen=True)
class StreamedUpload:
    path: Path
    size: int
    exceeded: bool


async def stream_to_temp(
    upload, *, prefix: str, suffix: str, max_bytes: int
) -> StreamedUpload:
    """Stream an UploadFile to a temp file, stopping once `max_bytes` is passed.

    Returns the path in EVERY case, including the over-cap one, so the caller
    can unlink it. `size` is the count at the moment streaming stopped, which
    is what the usage row should record.
    """
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(name)
    size = 0
    exceeded = False
    with os.fdopen(fd, "wb") as out:
        while True:
            chunk = await upload.read(CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                exceeded = True
                break
            out.write(chunk)
    return StreamedUpload(path=path, size=size, exceeded=exceeded)


async def enforce_rate_limit(limiter: RateLimiter, recorder: UsageRecorder) -> None:
    """429 with a `Retry-After` if this key has spent its bucket."""
    wait = limiter.check(recorder.key_id)
    if wait is not None:
        await recorder.finish(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded for this API key",
            bytes_in=0,
            headers={"Retry-After": str(wait)},
        )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `.venv/bin/pytest tests/test_publicapi_route_helpers.py -q`
Expected: 8 passed

- [ ] **Step 5: Move `/v1/ocr` onto the helpers**

In `app/publicapi/ocr_router.py`:

1. Add `from . import _route` to the imports; drop the now-unused `os`,
   `tempfile`, `uuid4`, `record_usage` and `get_rate_limiter` imports **only
   if nothing else still uses them**.
2. Replace the `request_id`/`started`/`finish` preamble with:

```python
    settings = get_settings()
    recorder = _route.UsageRecorder(session, client=client, route=_ROUTE)
    request_id = recorder.request_id
    response.headers["X-Request-Id"] = request_id
    dest: Path | None = None
    size = 0
    summary = None

    async def finish(status_code: int, detail: str | None = None, lines: int | None = None):
        """Record the usage row, then raise or return. Called on EVERY path."""
        await recorder.finish(
            status_code,
            detail,
            bytes_in=size,
            width=summary.width if summary else None,
            height=summary.height if summary else None,
            lines_out=lines,
        )
```

3. Replace step 1 (the rate-limit block) with:

```python
        # 1) rate limit, before touching disk
        await _route.enforce_rate_limit(get_rate_limiter(), recorder)
```

4. Replace step 4 (the manual `mkstemp` + read loop) with:

```python
        # 4) stream to a temp file, counting bytes (413 before any decode)
        streamed = await _route.stream_to_temp(
            file, prefix="ocr-", suffix=ext, max_bytes=settings.ocr_max_upload_bytes
        )
        dest, size = streamed.path, streamed.size
        if streamed.exceeded:
            await finish(
                413,
                f"image exceeds the "
                f"{settings.ocr_max_upload_bytes // (1024 * 1024)} MB limit",
            )
        if size == 0:
            await finish(400, "uploaded file is empty")
```

5. Replace the two remaining inline `record_usage(...) + commit + raise`
   blocks (the 503-at-capacity one) with `await recorder.finish(...)`:

```python
        except asyncio.TimeoutError:
            await recorder.finish(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "OCR is at capacity; retry shortly",
                bytes_in=size,
                headers={"Retry-After": "5"},
                width=summary.width,
                height=summary.height,
            )
```

Leave every message string, status code and comment exactly as it is. This is
a move, not a rewrite.

- [ ] **Step 6: Prove the refactor changed nothing**

```bash
set -a && . ./.env && set +a
.venv/bin/pytest tests/test_ocr_api_integration.py tests/test_ocr_api_boundaries.py \
                 tests/test_apikey_rate_limit.py -q
```
Expected: 46 passed, 1 skipped in the integration module (the "OCR stack
absent" negative) and 12 passed in boundaries — **the same counts as before
the refactor**, with no test file edited in this task. If any test needed a
change, revert and redo: the refactor altered behaviour.

- [ ] **Step 7: Run the live eval too, since it exercises the real 200 path**

```bash
set -a && . ./.env && set +a
OCR_LIVE_TESTS=1 .venv/bin/pytest tests/test_ocr_api_eval.py -q
```
Expected: 15 passed. If the OCR stack is absent in your environment this
SKIPS — say so in the task report rather than claiming it passed.

- [ ] **Step 8: Commit**

```bash
git add app/publicapi/_route.py app/publicapi/ocr_router.py \
        tests/test_publicapi_route_helpers.py
git commit -m "refactor(publicapi): factor the five per-route policies out of ocr_router"
```

---

### Task 5: A path-aware upload guard

**Files:**
- Modify: `app/publicapi/middleware.py`
- Modify: `app/main.py:23,119-133,183-188`
- Modify: `app/config.py:258-295` (add the setting), `app/config.py:299+` (validate it)
- Modify: `tests/test_ocr_api_boundaries.py:190,202,225,256`,
  `tests/test_ocr_api_integration.py:332,413,422,450,456`
- Test: `tests/test_upload_guard.py`

**Interfaces:**
- Consumes: `Settings.ocr_max_upload_bytes`.
- Produces: `app.publicapi.middleware.UploadContentLengthGuard` (renamed from
  `OcrContentLengthGuard`), `middleware.UPLOAD_CAPS: dict[str, str]` mapping
  request path to the `Settings` attribute holding its cap, and
  `Settings.extract_max_upload_bytes: int = 25 * 1024 * 1024`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_upload_guard.py`:

```python
"""The declared-Content-Length guard, now covering more than one path.

It is a narrow, provably-correct PRE-AUTH gate: FastAPI spools a multipart
file part to disk before any dependency runs, so without this an attacker with
no key can make the gateway write an arbitrarily large body before it answers
401. It is NOT a substitute for a reverse-proxy cap — a client that lies about
its Content-Length, or omits it, sails past any declared-length check.
"""

import asyncio

from app.publicapi.middleware import UPLOAD_CAPS, UploadContentLengthGuard


def _call(path, method="POST", content_length=None):
    """Drive the ASGI guard directly and report (status, inner_was_called)."""
    seen = {"inner": False}
    sent = []

    async def inner(scope, receive, send):
        seen["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}

    async def send(message):
        if message["type"] == "http.response.start":
            sent.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(UploadContentLengthGuard(inner)(scope, receive, send))
    return sent[0], seen["inner"]


def test_both_upload_paths_are_covered():
    assert set(UPLOAD_CAPS) == {"/v1/ocr", "/v1/extract"}


def test_an_oversized_ocr_body_is_413ed_before_the_app_is_called():
    status, inner_called = _call("/v1/ocr", content_length=999_000_000)
    assert status == 413
    assert inner_called is False, "the guard must answer without calling inward"


def test_an_oversized_extract_body_is_413ed_too():
    status, inner_called = _call("/v1/extract", content_length=999_000_000)
    assert status == 413
    assert inner_called is False


def test_a_small_body_passes_through():
    status, inner_called = _call("/v1/ocr", content_length=100)
    assert status == 200 and inner_called is True


def test_an_unguarded_path_passes_through_whatever_it_declares():
    status, inner_called = _call("/v1/chat", content_length=999_000_000)
    assert status == 200 and inner_called is True


def test_a_GET_is_never_guarded():
    status, inner_called = _call("/v1/ocr", method="GET", content_length=999_000_000)
    assert status == 200 and inner_called is True


def test_a_chunked_request_with_no_content_length_is_let_through():
    # Refusing it would break a legitimate streaming client; the route's own
    # counted cap still applies once the body is actually read.
    status, inner_called = _call("/v1/extract", content_length=None)
    assert status == 200 and inner_called is True


def test_the_two_paths_can_carry_DIFFERENT_caps():
    from app.config import get_settings

    settings = get_settings()
    assert settings.extract_max_upload_bytes != settings.ocr_max_upload_bytes, (
        "a 10 MB image cap is the wrong cap for a PDF — if these are ever "
        "equal by intent, delete this test and say why"
    )
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest tests/test_upload_guard.py -q`
Expected: FAIL — `ImportError: cannot import name 'UPLOAD_CAPS'`

- [ ] **Step 3: Add the setting**

In `app/config.py`, after the `ocr_max_upload_bytes` line (~274):

```python
    # A PDF is legitimately bigger than a scanned page image, so /v1/extract
    # carries its own cap. Two upload paths, two numbers — one shared cap
    # would either starve documents or over-admit images.
    extract_max_upload_bytes: int = 25 * 1024 * 1024
```

In `_validate_auth_and_external_api_settings`, beside the existing
`ocr_max_upload_bytes` check (~320):

```python
        if self.extract_max_upload_bytes < 1024:
            raise ValueError("EXTRACT_MAX_UPLOAD_BYTES must be at least 1024")
```

- [ ] **Step 4: Generalise the guard**

In `app/publicapi/middleware.py`, replace the `OCR_PATH` constant and the
class with:

```python
# Request path -> the `Settings` attribute holding that path's cap. Two upload
# paths with two different numbers: a 10 MB image cap is the wrong cap for a
# PDF. Adding a third upload route means adding a line here, and the M-e
# caveat below then applies to it too.
UPLOAD_CAPS: dict[str, str] = {
    "/v1/ocr": "ocr_max_upload_bytes",
    "/v1/extract": "extract_max_upload_bytes",
}


class UploadContentLengthGuard:
    """413s an upload whose DECLARED `Content-Length` exceeds its path's cap,
    before Starlette/FastAPI parse the body at all."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        setting = (
            UPLOAD_CAPS.get(scope.get("path"))
            if scope["type"] == "http" and scope.get("method") == "POST"
            else None
        )
        if setting is None:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = None
            if length is not None:
                # Read fresh every request (not cached at construction) so a
                # settings-cache-clearing test, or a live config reload, is
                # honoured the same way the routes' own checks already are.
                max_bytes = getattr(get_settings(), setting)
                if length > max_bytes:
                    response = JSONResponse(
                        {
                            "detail": (
                                f"upload exceeds the {max_bytes // (1024 * 1024)} "
                                "MB limit"
                            )
                        },
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
```

Update the module docstring: the M-e `--root-path` caveat now applies to
**every** key in `UPLOAD_CAPS`, not just `/v1/ocr`. Say that explicitly —
the trap gets worse with each path added.

- [ ] **Step 5: Update the two call sites in `app/main.py`**

Change the import on line 23 to
`from .publicapi.middleware import UploadContentLengthGuard`, the
`app.add_middleware(...)` call on line 133 to use the new name, and the three
comments (lines 119, 183, 188) that name the old class. The **ordering must
not change** — it is still added BEFORE `CORSMiddleware` so CORS ends up
outermost and the 413 carries CORS headers.

- [ ] **Step 6: Update the tests that name the old class**

`tests/test_ocr_api_boundaries.py` lines 190, 202, 225, 256 and
`tests/test_ocr_api_integration.py` lines 332, 413, 422, 450, 456 reference
`OcrContentLengthGuard` (four of them as strings inside subprocess source).
Rename all nine occurrences to `UploadContentLengthGuard`. Change nothing
else in those tests.

Also update the 413 detail assertions if any test matches the old wording
`"image exceeds the"` — the message is now `"upload exceeds the"`. Grep for it:

```bash
grep -rn "image exceeds the" tests/ app/ docs/
```

- [ ] **Step 7: Run everything that touches the guard**

```bash
set -a && . ./.env && set +a
.venv/bin/pytest tests/test_upload_guard.py tests/test_ocr_api_boundaries.py \
                 tests/test_ocr_api_integration.py -q
```
Expected: 8 + 12 + 46 passed, 1 skipped.

- [ ] **Step 8: Note the one-task window, then commit**

Between this task and Task 6 the guard knows `/v1/extract` but no route serves
it, so an oversized POST there answers **413 instead of 404** for the length of
one commit. That is the same shape of disclosure
`test_with_the_switch_unset_the_guard_middleware_is_absent` exists to prevent —
harmless here because it is a transient state inside one branch and the route
lands in the very next task, but do not ship a release from this commit.

```bash
git add app/publicapi/middleware.py app/main.py app/config.py \
        tests/test_upload_guard.py tests/test_ocr_api_boundaries.py \
        tests/test_ocr_api_integration.py
git commit -m "refactor(publicapi): the upload guard is path-aware, with a per-path cap"
```

---

### Task 6: `POST /v1/extract`

**Files:**
- Create: `app/publicapi/extract_router.py`
- Modify: `app/apikeys/throttle.py` (a second limiter), `app/config.py`
  (its settings), `app/main.py` (registration)
- Test: `tests/test_extract_api_integration.py`

**Interfaces:**
- Consumes: `policy.SCOPE_DOCUMENT_READ` (Task 1),
  `extraction.{read_any, EXTRACT_EXTS, ExtractedText}` (Task 2),
  `extract_schemas.{ExtractResponse, build_extract_response}` (Task 3),
  `_route.{UsageRecorder, stream_to_temp, enforce_rate_limit}` (Task 4),
  `Settings.extract_max_upload_bytes` (Task 5).
- Produces: `app.publicapi.extract_router.router`, mounted in `app/main.py`;
  `throttle.get_extract_rate_limiter() -> RateLimiter`;
  `Settings.extract_rate_per_minute: int = 60`,
  `Settings.extract_rate_burst: int = 20`,
  `Settings.extract_max_concurrent: int = 4`,
  `Settings.extract_queue_wait_seconds: int = 10`.

- [ ] **Step 1: Add the four settings**

In `app/config.py`, after `extract_max_upload_bytes`:

```python
    # A text parse is far cheaper than an OCR call, which is far cheaper than
    # a model pass. One bucket cannot govern all three, so /v1/extract carries
    # its own. Per PROCESS, like every limiter here: N workers means N x this.
    extract_rate_per_minute: int = 60
    extract_rate_burst: int = 20
    # Parsing a 500-page PDF is CPU-bound. `to_thread` keeps it off the event
    # loop; this cap keeps the default executor from running many at once.
    extract_max_concurrent: int = 4
    extract_queue_wait_seconds: int = 10
```

And in the validator:

```python
        if self.extract_rate_per_minute < 1 or self.extract_rate_burst < 1:
            raise ValueError(
                "EXTRACT_RATE_PER_MINUTE and EXTRACT_RATE_BURST must be at least 1"
            )
        if self.extract_max_concurrent < 1:
            raise ValueError("EXTRACT_MAX_CONCURRENT must be at least 1")
        if self.extract_queue_wait_seconds < 1:
            raise ValueError("EXTRACT_QUEUE_WAIT_SECONDS must be at least 1")
```

- [ ] **Step 2: Add the second rate limiter**

In `app/apikeys/throttle.py`, add `"get_extract_rate_limiter"` to `__all__`,
add a module-level `_extract_rate_limiter: RateLimiter | None = None`, and:

```python
def get_extract_rate_limiter() -> RateLimiter:
    """Process-wide singleton for /v1/extract's own bucket.

    Deliberately NOT the OCR limiter: a text parse costs a fraction of an OCR
    call, and making an extract consume an OCR token would silently couple two
    unrelated capacity decisions. Same reasoning as keeping the API-key
    lockout tuning separate from `LOGIN_MAX_ATTEMPTS`.
    """
    global _extract_rate_limiter
    if _extract_rate_limiter is None:
        settings = get_settings()
        _extract_rate_limiter = RateLimiter(
            per_minute=settings.extract_rate_per_minute,
            burst=settings.extract_rate_burst,
        )
    return _extract_rate_limiter
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_extract_api_integration.py`:

```python
"""Integration tests for POST /v1/extract.

None of these need the OCR stack: every case uses a native format, because the
image branch is `/v1/ocr`'s engine and is already covered there. The test
client mechanism mirrors tests/test_ocr_api_integration.py — read that file's
docstring for why `_client()` must be entered as a context manager.
"""

import contextlib
import io
import os

import pytest

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

PASSWORD = "supersecret123"
ADMIN_EMAIL = "admin@example.com"


@contextlib.contextmanager
def _client():
    from fastapi.testclient import TestClient

    from app.config import get_settings

    previous = os.environ.get("EXTERNAL_API_ENABLED")
    os.environ["EXTERNAL_API_ENABLED"] = "true"
    get_settings.cache_clear()
    import importlib

    import app.main

    importlib.reload(app.main)
    try:
        with TestClient(app.main.app) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop("EXTERNAL_API_ENABLED", None)
        else:
            os.environ["EXTERNAL_API_ENABLED"] = previous
        get_settings.cache_clear()


def _admin_headers(client):
    resp = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    if resp.status_code != 200:
        pytest.skip(f"cannot log in as {ADMIN_EMAIL} ({resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _mint(client, name, scopes):
    resp = client.post(
        "/v1/api-keys", json={"name": name, "scopes": scopes},
        headers=_admin_headers(client),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def _post(client, key, *, filename="a.txt", data=b"hello\nworld\n", ctype="text/plain"):
    return client.post(
        "/v1/extract",
        files={"file": (filename, data, ctype)},
        headers={"X-API-Key": key},
    )


def _pdf_bytes(text="Gross Pay 87500"):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(60, 10, text)
    return bytes(pdf.output())


def test_a_txt_extract_is_native_and_carries_no_caveat():
    with _client() as client:
        key = _mint(client, "e1", ["document:read"])
        resp = _post(client, key)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == "hello\nworld"
        assert body["source"]["route"] == "native"
        assert body["source"]["authoritative"] is True
        assert "caveat" not in body["source"]
        assert body["request_id"]


def test_the_request_id_is_on_the_200_header_too():
    with _client() as client:
        key = _mint(client, "e2", ["document:read"])
        resp = _post(client, key)
        assert resp.headers["X-Request-Id"] == resp.json()["request_id"]


def test_a_pdf_reports_its_page_counts():
    with _client() as client:
        key = _mint(client, "e3", ["document:read"])
        resp = _post(client, key, filename="a.pdf", data=_pdf_bytes(),
                     ctype="application/pdf")
        assert resp.status_code == 200, resp.text
        src = resp.json()["source"]
        assert src["pages"] == 1 and src["text_pages"] == 1


def test_a_csv_comes_back_as_sheets_with_an_empty_text():
    with _client() as client:
        key = _mint(client, "e4", ["document:read"])
        resp = _post(client, key, filename="a.csv",
                     data=b"name,amount\nalice,10\n", ctype="text/csv")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == ""
        assert body["sheets"][0]["headers"] == ["name", "amount"]


def test_an_ocr_only_key_is_403_not_401():
    with _client() as client:
        key = _mint(client, "e5", ["ocr:read"])
        resp = _post(client, key)
        assert resp.status_code == 403
        assert "document:read" in resp.json()["detail"]


def test_every_bad_credential_gets_the_same_401_body():
    with _client() as client:
        for key in ("", "garbage", "lgw_live_00000000_nosuchsecret"):
            resp = _post(client, key)
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Invalid API key"


def test_an_unsupported_extension_is_400():
    with _client() as client:
        key = _mint(client, "e6", ["document:read"])
        resp = _post(client, key, filename="a.exe", data=b"MZ", ctype="application/x-msdownload")
        assert resp.status_code == 400
        assert "not a supported" in resp.json()["detail"].lower()


def test_an_empty_upload_is_400():
    with _client() as client:
        key = _mint(client, "e7", ["document:read"])
        resp = _post(client, key, data=b"")
        assert resp.status_code == 400


def test_a_document_read_key_cannot_reach_the_ocr_route():
    """The reverse of the test below. Scope separation is only a boundary if
    it holds in BOTH directions — a key minted for text extraction must not
    quietly acquire the model-adjacent OCR route, and vice versa."""
    with _client() as client:
        key = _mint(client, "e10", ["document:read"])
        resp = client.post(
            "/v1/ocr",
            files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 403
        assert "ocr:read" in resp.json()["detail"]


def test_a_jwt_cannot_be_used_on_the_extract_route():
    with _client() as client:
        resp = client.post(
            "/v1/extract",
            files={"file": ("a.txt", b"hi", "text/plain")},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 401


def test_a_scanned_pdf_is_422_and_says_so():
    # A PDF whose pages exist but yield no text. fpdf2 makes one by drawing
    # only a rectangle — no text operators, so pypdf finds no text layer.
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.rect(10, 10, 50, 50)
    with _client() as client:
        key = _mint(client, "e8", ["document:read"])
        resp = _post(client, key, filename="scan.pdf", data=bytes(pdf.output()),
                     ctype="application/pdf")
        assert resp.status_code == 422, resp.text
        assert "no text layer" in resp.json()["detail"].lower()


def test_a_usage_row_is_written_for_a_success_and_for_a_403():
    import asyncio

    from sqlalchemy import text as sql_text

    with _client() as client:
        good = _mint(client, "e9-good", ["document:read"])
        bad = _mint(client, "e9-bad", ["ocr:read"])
        assert _post(client, good).status_code == 200
        assert _post(client, bad).status_code == 403

    async def count():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(DB_URL, poolclass=None)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    sql_text(
                        "SELECT status_code, count(*) FROM api_key_usage "
                        "WHERE route = 'POST /v1/extract' GROUP BY 1"
                    )
                )
                return dict(rows.all())
        finally:
            await engine.dispose()

    by_status = asyncio.run(count())
    assert by_status.get(200, 0) >= 1
    assert by_status.get(403, 0) >= 1
```

- [ ] **Step 4: Run them and watch them fail**

Run: `set -a && . ./.env && set +a && .venv/bin/pytest tests/test_extract_api_integration.py -q`
Expected: FAIL — every request 404s, because the route does not exist.

- [ ] **Step 5: Write the router**

Create `app/publicapi/extract_router.py`:

```python
"""POST /v1/extract — the text and structure of one document.

The engines are all pre-existing and pure; this file is the HTTP boundary and
nothing else. Two things it does NOT do, deliberately:

  * **It never calls a language model.** Named-field extraction is
    `/v1/extract/fields` (Phase B) behind its own scope, so a key provisioned
    for text cannot silently buy model access by adding a form field.
  * **It never infers "scanned" from empty text.** `extraction.read_any`
    reports `text_pages == 0` as a FACT and this route turns it into a 422.
    docs/nrb-integration.md §18 found five deployment defects that all produced
    successful operations with no text; a 200 with empty text is the worst
    outcome available, because the caller writes "no text found" into a file.

`asyncio.to_thread` is mandatory, not an optimisation: parsing a 500-page PDF
is synchronous and CPU-bound, and doing it in an `async def` stalls the whole
event loop for every in-flight chat stream in this worker. The semaphore is
separate because `to_thread`'s default executor is much larger.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..apikeys.dependencies import ApiClient, require_api_client
from ..apikeys.policy import SCOPE_DOCUMENT_READ
from ..apikeys.throttle import get_extract_rate_limiter
from ..config import get_settings
from ..db.session import get_session
from ..files import image_ocr, readers
from . import _route, extraction
from .extract_schemas import ExtractResponse, build_extract_response

logger = logging.getLogger("app.publicapi.extract")

router = APIRouter(prefix="/v1", tags=["extract"])

_ROUTE = "POST /v1/extract"

_slots: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(get_settings().extract_max_concurrent)
    return _slots


@router.post(
    "/extract",
    response_model=ExtractResponse,
    summary="Extract text and structure from one document (API key, scope document:read)",
    responses={
        400: {"description": "Unsupported extension, empty upload, corrupt file, or a bad `lang`."},
        401: {"description": "The API key is missing, malformed, unknown, wrong, revoked or expired — one message for all six causes."},
        403: {"description": "The key is genuine but lacks `document:read`. Ask an admin to re-mint it; do not rotate the key."},
        413: {"description": "Over `EXTRACT_MAX_UPLOAD_BYTES` (default 25 MB)."},
        422: {"description": "A PDF whose pages carry no text layer. Its text needs OCR, which this endpoint does not do for PDFs."},
        429: {"description": "This key's rate limit, or its prefix is credential-locked. `Retry-After` and the detail distinguish them."},
        503: {"description": "The box is at capacity right now (`EXTRACT_MAX_CONCURRENT`/`EXTRACT_QUEUE_WAIT_SECONDS`), or — for an IMAGE upload only — the OCR stack is not installed."},
        500: {"description": "An unexpected failure. Logged server-side, never echoed; report it rather than retrying."},
    },
)
async def extract(
    response: Response,
    file: UploadFile,
    lang: str | None = Form(default=None),
    client: ApiClient = Depends(require_api_client(SCOPE_DOCUMENT_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Return the text of one uploaded document, plus what kind of text it is.

    Accepts `.pdf .docx .txt .md .json` (read from the document's own text
    layer), `.xlsx .csv` (returned as `sheets`, not flat lines), and
    `.png .jpg .jpeg .webp .tif .tiff .bmp` (read by OCR).

    The `source` block is the part to read first. `route: "native"` means the
    text came from the document's own text layer and is exact —
    `authoritative` is true and there is no caveat. `route: "ocr"` means it was
    machine-read: `authoritative` is false, a `caveat` is present, and no
    figure, date, account number or contact detail from it should be treated as
    correct without being checked against the original.

    A PDF whose pages carry no text layer is a **422**, not an empty 200 — its
    text needs OCR, and silently returning nothing would read as "this document
    is blank". `lang` applies to image uploads only.
    """
    settings = get_settings()
    recorder = _route.UsageRecorder(session, client=client, route=_ROUTE)
    response.headers["X-Request-Id"] = recorder.request_id
    dest: Path | None = None
    size = 0

    async def finish(status_code: int, detail: str | None = None, lines: int | None = None):
        await recorder.finish(status_code, detail, bytes_in=size, lines_out=lines)

    try:
        await _route.enforce_rate_limit(get_extract_rate_limiter(), recorder)

        chosen = (lang or image_ocr.DEFAULT_LANG).strip()
        if chosen not in image_ocr.SUPPORTED_LANGS:
            await finish(
                400,
                f"unsupported lang '{chosen}' (supported: "
                f"{', '.join(sorted(image_ocr.SUPPORTED_LANGS))})",
            )

        ext = Path(file.filename or "").suffix.lower()
        if ext not in extraction.EXTRACT_EXTS:
            # The caller's own unbounded filename, reflected back. JSON-encoded
            # so it is not an injection, but truncate it anyway.
            shown = (ext or (file.filename or ""))[:100]
            await finish(
                400,
                f"'{shown}' is not a supported document — /v1/extract accepts "
                f"{', '.join(sorted(extraction.EXTRACT_EXTS))}",
            )

        streamed = await _route.stream_to_temp(
            file, prefix="extract-", suffix=ext,
            max_bytes=settings.extract_max_upload_bytes,
        )
        dest, size = streamed.path, streamed.size
        if streamed.exceeded:
            await finish(
                413,
                f"upload exceeds the "
                f"{settings.extract_max_upload_bytes // (1024 * 1024)} MB limit",
            )
        if size == 0:
            await finish(400, "uploaded file is empty")

        try:
            await asyncio.wait_for(
                _semaphore().acquire(), timeout=settings.extract_queue_wait_seconds
            )
        except asyncio.TimeoutError:
            await recorder.finish(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "extraction is at capacity; retry shortly",
                bytes_in=size,
                headers={"Retry-After": "5"},
            )

        try:
            extracted = await asyncio.to_thread(
                extraction.read_any, dest, lang=chosen
            )
        except image_ocr.OcrUnavailable as exc:
            # Only reachable for an IMAGE upload. Same split as /v1/ocr: the
            # package importing but the engine failing to build is a 500,
            # genuinely absent is a 503 — collapsing them sends an operator to
            # rebuild with a flag that is already set.
            logger.warning("extract ocr unavailable (request %s): %s", recorder.request_id, exc)
            if image_ocr.available():
                await finish(500, "extraction failed unexpectedly")
            await finish(503, "image OCR is not enabled on this deployment")
        except readers.ReadError as exc:
            await finish(400, f"could not read the document ({exc})")
        except Exception:
            logger.exception("extract failed unexpectedly (request %s)", recorder.request_id)
            await finish(500, "extraction failed unexpectedly")
        finally:
            _semaphore().release()

        if extracted.is_scanned_pdf:
            await finish(
                422,
                f"this PDF has {extracted.pages} page(s) but no text layer — "
                "its text would need OCR, which /v1/extract does not do for PDFs",
            )

        await finish(200, None, lines=len(extracted.lines))
        logger.info(
            "extract ok request=%s key=%s kind=%s route=%s lines=%d %dms",
            recorder.request_id, client.key_id, extracted.kind,
            extracted.route, len(extracted.lines), recorder.elapsed_ms,
        )
        return build_extract_response(extracted, recorder.request_id)
    finally:
        if dest is not None:
            dest.unlink(missing_ok=True)
        await file.close()
```

- [ ] **Step 6: Register the router**

In `app/main.py`, add `from .publicapi.extract_router import router as extract_router`
beside the existing OCR import, and inside the SAME
`if get_settings().external_api_enabled:` block (line ~180):

```python
    app.include_router(extract_router)
```

- [ ] **Step 7: Run the tests**

Run: `set -a && . ./.env && set +a && .venv/bin/pytest tests/test_extract_api_integration.py -q`
Expected: 12 passed.

If `test_a_scanned_pdf_is_422_and_says_so` fails because fpdf2's
rectangle-only page still yields a text layer, replace that fixture with a
PDF built by `pypdf.PdfWriter().add_blank_page(width=200, height=200)` — a
genuinely blank page has pages but no text. Do not weaken the assertion.

- [ ] **Step 8: Prove the feature stays absent when the switch is off**

Append to `tests/test_ocr_api_boundaries.py`:

```python
def test_with_the_switch_unset_the_extract_route_is_absent_from_openapi():
    import importlib
    import os

    from app.config import get_settings

    previous = os.environ.get("EXTERNAL_API_ENABLED")
    os.environ["EXTERNAL_API_ENABLED"] = "false"
    get_settings.cache_clear()
    try:
        import app.main

        importlib.reload(app.main)
        assert "/v1/extract" not in app.main.app.openapi()["paths"]
    finally:
        if previous is None:
            os.environ.pop("EXTERNAL_API_ENABLED", None)
        else:
            os.environ["EXTERNAL_API_ENABLED"] = previous
        get_settings.cache_clear()
        importlib.reload(app.main)
```

Run: `.venv/bin/pytest tests/test_ocr_api_boundaries.py -q`
Expected: 14 passed (12 original + the switch test from this step + the one
added in Task 3's step 5 if it was renamed rather than added — count what you
actually see and report it).

- [ ] **Step 9: Commit**

```bash
git add app/publicapi/extract_router.py app/apikeys/throttle.py app/config.py \
        app/main.py tests/test_extract_api_integration.py tests/test_ocr_api_boundaries.py
git commit -m "feat(publicapi): POST /v1/extract — text and structure from one document"
```

---

### Task 7: The deterministic eval, and the docs

**Files:**
- Create: `tests/test_extract_api_eval.py`
- Modify: `docs/external-api.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a stated pass rate that `docs/external-api.md` cites.

**Why this eval can assert exact output and `/v1/ocr`'s cannot:** native
extraction is deterministic. The OCR eval scores aggregates because the engine
is measurably nondeterministic on Devanagari; here, the same DOCX yields the
same lines every time, so exact assertions are both possible and correct.

- [ ] **Step 1: Write the eval**

Create `tests/test_extract_api_eval.py`:

```python
"""A deterministic eval for POST /v1/extract.

Unlike tests/test_ocr_api_eval.py this asserts EXACT output, because native
extraction is deterministic — the same DOCX yields the same lines every run.
Only the image case is nondeterministic, and it is deliberately excluded: that
engine is already evaluated in tests/test_ocr_api_eval.py and re-scoring it
here would just import its nondeterminism.

Every case is built in-process, so this needs no fixture files and no network.
"""

import contextlib
import os

import pytest

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

from tests.test_extract_api_integration import _client, _mint, _post  # noqa: E402


def _docx_bytes(paragraphs):
    import io

    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(headers, rows):
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CASES = [
    ("txt-plain", "a.txt", b"alpha\nbeta\n", "text/plain",
     lambda b: b["text"] == "alpha\nbeta" and b["source"]["route"] == "native"),
    ("md-headings", "a.md", b"# Title\n\nBody text\n", "text/markdown",
     lambda b: "# Title" in b["text"] and "Body text" in b["text"]),
    ("json-object", "a.json", b'{"amount": 87500, "name": "Ramesh"}', "application/json",
     lambda b: "87500" in b["text"] and "Ramesh" in b["text"]),
    ("docx-paragraphs", "a.docx",
     _docx_bytes(["Employee: Ramesh Shrestha", "Gross Pay: 87,500.00"]),
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     lambda b: b["text"].splitlines() == ["Employee: Ramesh Shrestha", "Gross Pay: 87,500.00"]),
    ("xlsx-sheet", "a.xlsx",
     _xlsx_bytes(["name", "amount"], [["alice", 10], ["bob", 20]]),
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     lambda b: b["sheets"][0]["headers"] == ["name", "amount"]
               and b["sheets"][0]["total_rows"] == 2 and b["text"] == ""),
    ("csv-sheet", "a.csv", b"name,amount\nalice,10\n", "text/csv",
     lambda b: b["sheets"][0]["rows"] == [["alice", "10"]]),
    ("native-carries-no-caveat", "a.txt", b"anything\n", "text/plain",
     lambda b: "caveat" not in b["source"] and b["source"]["authoritative"] is True),
]


@pytest.mark.parametrize("name,filename,data,ctype,check", CASES,
                         ids=[c[0] for c in CASES])
def test_case(name, filename, data, ctype, check):
    with _client() as client:
        key = _mint(client, f"eval-{name}", ["document:read"])
        resp = _post(client, key, filename=filename, data=data, ctype=ctype)
        assert resp.status_code == 200, resp.text
        assert check(resp.json()), f"{name}: {resp.json()}"


def test_the_whole_eval_set_passes():
    """One aggregate so a partial regression cannot hide in a green module."""
    failures = []
    with _client() as client:
        for name, filename, data, ctype, check in CASES:
            key = _mint(client, f"agg-{name}", ["document:read"])
            resp = _post(client, key, filename=filename, data=data, ctype=ctype)
            if resp.status_code != 200 or not check(resp.json()):
                failures.append(name)
    assert not failures, f"{len(failures)}/{len(CASES)} failed: {failures}"
```

- [ ] **Step 2: Run it and record the real number**

Run: `set -a && . ./.env && set +a && .venv/bin/pytest tests/test_extract_api_eval.py -q`
Expected: 8 passed (7 cases + the aggregate). **Write down the number you
actually see** — it goes into the docs in the next step, and a number nobody
ran is worse than no number.

- [ ] **Step 3: Document the endpoint**

Add a `POST /v1/extract` section to `docs/external-api.md` covering:
- the accepted extensions, split by which route each family takes
- the `source` block, and that **`caveat` is absent for a native source** —
  with the reason, not just the fact
- the full status table (400/401/403/413/422/429/500/503), including that the
  422 is a scanned PDF and what a caller should do about it
- that `EXTRACT_MAX_UPLOAD_BYTES` is 25 MB while `/v1/ocr`'s is 10 MB, and
  that the nginx `client_max_body_size` prerequisite must now be sized for the
  **larger** of the two
- that the `--root-path` caveat now applies to both guarded paths
- that `/v1/extract` has its own rate bucket (`EXTRACT_RATE_PER_MINUTE`,
  `EXTRACT_RATE_BURST`), per process like every other limiter here
- an **Evaluation & Improvement** subsection: success metric (share of 200s the
  consuming app uses without a re-parse; owned proxy = the status split and the
  non-empty-text rate per key in `api_key_usage`), the eval above with the pass
  rate from step 2, feedback capture (`api_key_usage` only — no bytes, no
  text), and a monthly review loop plus "re-run the eval on any change to
  `documents.py`, `readers.py`, or the pypdf/openpyxl/python-docx pins".

- [ ] **Step 4: Update CLAUDE.md**

Two edits:
1. In the **Endpoints** section, under "External (API key…)", add `/v1/extract`
   beside `/v1/ocr` with its scope, accepted extensions and status list.
2. In **Conventions / gotchas**, add one entry: that `app/publicapi/_route.py`
   is now where the five per-route policies live, that a new external route
   must use it rather than re-deriving them, and that `caveat` is absent
   rather than null for native text — with the §29.2 over-warning reason.
3. In the **Layout** section, extend the `publicapi/` entry to name
   `_route.py`, `extraction.py`, `extract_schemas.py` and `extract_router.py`,
   and note that `middleware.UploadContentLengthGuard` is now path-aware.

- [ ] **Step 5: Run the whole suite in two halves**

The full suite OOM-kills this box in one process. Split it:

```bash
set -a && . ./.env && set +a
ls tests/test_*.py | head -65 > /tmp/h1.txt
ls tests/test_*.py | tail -70 > /tmp/h2.txt
.venv/bin/pytest -q -p no:randomly $(cat /tmp/h1.txt) | tail -3
.venv/bin/pytest -q -p no:randomly $(cat /tmp/h2.txt) | tail -3
```
Expected: **0 failures** across both halves. Baseline before this plan was
2487 passed / 30 skipped / 0 failed. Report the new totals and account for the
difference — every added test should be accounted for by a task above.

- [ ] **Step 6: Commit**

```bash
git add tests/test_extract_api_eval.py docs/external-api.md CLAUDE.md
git commit -m "docs+eval: POST /v1/extract runbook, deterministic eval, CLAUDE.md entries"
```

---

## Phase B is a separate plan

`POST /v1/extract/fields` is deliberately not in this plan. It needs
`publicapi/_route.py`'s interfaces to be real rather than predicted, and its
central design question — how badly the caller-supplied-field-name problem
(spec §5.2) bites — is unmeasured on the production model. Write that plan
after Phase A lands, and after `qwen3.5:35b-a3b` has been reached and §5.2's
probe re-run against it.
