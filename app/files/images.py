"""Normalize an uploaded IMAGE into facts about it (format + dimensions).

Pure module — no DB, no HTTP, no settings, and deliberately **no OCR**. The
upload route (validate + summary) and the chat attachment note both go through
`summarize_image`, and `history/service._resolve_attachments` re-runs it on
every attached file on EVERY turn. An OCR pass here would therefore be paid per
turn; text extraction lives in `image_ocr.py` and is reached only by the
`read_image` tool.

Same contract as `documents.py`: report FACTS, raise only `ReadError`, and never
put the on-disk path or the numeric user id in an exception message (see the
leak comment on `documents._decode` — a reader's message exposed
`/…/files/3/{uuid}.txt` into model context).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .readers import ReadError

logger = logging.getLogger("app.files")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}

# Decoded-pixel ceiling — the guard `router.py`'s zip-bomb check cannot provide.
# A ~200-byte PNG can declare 40000x40000, sailing past both the 10 MB wire cap
# and the OOXML zip check, and decoding it would allocate gigabytes. A module
# constant rather than a setting, exactly like `documents.MAX_PDF_PAGES`.
# 40 MP is past any phone camera; ~120 MB decoded as RGB.
MAX_IMAGE_PIXELS = 40_000_000

# Pillow's own format name -> how we say it to a human and to the model. This
# doubles as the DECODER ALLOWLIST: a format absent from here is refused even
# when Pillow reads it happily. Pillow reaches dozens of decoders (EPS, FLI,
# ICNS, …) and has a history of CVEs in the obscure ones; an upload route should
# only be able to reach the handful of formats it actually accepts. The check is
# on the SNIFFED format, not the extension, so a GIF renamed .png is refused.
_KINDS = {
    "PNG": "PNG image",
    "JPEG": "JPEG image",
    "MPO": "JPEG image",  # multi-picture JPEG, what many phones actually emit
    "WEBP": "WebP image",
    "TIFF": "TIFF image",
    "BMP": "BMP image",
}


@dataclass
class ImageSummary:
    kind: str
    width: int
    height: int
    # TIFF/WebP/GIF can hold several frames, and a scanned .tif routinely does —
    # a document scanner's normal output. Measured: the OCR engine reads only the
    # FIRST frame, and page 2's text vanished with no warning. So the count is a
    # reported fact, exactly as documents.py reports pages_skipped rather than
    # quietly stopping at MAX_PDF_PAGES.
    frames: int = 1

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "frames": self.frames,
        }

    def text(self) -> str:
        """One-line human/model summary, e.g. 'PNG image, 1240×800'."""
        base = f"{self.kind}, {self.width}×{self.height}"
        if self.frames > 1:
            return f"{base}, {self.frames} frames (only the first is read)"
        return base


def summarize_image(path: Path) -> ImageSummary:
    """Format + dimensions of an image (raises ReadError on anything else).

    Never decodes the full bitmap before the pixel cap is checked, and never
    OCRs. `verify()` runs afterwards so a truncated/corrupt image is a 400 at
    upload time rather than a surprise inside the read tool — the same choice
    `documents.py` makes by actually parsing a PDF in its summary.
    """
    path = Path(path)
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - Pillow is in requirements.txt
        raise ReadError("image support is not installed") from exc

    try:
        with Image.open(path) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            # Before verify(), which invalidates the image object.
            frames = int(getattr(img, "n_frames", 1) or 1)

            # BEFORE any decode. Pillow only *raises* above 2x its own limit and
            # merely warns between 1x and 2x, so relying on its exception alone
            # would let a 1.5x bomb through.
            if width * height > MAX_IMAGE_PIXELS:
                raise ReadError(
                    f"image is too large to process safely "
                    f"({width}×{height} exceeds {MAX_IMAGE_PIXELS} pixels)"
                )
            img.verify()
    except ReadError:
        raise
    except UnidentifiedImageError as exc:
        raise ReadError("file is not a readable image") from exc
    except OSError as exc:
        # Never str(exc)/exc.filename here — both carry the absolute path.
        raise ReadError(
            f"could not read the image ({exc.strerror or type(exc).__name__})"
        ) from exc
    except Exception as exc:  # Pillow's DecompressionBombError, plugin errors
        if type(exc).__name__ == "DecompressionBombError":
            raise ReadError("image is too large to process safely") from exc
        raise ReadError(f"could not read the image ({type(exc).__name__})") from exc

    if fmt not in _KINDS:
        raise ReadError(f"unsupported image format ({fmt or 'unrecognised'})")
    return ImageSummary(
        kind=_KINDS[fmt], width=width, height=height, frames=frames
    )
