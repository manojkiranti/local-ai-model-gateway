"""Boundary tests: the shared caveat, no threshold, no OCR import at import.

These are the tests that stop a rewrite quietly losing a property. They are
deliberately structural (AST, subprocess) rather than behavioural, because each
property is invisible in ordinary output right up to the moment it matters.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_the_caveat_is_one_constant_with_three_readers():
    """`read_image` (chat), `/v1/ocr` and `/v1/extract` all render the SAME
    sentence. A second copy drifts, and then two surfaces disagree about the
    wording and a reader cannot tell which to believe. Same rule as
    sources.VERIFY_NOTE.
    """
    from app.files import image_ocr
    from app.publicapi import extract_schemas, schemas
    from app.tools.local import read_image

    assert image_ocr.OCR_CAVEAT
    assert read_image.CAVEAT is image_ocr.OCR_CAVEAT
    assert schemas.CAVEAT is image_ocr.OCR_CAVEAT
    assert extract_schemas.OCR_CAVEAT is image_ocr.OCR_CAVEAT


def test_neither_the_router_nor_the_schemas_compare_a_confidence_to_a_literal():
    """No threshold. docs/nrb-integration.md §16.6 measured orthographic
    well-formedness, which is not a per-field correctness estimate; a constant
    derived from it would dress a guess as a measurement.
    """
    from app.publicapi import extract_schemas, ocr_router, schemas

    for module in (ocr_router, schemas, extract_schemas):
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


def test_with_the_switch_unset_the_ocr_routes_are_absent_from_openapi():
    """The property that makes this whole branch safe to merge — the master
    switch defaults false, so merging changes nothing about any existing
    deployment. Nothing else asserts this: a future edit moving
    `include_router` outside the `if external_api_enabled:` block in
    `app/main.py` would keep the rest of the suite green.

    SUBPROCESS, and with an explicit `env=` (not just deleting the var in THIS
    process): `EXTERNAL_API_ENABLED` and the imported `app.main` module are
    both process-global, and other test files in this suite deliberately flip
    the switch on and reload `app.main` (see `test_apikey_admin_integration.py`
    and `test_ocr_api_integration.py`'s `_client()` helpers) — inheriting
    `os.environ` unchanged into a subprocess would make this pass or fail by
    accident of test ORDER within the same pytest run.
    """
    env = dict(os.environ)
    env.pop("EXTERNAL_API_ENABLED", None)
    script = (
        "import app.main;"
        "paths = app.main.app.openapi()['paths'];"
        "assert '/v1/ocr' not in paths, 'route present with the switch unset';"
        "assert '/v1/api-keys' not in paths, 'route present with the switch unset';"
        "print('OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"


def test_with_the_switch_unset_the_guard_middleware_is_absent():
    """M-d: the merge-safety test above only asserted on the ROUTERS. If
    `app.add_middleware(UploadContentLengthGuard)` were dedented out of the
    `external_api_enabled` block in `app/main.py`, nothing would fail — and a
    feature-disabled deployment would answer 413 instead of 404 to an
    oversized `POST /v1/ocr`, revealing the route exists on a deployment that
    never enabled it. Same gap shape as the routers, for the middleware the
    residual-fix wave added.
    """
    env = dict(os.environ)
    env.pop("EXTERNAL_API_ENABLED", None)
    script = (
        "import app.main;"
        "names = [m.cls.__name__ for m in app.main.app.user_middleware];"
        "assert 'UploadContentLengthGuard' not in names, "
        "f'guard present with the switch unset: {names}';"
        "print('OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"


def test_with_the_switch_enabled_the_guard_middleware_is_present():
    """The other direction: turning the switch on must actually register the
    guard, not just avoid crashing."""
    env = dict(os.environ)
    env["EXTERNAL_API_ENABLED"] = "true"
    script = (
        "import app.main;"
        "names = [m.cls.__name__ for m in app.main.app.user_middleware];"
        "assert 'UploadContentLengthGuard' in names, "
        "f'guard missing with the switch enabled: {names}';"
        "print('OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"


def test_the_guard_is_registered_outside_cors_so_its_413_carries_cors_headers():
    """M-a, proved by construction rather than by an HTTP round trip: since
    `Starlette.add_middleware` inserts at position 0 and the stack is built by
    wrapping in REVERSE of that list, the middleware whose position in
    `user_middleware` comes AFTER CORSMiddleware is the one added FIRST, and
    ends up wrapped INSIDE it (closer to the app) — meaning CORS is outermost
    and processes the guard's response on the way back out. The end-to-end
    behavioural check (an actual 413 carrying `access-control-allow-origin`)
    is `test_the_guards_413_carries_cors_headers_for_an_allowed_origin` in
    tests/test_ocr_api_integration.py.
    """
    env = dict(os.environ)
    env["EXTERNAL_API_ENABLED"] = "true"
    script = (
        "import app.main;"
        "names = [m.cls.__name__ for m in app.main.app.user_middleware];"
        "guard_ix = names.index('UploadContentLengthGuard');"
        "cors_ix = names.index('CORSMiddleware');"
        "assert guard_ix > cors_ix, "
        "f'guard must come AFTER cors in user_middleware (outer wraps inner): {names}';"
        "print('OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"


def test_with_the_switch_enabled_the_ocr_routes_are_present_in_openapi():
    """The other direction of the same property: turning it on must actually
    register both routers, not just avoid crashing."""
    env = dict(os.environ)
    env["EXTERNAL_API_ENABLED"] = "true"
    script = (
        "import app.main;"
        "paths = app.main.app.openapi()['paths'];"
        "assert '/v1/ocr' in paths, 'route missing with the switch enabled';"
        "assert '/v1/api-keys' in paths, 'route missing with the switch enabled';"
        "print('OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"


def test_the_ocr_route_declares_a_real_api_key_security_scheme():
    """Before this, `x_api_key` was a bare `Header(default=None, ...)`, which
    OpenAPI renders as an ordinary OPTIONAL parameter with no security scheme
    at all — Swagger shows no lock, and a generated client marks the header
    optional. Using `fastapi.security.APIKeyHeader` as the dependency's
    source makes the schema honest. SUBPROCESS + explicit env for the same
    reason as the sibling tests above: the switch and `app.main` are both
    process-global.
    """
    env = dict(os.environ)
    env["EXTERNAL_API_ENABLED"] = "true"
    script = (
        "import app.main;"
        "spec = app.main.app.openapi();"
        "schemes = spec['components']['securitySchemes'];"
        "api_key_schemes = [s for s in schemes.values() "
        "if s.get('type') == 'apiKey' and s.get('name') == 'X-API-Key'];"
        "assert api_key_schemes, f'no X-API-Key apiKey scheme found: {schemes}';"
        "ocr_op = spec['paths']['/v1/ocr']['post'];"
        "assert ocr_op.get('security'), 'POST /v1/ocr has no security requirement';"
        "print('OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"
