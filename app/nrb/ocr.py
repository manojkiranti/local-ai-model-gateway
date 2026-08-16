"""The OCR boundary: one PDF page of pixels in, text out. Worker-side only.

**This file is the ONLY place in the repository that knows Docling's OCR stage
exists**, exactly as `legacy_font.py` is the only place that knows npttf2utf
exists. Everything else depends on the `PageOcrEngine` Protocol, so a different
engine is one new class rather than a sweep — and `app/nrb/recovery.py` can be
tested end to end with a stub on a machine that has no OCR stack at all.

WHAT IS CONFIGURED HERE, AND WHY EACH VALUE
-------------------------------------------
    engine   docling's OCR stage + RapidOCR
    model    PP-OCRv5, Devanagari
    backend  onnxruntime
    mode     force_full_page_ocr, table structure off

All four come from the measured A/B in `docs/nrb/phase6b-ocr-spike-v5.md`, not
from a default. The backend is load-bearing rather than incidental: docling's
`_resolve_rapidocr` reaches PP-OCRv4 through torch and **PP-OCRv5 only through
onnxruntime**, and v4 is rejected for Nepali. On 14 spike pages v4 produced
Devanagari characters in the wrong order and without conjuncts — halant per
Devanagari character 0.0042 and mean word length 24.7, against 0.0982 / 5.7 for
npttf2utf's own output on the same corpus — while v5 measured 0.0798 / 5.4,
unanimously better on every page and on both signals.

WHAT THIS DOES NOT CLAIM
------------------------
**OCR output is retrieval text, not a transcription.** On a 150 dpi scan v5 drops
letterheads, subject lines and whole body paragraphs, and it is unreliable on
latin runs (`lc_visakhapatnam@nrb.org.np` came back as noise). It must never be
treated as authoritative for a figure, a date, an account number or a contact
detail. There is no confidence score here on purpose: the spike measured
orthographic well-formedness, which is not a per-field correctness estimate, and
inventing a threshold from it would dress a guess as a measurement.

WHY IT DOES NOT REUSE `rag/parsing._docling_converter`
------------------------------------------------------
That converter is department RAG's, and it is deliberately `do_ocr=False` — a
contract `extraction.docling_pipeline_is_native()` actively asserts. Turning OCR
on there would change department RAG's behaviour to make NRB convenient, and
would point a Chinese/English-dictionary recogniser at every uploaded PDF. This
builds its own converter, and the two configurations are meant to differ.

DEPENDENCIES
------------
`rapidocr` and `onnxruntime` are declared in `requirements-worker.txt` and
deliberately NOT in `requirements.txt`, which is the only file `Dockerfile`
installs. The API image cannot acquire an OCR stack by accident — the same
structural guarantee `requirements-nrb.txt` gives npttf2utf. Every import here is
inside a function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("app.nrb.ocr")

__all__ = [
    "DoclingRapidOcrEngine",
    "OCR_BACKEND",
    "OCR_ENGINE",
    "OCR_LANG",
    "OCR_MODEL",
    "OcrUnavailable",
    "PageOcrEngine",
    "engine_version",
]

# Recorded on every recovered page, for the same reason a conversion records its
# mapping and backend version: "OCR'd" is not a reproducible claim, "PP-OCRv5
# devanagari via docling 2.118.1" is.
OCR_ENGINE = "docling+rapidocr"
OCR_MODEL = "PP-OCRv5"
OCR_LANG = "devanagari"
OCR_BACKEND = "onnxruntime"


class OcrUnavailable(RuntimeError):
    """OCR could not run, or ran and failed on this page.

    Raised rather than returning an empty string, for the same reason
    `ConverterUnavailable` exists: a stage that silently produced nothing looks
    exactly like a page that legitimately holds no text, and the recovery record
    has to tell those two apart.
    """


@runtime_checkable
class PageOcrEngine(Protocol):
    """One OCR engine, as `(file, page) -> text`.

    Page-addressed rather than document-addressed because routing is per page:
    `e08988860534` needs OCR on page 1 and the deterministic converter on pages
    2-50, and an engine that could only do whole documents would force the
    expensive answer onto 49 pages that do not need it.
    """

    name: str
    model: str
    version: str

    def ocr_page(self, path: Path, page_number: int) -> str:
        ...


def engine_version() -> str:
    """Installed versions of the two packages that decide the output.

    Best-effort: a missing version is reported as `unknown` rather than raising,
    because this is an evidence field and `ocr_page` is where unavailability is
    supposed to surface.
    """
    from importlib.metadata import PackageNotFoundError, version

    parts = []
    for package in ("docling", "rapidocr", "onnxruntime"):
        try:
            parts.append(f"{package} {version(package)}")
        except PackageNotFoundError:
            parts.append(f"{package} unknown")
    return "; ".join(parts)


@dataclass
class DoclingRapidOcrEngine:
    """PP-OCRv5 Devanagari, one converter reused across pages.

    Building a `DocumentConverter` loads the detection, classification and
    recognition models; doing it per page would make a 3-second page a
    15-second one. So the converter is built on first use and held — and
    `close()` drops it, because these models are hundreds of MB and a corpus pass
    should be able to release them.

    Not a module-level singleton: that would be global mutable state on an import
    path, and the engine is meant to be constructed by the caller that knows it
    is running in the worker.
    """

    name: str = OCR_ENGINE
    model: str = OCR_MODEL
    lang: str = OCR_LANG
    backend: str = OCR_BACKEND
    version: str = ""
    _converter: Any = field(default=None, repr=False)

    def open(self) -> tuple[bool, str]:
        """Build the converter. Returns `(ok, evidence)`; never raises.

        The evidence string names the model and backend so a run's log records
        which recogniser produced its text, rather than leaving it to be inferred
        from the installed package set later.
        """
        try:
            self._converter = self._build()
        except OcrUnavailable as exc:
            return False, str(exc)
        self.version = engine_version()
        return True, f"{self.model}/{self.lang} via {self.backend} ({self.version})"

    def _build(self) -> Any:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                RapidOcrOptions,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise OcrUnavailable(
                "docling is not installed. OCR is worker-side only — see "
                "requirements-worker.txt and the note in app/nrb/ocr.py."
            ) from exc

        options = PdfPipelineOptions()
        options.do_ocr = True
        # Layout table structure is a Phase 7 chunking concern and costs real
        # time per page. This stage is asked for text, nothing else.
        options.do_table_structure = False
        # Full page, not just the regions docling thinks lack text. Every page
        # that reaches here was routed BECAUSE its text layer is untrustworthy:
        # a scanner's hidden latin-alphabet guess is present but wrong, so
        # letting docling skip regions that already "have text" would preserve
        # exactly the garbage this route exists to replace.
        options.ocr_options = RapidOcrOptions(
            lang=[self.lang], backend=self.backend, force_full_page_ocr=True
        )
        return DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )

    def ocr_page(self, path: Path, page_number: int) -> str:
        """One page's text, or `OcrUnavailable`.

        `page_range` is inclusive on both ends and 1-indexed — the same numbering
        `provenance.PageProvenance.page_number` and `documents.read_pdf_pages`
        use, so a page number means one thing across the whole pipeline.
        """
        if self._converter is None:
            self._converter = self._build()
            self.version = engine_version()
        try:
            result = self._converter.convert(
                str(path), page_range=(page_number, page_number)
            )
            return result.document.export_to_text()
        except Exception as exc:  # noqa: BLE001 - the engine raises bare Exceptions
            logger.warning(
                "NRB OCR: page %d failed (%s)", page_number, type(exc).__name__
            )
            raise OcrUnavailable(
                f"{self.model} failed on page {page_number}: {type(exc).__name__}"
            ) from exc

    def close(self) -> None:
        """Drop the converter so its models can be collected."""
        self._converter = None

    def __enter__(self) -> "DoclingRapidOcrEngine":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
