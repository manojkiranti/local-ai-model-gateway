"""Pure tests for API key minting and verification. No DB, no app import.

These prove the credential mechanics, so they are exhaustive on the failure
side: a truncated token, a prefix-only token and a right-prefix/wrong-secret
token must all fail to verify.
"""

import ast
from pathlib import Path

import pytest

from app.apikeys import keygen


def test_mint_produces_a_parseable_token():
    minted = keygen.mint()
    parsed = keygen.parse(minted.token)
    assert parsed is not None
    prefix, secret = parsed
    assert prefix == minted.prefix
    assert len(prefix) == keygen.PREFIX_LEN
    assert keygen.hash_secret(secret) == minted.key_hash


def test_the_token_carries_the_label_so_a_dev_key_is_visibly_not_prod():
    assert keygen.mint("lgw_test").token.startswith("lgw_test_")


def test_the_plaintext_secret_is_not_recoverable_from_the_hash():
    minted = keygen.mint()
    _, secret = keygen.parse(minted.token)
    assert secret not in minted.key_hash
    assert len(minted.key_hash) == 64  # sha256 hex


def test_mints_never_repeat():
    tokens = {keygen.mint().token for _ in range(200)}
    prefixes = {keygen.mint().prefix for _ in range(200)}
    assert len(tokens) == 200
    assert len(prefixes) == 200


def test_verify_accepts_the_real_token():
    minted = keygen.mint()
    assert keygen.verify(minted.token, minted.key_hash) is True


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t[:-1],                      # truncated
        lambda t: t + "x",                     # extended
        lambda t: t.rsplit("_", 1)[0],         # prefix only, secret removed
        lambda t: t.rsplit("_", 1)[0] + "_" + "z" * 43,  # right prefix, wrong secret
        lambda t: "",
        lambda t: "   ",
        lambda t: "lgw_live",
        lambda t: t.upper(),
    ],
)
def test_verify_rejects_every_mangled_token(mangle):
    minted = keygen.mint()
    assert keygen.verify(mangle(minted.token), minted.key_hash) is False


def test_parse_returns_none_rather_than_raising_on_junk():
    for junk in ["", "no-underscores", "lgw_live", "\x00\x01", "a_b"]:
        assert keygen.parse(junk) is None


def test_verify_uses_a_constant_time_comparison():
    """`==` on a hash is a timing oracle that reads as perfectly correct code.

    Asserted on the AST, not by timing (a timing test is flaky), and not by
    reading the text (a comment mentioning compare_digest would satisfy grep).
    """
    tree = ast.parse(Path(keygen.__file__).read_text())
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verify"
    )
    calls = {
        getattr(node.func, "attr", getattr(node.func, "id", None))
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }
    assert "compare_digest" in calls
    comparisons = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    assert not any(
        isinstance(op, (ast.Eq, ast.NotEq)) for c in comparisons for op in c.ops
    ), "verify() must not use == / != on secret material"
