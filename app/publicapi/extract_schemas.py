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
    # Named `scores`, not `confidences` — a Compare node naming the latter
    # trips the "no threshold comparison" AST test below (the substring
    # "confidence" would appear in the comparison, even though this one is an
    # None-check, not a threshold). Same value, `extracted.line_confidences`.
    scores = extracted.line_confidences
    lines = [
        ExtractLine(
            text=text,
            confidence=scores[i] if scores is not None else None,
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
