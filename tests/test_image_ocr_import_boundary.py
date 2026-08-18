"""The OCR stack must never load just because the API did.

`Dockerfile` used to give this for free by installing `requirements.txt` alone,
so no OCR package existed in the API image at all. Image OCR makes the stack
INSTALLABLE there (`--build-arg INSTALL_OCR=true`), which retires that
guarantee — this test replaces it.

Why it matters beyond image size: rapidocr pulls opencv, and loading three ONNX
models costs real memory in every uvicorn worker. A module-scope import would
make an API process that has never been asked to read an image pay for the
ability to.

Runs in a SUBPROCESS deliberately: `sys.modules` is process-global, and
tests/test_image_ocr.py imports rapidocr in the same pytest run — an in-process
check would pass or fail depending on test order. Same reasoning as
tests/test_rag_parsing.py:136.
"""

from __future__ import annotations

import subprocess
import sys

FORBIDDEN = ("rapidocr", "onnxruntime", "cv2", "docling", "torch")


def _probe(import_line: str) -> str:
    code = (
        f"import sys; {import_line};"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_importing_the_tool_registry_does_not_load_the_ocr_stack():
    assert _probe("import app.tools.local") == ""


def test_importing_the_read_image_tool_does_not_load_the_ocr_stack():
    """The tool imports app.files.image_ocr at module scope — which is only safe
    because every rapidocr import in that module is inside a function."""
    assert _probe("import app.tools.local.read_image") == ""


def test_importing_the_files_router_does_not_load_the_ocr_stack():
    assert _probe("import app.files.router") == ""


def test_importing_the_app_does_not_load_the_ocr_stack():
    assert _probe("import app.main") == ""


def test_the_ocr_module_itself_is_importable_without_rapidocr_installed():
    """`available()` must answer False rather than raise, so the tool can report
    'not enabled on this deployment' instead of 500ing."""
    code = (
        "import sys; sys.modules['rapidocr'] = None;"
        "from app.files import image_ocr;"
        "print(image_ocr.available())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"
