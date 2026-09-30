"""Office file -> PDF, for the side-panel preview (GET /v1/files/{id}/pdf).

A browser renders a PDF natively but not a .docx/.pptx/.xlsx, so the preview
panel shows a generated Word memo (or any Office file) as the PDF LibreOffice
makes of it — the real layout, logo and tables included, not an approximation.

LibreOffice is OPTIONAL, like the OCR stack: the `INSTALL_DOC_PDF` build ARG
adds it to the gateway image (compose turns it on). Absent, `available()` is
False and the route answers 503, and the frontend falls back to its structured
preview.

Each conversion runs `soffice` headless with its OWN throwaway profile
directory — two concurrent `soffice` processes sharing the default profile
lock each other out and one silently produces nothing. The PDF is cached next
to the source as `<name>.preview.pdf` and rebuilt only when the source is
newer, so reopening a preview is instant.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

CONVERTIBLE_EXTS = {".docx", ".pptx", ".xlsx"}
CONVERT_TIMEOUT_SECONDS = 90


class ConvertError(Exception):
    """LibreOffice ran but produced no PDF (or timed out)."""


def available() -> bool:
    return shutil.which("soffice") is not None


def cached_pdf_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}.preview.pdf")


def convert_to_pdf(source: Path) -> Path:
    """Return a PDF rendering of `source`, converting only if the cached copy
    is missing or older than the source. Sync and slow (seconds) — call it in
    a thread."""
    target = cached_pdf_path(source)
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    with tempfile.TemporaryDirectory(prefix="lo-") as work:
        work_dir = Path(work)
        cmd = [
            "soffice",
            f"-env:UserInstallation=file://{work_dir / 'profile'}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--convert-to",
            "pdf",
            "--outdir",
            str(work_dir),
            str(source),
        ]
        try:
            subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CONVERT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConvertError("conversion timed out") from exc
        produced = work_dir / f"{source.stem}.pdf"
        if not produced.exists() or produced.stat().st_size == 0:
            raise ConvertError("LibreOffice produced no PDF")
        shutil.move(str(produced), target)
    return target
