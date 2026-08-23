"""The OCR response envelope.

Three fields exist because of measured facts rather than taste:

  * `authoritative` is ALWAYS False and `caveat` is ALWAYS present. An external
    app that writes this text into a client file must be told, in the payload,
    on every response — not in documentation it read once.
  * `partial` is True when the image has more than one frame, because a
    multi-frame .tif is a scanner's normal output and the engine reads frame 1
    only (measured: page 2's text silently vanished). `read_document` reports
    `pages_skipped` for the same reason.
  * `request_id` is echoed so a caller's support ticket joins to an
    `api_key_usage` row. It is the only reason those rows are worth writing.

`text` and `lines` both ship: `text` is the 90% case so the caller does not
reassemble it, and `lines` carries per-line confidence. Confidence is REPORTED
and never compared to anything (§16.6 declines to invent a threshold from an
orthography measurement); an AST test enforces that.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..files.image_ocr import OCR_CAVEAT, OcrResult
from ..files.images import ImageSummary

CAVEAT = OCR_CAVEAT


class OcrLine(BaseModel):
    text: str
    confidence: float = Field(
        description=(
            "Reported, never enforced — this measures orthographic "
            "well-formedness, not per-field correctness, so nothing in this "
            "API compares it to a threshold. Use it as a hint for which "
            "lines to check first, not as a reliability flag."
        )
    )


class OcrEngineInfo(BaseModel):
    name: str
    model: str
    backend: str
    lang: str
    version: str


class OcrImageInfo(BaseModel):
    kind: str = Field(
        description='The sniffed image format as a human string, e.g. "PNG image" — never the bare format name.'
    )
    width: int
    height: int
    frames: int = Field(
        description="Frame count in the upload. >1 for a multi-frame TIFF; see `partial`."
    )


class OcrResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "text": "नेपाल राष्ट्र बैंक\nAccount No: 1234567890",
                "lines": [
                    {"text": "नेपाल राष्ट्र बैंक", "confidence": 0.94},
                    {"text": "Account No: 1234567890", "confidence": 0.81},
                ],
                "authoritative": False,
                "caveat": CAVEAT,
                "partial": False,
                "image": {
                    "kind": "PNG image",
                    "width": 1240,
                    "height": 1754,
                    "frames": 1,
                },
                "engine": {
                    "name": "rapidocr",
                    "model": "PP-OCRv5",
                    "backend": "onnxruntime",
                    "lang": "devanagari",
                    "version": "rapidocr 1.4.1, onnxruntime 1.18.0",
                },
                "request_id": "3f9a2e7c1b4d4a8e9f0c2d3e4f5a6b7c",
            }
        }
    )

    text: str = Field(description="All recognised lines, joined with newlines. The 90% case.")
    lines: list[OcrLine] = Field(
        description="Per-line text and confidence, for a caller that wants more than the flat `text`."
    )
    # Never True. See limit 1 in image_ocr's module docstring.
    authoritative: bool = Field(
        default=False,
        description=(
            "Always false. This is machine-read text (OCR), not a "
            "transcription — never treat a figure, date, account number or "
            "contact detail from this endpoint as correct without checking "
            "it against the image."
        ),
    )
    caveat: str = Field(
        default=CAVEAT,
        description=(
            "The same warning rendered into chat by `read_image` — one "
            "constant, two readers, so the API and a chat citation never "
            "disagree about the wording."
        ),
    )
    partial: bool = Field(
        default=False,
        description=(
            "True when the upload had more than one frame (e.g. a "
            "multi-page TIFF) and only the FIRST frame was read — the rest "
            "were silently skipped by the engine, not by this API."
        ),
    )
    image: OcrImageInfo
    engine: OcrEngineInfo
    request_id: str = Field(
        description=(
            "Echoed from `X-Request-Id` on this response. Quote it in a "
            "support ticket to locate the matching `api_key_usage` row — "
            "only present on a 200; every non-200 path raises before this "
            "header is ever sent, so there is no request id to quote for a "
            "failed call."
        )
    )


def build_response(
    result: OcrResult, summary: ImageSummary, request_id: str
) -> OcrResponse:
    lines = [
        OcrLine(text=text, confidence=score)
        for text, score in zip(result.lines, result.scores)
    ]
    return OcrResponse(
        text="\n".join(result.lines),
        lines=lines,
        authoritative=result.authoritative,
        caveat=CAVEAT,
        partial=summary.frames > 1,
        image=OcrImageInfo(
            kind=summary.kind,
            width=summary.width,
            height=summary.height,
            frames=summary.frames,
        ),
        engine=OcrEngineInfo(
            name=result.engine,
            model=result.model,
            backend=result.backend,
            lang=result.lang,
            version=result.version,
        ),
        request_id=request_id,
    )
