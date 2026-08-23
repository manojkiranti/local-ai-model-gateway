"""Text out of an uploaded IMAGE, via PP-OCRv5 on onnxruntime.

**This file is the ONLY place in the repository that knows `rapidocr` exists.**
Everything else goes through `ocr_image`, so a different engine is one new
module rather than a sweep.

Why not the NRB path (`app/nrb/ocr.py`)? That one reaches the same recogniser
*through docling*, because its input is a PDF PAGE and docling is what
rasterises it. Docling costs torch (1.1 G) + transformers (93 M) and must never
enter the API image. `RapidOCR.__call__` takes an image directly, so for a bare
upload docling is pure overhead: this module talks to rapidocr and imports no
part of docling.

What is reused is the expensive part — the MODEL and BACKEND decision recorded
in `docs/nrb-integration.md` §16.6. PP-OCRv4 is rejected for Nepali (it recovers
the script but not the orthography: halant per Devanagari char 0.0042 and mean
word length 24.7, against v5's 0.0798/5.4 and npttf2utf's reference 0.0982/5.7).
Docling reaches v4 through torch and v5 only through onnxruntime, so the backend
is load-bearing there; here we name the version outright.

Three limits are stated rather than engineered around, all from §16.6:

  1. **OCR output is retrieval text, not a transcription.** v5 drops letterheads,
     subject lines and whole paragraphs, and is unreliable on latin runs. It must
     never be treated as authoritative for a figure, a date, an account number
     or a contact detail — hence `OcrResult.authoritative` is always False.
  2. **There is no confidence THRESHOLD.** Per-line scores are reported because
     they are useful information, but nothing here compares one to a constant:
     the spike measured orthographic well-formedness, which is not a per-field
     correctness estimate, and a threshold derived from it would dress a guess
     as a measurement. A test asserts this by AST.
  3. **Conversion beats OCR where a font is embedded** (v5 renders कारवाही as
     शदक). That is why the NRB corpus routes per page and OCR is its narrow
     fallback. An uploaded photo has no font to recover, so OCR is all there is.

Dependency boundary: `rapidocr`/`onnxruntime` live in `requirements-ocr.txt`,
which `Dockerfile` installs only under `--build-arg INSTALL_OCR=true`. Every
import is inside a function, and a subprocess test asserts that importing the
tool registry pulls in none of rapidocr, onnxruntime or cv2.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger("app.files.image_ocr")

__all__ = [
    "OCR_ENGINE",
    "OCR_MODEL",
    "OCR_BACKEND",
    "DEFAULT_LANG",
    "SUPPORTED_LANGS",
    "OCR_CAVEAT",
    "OcrUnavailable",
    "OcrResult",
    "available",
    "prewarm",
    "engine_version",
    "ocr_config",
    "ocr_params",
    "group_lines",
    "ocr_image",
    "reset_engines",
    "engine_cache_info",
]

OCR_ENGINE = "rapidocr"
OCR_MODEL = "PP-OCRv5"
OCR_BACKEND = "onnxruntime"

# `devanagari` by default because it is the only single recogniser that reads
# BOTH scripts: its dictionary includes ASCII, so English comes back correctly
# (measured: "45,320.75" and "0123456789" exact), while a latin-only model
# returns *nothing* for Nepali. Degraded latin beats absent Devanagari.
DEFAULT_LANG = "devanagari"
SUPPORTED_LANGS = {"devanagari", "en"}

# ONE constant, TWO readers: `app/tools/local/read_image.py` renders it into the
# model's context, and `app/publicapi/schemas.py` publishes it to an external
# caller. A second copy drifts, and then a UI badge or an API field contradicts
# the answer text, leaving the reader unable to tell which to believe — exactly
# the reasoning behind `app/rag/sources.py`'s VERIFY_NOTE.
#
# It says what §16.6 measured: PP-OCRv5 drops letterheads and subject lines,
# mangles latin runs, and misreads dates (२०६९।१।३१ as २०६९।९।३१).
OCR_CAVEAT = (
    "CAVEAT: this is machine-read text (OCR), not a transcription — words and "
    "whole lines can be dropped or misread. VERIFY every figure, date, account "
    "number and contact detail against the image itself before relying on it, "
    "and say so when you quote one."
)

# The configuration, declared as plain strings so it can be asserted in an
# environment where rapidocr is not installed — which is exactly the environment
# where a silent fallback to rapidocr's own defaults would go unnoticed.
#
# EVERY key here is load-bearing. rapidocr/config.yaml ships
# `lang_type: "ch"` and `ocr_version: "PP-OCRv6"`, so an omitted key does not
# mean "engine default", it means **Chinese PP-OCRv6** — the same trap
# `app/nrb/ocr.py` documents for the RAG converter ("would point a
# Chinese/English-dictionary recogniser at every uploaded PDF").
#
# Detection is held identical to the measured NRB configuration (`ch`, v5,
# mobile) so §16.6's evidence carries over unchanged: a detector finds text
# BOXES, not characters, so it is script-agnostic and there is nothing to gain
# by varying it. `lang` moves the recogniser and nothing else.
_DET_CONFIG = {
    "Det.engine_type": OCR_BACKEND,
    "Det.lang_type": "ch",
    "Det.model_type": "mobile",
    "Det.ocr_version": OCR_MODEL,
}
_REC_CONFIG = {
    "Rec.engine_type": OCR_BACKEND,
    "Rec.model_type": "mobile",
    "Rec.ocr_version": OCR_MODEL,
}

# key suffix -> the rapidocr enum that validates it. rapidocr REFUSES plain
# strings ("The value of Det.engine_type must be Enum Type"), so the table above
# is only honoured if every key converts.
_ENUM_FOR = {
    "engine_type": "EngineType",
    "model_type": "ModelType",
    "ocr_version": "OCRVersion",
    "Det.lang_type": "LangDet",
    "Rec.lang_type": "LangRec",
    "Cls.lang_type": "LangCls",
}


class OcrUnavailable(RuntimeError):
    """The OCR stack is not installed, or failed to load.

    Raised rather than returning "" — a stage that silently produced nothing
    looks exactly like an image that legitimately holds no text.
    """


@dataclass(frozen=True)
class OcrResult:
    lines: list[str]
    scores: list[float]          # one per LINE: the worst of its boxes
    engine: str = OCR_ENGINE
    model: str = OCR_MODEL
    backend: str = OCR_BACKEND
    lang: str = DEFAULT_LANG
    version: str = ""
    # Never True. See limit 1 in the module docstring.
    authoritative: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def mean_score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores) / len(self.scores)

    @property
    def min_score(self) -> float:
        if not self.scores:
            return 0.0
        return min(self.scores)


def ocr_config(lang: str) -> dict[str, str]:
    """The full intended rapidocr configuration for `lang`, as plain strings."""
    if lang not in SUPPORTED_LANGS:
        raise ValueError(
            f"unsupported OCR language '{lang}' (supported: "
            f"{', '.join(sorted(SUPPORTED_LANGS))})"
        )
    return {**_DET_CONFIG, **_REC_CONFIG, "Rec.lang_type": lang}


def ocr_params(lang: str) -> dict[str, Any]:
    """`ocr_config` converted to the enum values rapidocr insists on."""
    config = ocr_config(lang)
    module = _rapidocr()
    params: dict[str, Any] = {}
    for key, value in config.items():
        enum_name = _ENUM_FOR.get(key) or _ENUM_FOR[key.split(".", 1)[1]]
        params[key] = getattr(module, enum_name)(value)
    return params


def _rapidocr():
    """The lazy import boundary. ImportError -> OcrUnavailable."""
    try:
        import rapidocr
    except ImportError as exc:
        raise OcrUnavailable(
            "rapidocr is not installed. Image OCR is an optional component — "
            "see requirements-ocr.txt and the INSTALL_OCR build ARG."
        ) from exc
    if rapidocr is None:  # a test may stub the module out
        raise OcrUnavailable("rapidocr is not installed.")
    return rapidocr


def available() -> bool:
    """Whether the OCR stack can be loaded at all (never raises).

    This only checks that `import rapidocr` succeeds — it does NOT build the
    engine, so it says nothing about whether the three ONNX models can
    actually be loaded. Use `prewarm()` to pay that cost eagerly.
    """
    try:
        _rapidocr()
    except OcrUnavailable:
        return False
    return True


def prewarm(lang: str = DEFAULT_LANG) -> bool:
    """Actually build the engine for `lang`, paying the ~0.7s model-load cost
    now instead of charging it to the first caller.

    `available()` alone does NOT do this — it only imports the package, which
    measured 0.002s against the engine load's 0.683s, so calling `available()`
    from a "prewarm" setting moved 2ms to startup and left the real cost on
    the first request. This is the function a genuine prewarm must call.

    Never raises: returns False when the stack is absent or the engine fails
    to load, so a deployment without OCR still boots.
    """
    try:
        _engine(lang)
    except OcrUnavailable:
        return False
    return True


def engine_version() -> str:
    """The installed package versions, for provenance in the tool header."""
    from importlib.metadata import PackageNotFoundError, version

    parts = []
    for package in ("rapidocr", "onnxruntime"):
        try:
            parts.append(f"{package} {version(package)}")
        except PackageNotFoundError:
            parts.append(f"{package} unknown")
    return ", ".join(parts)


@functools.lru_cache(maxsize=len(SUPPORTED_LANGS))
def _engine(lang: str):
    """One loaded engine per language, for the life of the process.

    Loading reads three ONNX models; doing that per request would dominate a
    chat turn. Mirrors `nrb/rag.nrb_dependencies()`'s single-shot construction.
    """
    module = _rapidocr()
    params = dict(ocr_params(lang))
    params["Global.log_level"] = "error"
    try:
        engine = module.RapidOCR(params=params)
    except Exception as exc:
        raise OcrUnavailable(
            f"could not load the OCR engine ({type(exc).__name__})"
        ) from exc
    logger.info(
        "image OCR engine ready: %s/%s lang=%s via %s (%s)",
        OCR_ENGINE, OCR_MODEL, lang, OCR_BACKEND, engine_version(),
    )
    return engine


def reset_engines() -> None:
    """Drop the loaded engines (frees the ONNX models). Tests and shutdown."""
    _engine.cache_clear()


def engine_cache_info():
    return _engine.cache_info()


def group_lines(
    boxes: Any, txts: Optional[Sequence[str]], scores: Optional[Sequence[float]]
) -> tuple[list[str], list[float]]:
    """Turn per-BOX detections into reading-ordered LINES.

    rapidocr returns one box per word, not per line — measured, "Total Amount:
    45,320.75" comes back as three boxes sharing one row. Emitting one word per
    line would destroy every table and every figure that has a label.

    Order comes from the GEOMETRY, never from the order rapidocr emitted: boxes
    are grouped into rows by vertical position, rows run top to bottom and words
    within a row run left to right.

    A line's score is the MINIMUM of its boxes': a line is only as trustworthy
    as its worst word. Note there is no comparison against a threshold anywhere
    — see limit 2 in the module docstring.

    `boxes`/`txts` are None (not empty) when nothing was detected.
    """
    if boxes is None or txts is None:
        return [], []
    entries = []
    for index, text in enumerate(txts):
        box = boxes[index]
        ys = [float(point[1]) for point in box]
        xs = [float(point[0]) for point in box]
        score = 1.0
        if scores is not None and index < len(scores):
            score = float(scores[index])
        entries.append(
            {
                "text": str(text),
                "top": min(ys),
                "bottom": max(ys),
                "left": min(xs),
                "centre": (min(ys) + max(ys)) / 2.0,
                "score": score,
            }
        )
    if not entries:
        return [], []

    entries.sort(key=lambda e: (e["centre"], e["left"]))

    rows: list[list[dict]] = [[entries[0]]]
    row_bottom = entries[0]["bottom"]
    for entry in entries[1:]:
        # A box joins the current row while its VERTICAL CENTRE still falls
        # inside that row's extent. Comparing centres to the row's bottom (not
        # box tops to box tops) keeps a tall box and a short one on the same
        # line, which is the normal case for a heading beside a figure.
        if entry["centre"] <= row_bottom:
            rows[-1].append(entry)
            row_bottom = max(row_bottom, entry["bottom"])
        else:
            rows.append([entry])
            row_bottom = entry["bottom"]

    lines: list[str] = []
    line_scores: list[float] = []
    for row in rows:
        row.sort(key=lambda e: e["left"])
        text = " ".join(e["text"] for e in row).strip()
        if not text:
            continue
        lines.append(text)
        line_scores.append(min(e["score"] for e in row))
    return lines, line_scores


def ocr_image(path: Path, *, lang: Optional[str] = None) -> OcrResult:
    """Read the text of one image. Raises OcrUnavailable / ValueError only."""
    chosen = lang or DEFAULT_LANG
    if chosen not in SUPPORTED_LANGS:
        raise ValueError(
            f"unsupported OCR language '{chosen}' (supported: "
            f"{', '.join(sorted(SUPPORTED_LANGS))})"
        )
    # Engine first, so a missing stack is OcrUnavailable rather than a file read.
    engine = _engine(chosen)
    try:
        raw = engine(str(Path(path)))
    except Exception as exc:
        raise OcrUnavailable(f"OCR failed ({type(exc).__name__})") from exc

    lines, scores = group_lines(
        getattr(raw, "boxes", None), getattr(raw, "txts", None), getattr(raw, "scores", None)
    )
    return OcrResult(
        lines=lines,
        scores=scores,
        lang=chosen,
        version=engine_version(),
        detail={"lines": len(lines)},
    )
