"""Offline tests for the calculator local tool (safe AST expression evaluator).

Covers correct arithmetic/precedence, the allowed math functions/constants, and
that every unsafe or malformed input returns a friendly ERROR string and NEVER
executes anything.
"""

import asyncio
import math

import pytest

from app.tools.local import calculator


def _run(expression):
    return asyncio.run(calculator.SPEC.func({"expression": expression}))


# ---- correct results (returned as "<expr> = <result>") ----

@pytest.mark.parametrize(
    "expr,expected",
    [
        ("2 + 2 * 10", "22"),          # precedence
        ("(2 + 2) * 10", "40"),        # parentheses
        ("7 // 2", "3"),               # floor div
        ("7 % 3", "1"),                # modulo
        ("2 ** 10", "1024"),           # power
        ("-5 + 3", "-2"),              # unary minus
        ("10 / 4", "2.5"),             # true div -> float
    ],
)
def test_arithmetic(expr, expected):
    assert _run(expr) == f"{expr} = {expected}"


def test_functions_and_constants():
    assert _run("sqrt(16)").endswith("= 4")
    assert _run("abs(-3)").endswith("= 3")
    assert _run("round(3.14159, 2)").endswith("= 3.14")
    assert _run("max(1, 5, 3)").endswith("= 5")
    assert _run("min(1, 5, 3)").endswith("= 1")
    assert _run("floor(2.9)").endswith("= 2")
    assert _run("ceil(2.1)").endswith("= 3")
    # pi constant present and correct to a few places
    result = _run("pi")
    assert result.startswith("pi = 3.14159")
    assert abs(float(_run("log(e)").split("= ")[1]) - 1.0) < 1e-9
    assert abs(float(_run("sin(0)").split("= ")[1])) < 1e-9


# ---- safety: unsafe / malformed input must ERROR, never execute ----

@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo hi')",  # no calls to arbitrary names
        "os.system('ls')",                      # no attribute access
        "open('/etc/passwd').read()",           # no arbitrary builtins
        "unknown_func(2)",                       # function not on the allowlist
        "x + 1",                                # bare names not allowed
        "1; import os",                          # not a single expression
        "[i for i in range(3)]",                # no comprehensions
        "lambda: 1",                            # no lambdas
        "",                                      # empty
        "2 +",                                   # syntax error
        "2 @ 3",                                 # unsupported operator
    ],
)
def test_unsafe_or_malformed_returns_error(expr):
    result = _run(expr)
    assert result.startswith("ERROR"), f"expected ERROR for {expr!r}, got {result!r}"


def test_missing_expression():
    assert asyncio.run(calculator.SPEC.func({})).startswith("ERROR")


def test_division_by_zero_is_friendly_error():
    assert _run("1 / 0").startswith("ERROR")


def test_huge_exponent_rejected_not_hang():
    # A DoS guard: gigantic powers are refused rather than computed.
    assert _run("9 ** 9 ** 9").startswith("ERROR")


def test_overlong_expression_rejected():
    assert _run("1+" * 5000 + "1").startswith("ERROR")


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "calculator" for spec in LOCAL_TOOLS)
