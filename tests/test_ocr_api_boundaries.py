"""Boundary tests: the shared caveat, no threshold, no OCR import at import.

These are the tests that stop a rewrite quietly losing a property. They are
deliberately structural (AST, subprocess) rather than behavioural, because each
property is invisible in ordinary output right up to the moment it matters.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest


def test_the_caveat_is_one_constant_with_two_readers():
    """A second copy drifts, and then the API and the chat answer caveat
    differently — leaving the reader unable to tell which to believe. Same rule
    as sources.VERIFY_NOTE.
    """
    from app.files import image_ocr
    from app.publicapi import schemas
    from app.tools.local import read_image

    assert image_ocr.OCR_CAVEAT
    assert read_image.CAVEAT is image_ocr.OCR_CAVEAT
    assert schemas.CAVEAT is image_ocr.OCR_CAVEAT


def test_neither_the_router_nor_the_schemas_compare_a_confidence_to_a_literal():
    """No threshold. docs/nrb-integration.md §16.6 measured orthographic
    well-formedness, which is not a per-field correctness estimate; a constant
    derived from it would dress a guess as a measurement.
    """
    from app.publicapi import ocr_router, schemas

    for module in (ocr_router, schemas):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            } | {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            }
            if names & {"confidence", "score", "scores", "mean_score", "min_score"}:
                pytest.fail(
                    f"{module.__name__} compares a confidence value at line "
                    f"{node.lineno}; scores are reported, never enforced"
                )


# --- import boundary ------------------------------------------------------


def test_importing_the_public_api_loads_no_ocr_stack():
    """A SUBPROCESS check, because sys.modules is process-global: any earlier
    test that used OCR would make an in-process assertion pass vacuously.

    The API image must be able to run with rapidocr/onnxruntime absent, and it
    must not pay their import cost when they happen to be present.
    """
    code = (
        "import app.publicapi.ocr_router, app.publicapi.schemas, sys;"
        "bad=[m for m in ('rapidocr','onnxruntime','cv2','docling') "
        "if any(k==m or k.startswith(m+'.') for k in sys.modules)];"
        "print('LOADED:'+','.join(bad) if bad else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.stdout.strip() == "CLEAN", out.stdout


def test_importing_the_whole_app_loads_no_ocr_stack():
    code = (
        "import app.main, sys;"
        "bad=[m for m in ('rapidocr','onnxruntime','cv2','docling') "
        "if any(k==m or k.startswith(m+'.') for k in sys.modules)];"
        "print('LOADED:'+','.join(bad) if bad else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.stdout.strip() == "CLEAN", out.stdout


def test_with_the_ocr_stack_unimportable_the_route_answers_503():
    """Simulates the deployment where INSTALL_OCR was false — the §18 case.

    Run in a subprocess with a meta_path finder that makes rapidocr/onnxruntime
    unimportable, so this holds even on a machine where the stack IS installed.

    Uses the modern `find_spec` finder API, not the legacy `find_module`/
    `load_module` pair (deprecated since 3.4, removed in 3.12) — the legacy
    fallback still works on this interpreter (3.10.14) but would silently stop
    blocking anything once the project moves past 3.11, at which point this
    test would pass while proving nothing.
    """
    script = r'''
import sys, os
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("rapidocr", "onnxruntime"):
            raise ImportError("blocked for the test")
        return None  # not ours: let the normal finders handle it
sys.meta_path.insert(0, _Block())

os.environ["EXTERNAL_API_ENABLED"] = "true"
from app.files import image_ocr
assert image_ocr.available() is False, "the import block did not take effect"

from app.publicapi.ocr_router import STACK_MISSING
print("DETAIL:" + STACK_MISSING)
'''
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert "DETAIL:image OCR is not enabled on this deployment" in out.stdout


def test_the_public_api_never_returns_a_user():
    """An ApiClient must not be convertible into a User: that separation is the
    entire reason app/apikeys/ exists rather than a branch in auth/."""
    from app.apikeys.dependencies import ApiClient

    assert not hasattr(ApiClient, "role")
    assert not hasattr(ApiClient, "email")
    assert not hasattr(ApiClient, "is_active")

    repo_root = Path(__file__).resolve().parent.parent
    for module_path in ("app/apikeys/dependencies.py", "app/publicapi/ocr_router.py"):
        source = (repo_root / module_path).read_text()
        assert "users.models import User" not in source, (
            f"{module_path} imports User; an API key must never resolve to one"
        )
