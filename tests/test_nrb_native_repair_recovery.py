"""native-3 inside recovery: routing untouched, repair on native pages only (spec §2, §3, §5)."""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap

import pytest

from app.nrb import extraction, recovery

# Recorded by Task 5 Step 1, BEFORE recovery.py was edited. If either changes,
# the routing changed: bump RECOVERY_ROUTING_VERSION (the base version) and
# re-record — the engine-version design (D1) is only honest while these hold.
PINNED = {
    "plan_document": "f57cd5f420dccd5c4ccef8e63c8a079e4edc82249b276e6262d8722250870101",
    "route_page": "5cb6c6391b5af28f8221f237004e7ff2d9952a4fb39b880a5429c2425ac41e3a",
}


def _ast_sha(fn) -> str:
    return hashlib.sha256(ast.dump(ast.parse(textwrap.dedent(inspect.getsource(fn)))).encode()).hexdigest()


def test_the_routing_functions_are_unchanged():
    assert _ast_sha(recovery.plan_document) == PINNED["plan_document"]
    assert _ast_sha(recovery.route_page) == PINNED["route_page"]
    assert recovery.RECOVERY_ROUTING_VERSION == "recovery-1"


def test_page_routes_for_a_native_plan_is_native_everywhere(tmp_path):
    plan = recovery.DocumentPlan(recovery.PLAN_NATIVE, "clean", None)
    assert recovery.page_routes(tmp_path / "x.pdf", plan, ["a", "b"]) == (
        (recovery.ROUTE_NATIVE, "clean"), (recovery.ROUTE_NATIVE, "clean"))


def test_page_routes_for_no_recovery_is_empty(tmp_path):
    plan = recovery.DocumentPlan(recovery.PLAN_NONE, "parser_error", None)
    assert recovery.page_routes(tmp_path / "x.pdf", plan, ["a"]) == ()


def test_page_routes_agree_with_what_recover_routes(tmp_path):
    """One routing answer, two callers: recover() and the evidence scripts."""
    from tests.test_nrb_recovery import _write_pdf

    path = _write_pdf(tmp_path, "mixed.pdf", [
        {"font": "Preeti", "embedded": True}, {"image": True}, {"font": "Preeti", "embedded": True},
    ])
    plan = recovery.DocumentPlan(recovery.PLAN_PAGES, "legacy_font_suspected", 1.0)
    pages = ["g]kfn /fi6« a}+s " * 5, "", "g]kfn /fi6« a}+s " * 5]
    routes = recovery.page_routes(path, plan, pages)
    assert [r for r, _ in routes] == [recovery.ROUTE_LEGACY, recovery.ROUTE_OCR, recovery.ROUTE_LEGACY]
