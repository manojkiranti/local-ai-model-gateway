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
