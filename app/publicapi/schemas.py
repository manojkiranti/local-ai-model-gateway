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

from pydantic import BaseModel

from ..files.image_ocr import OCR_CAVEAT, OcrResult
from ..files.images import ImageSummary

CAVEAT = OCR_CAVEAT


class OcrLine(BaseModel):
    text: str
    confidence: float


class OcrEngineInfo(BaseModel):
    name: str
    model: str
    backend: str
    lang: str
    version: str


class OcrImageInfo(BaseModel):
    kind: str
    width: int
    height: int
    frames: int


class OcrResponse(BaseModel):
    text: str
    lines: list[OcrLine]
    # Never True. See limit 1 in image_ocr's module docstring.
    authoritative: bool = False
    caveat: str = CAVEAT
    partial: bool = False
    image: OcrImageInfo
    engine: OcrEngineInfo
    request_id: str


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
