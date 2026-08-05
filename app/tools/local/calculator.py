"""Local tool: calculator (safe math-expression evaluator).

LLMs are unreliable at arithmetic, so this gives the model an exact evaluator.
Security is the whole point: we NEVER use eval(). The expression is parsed with
`ast.parse(..., mode="eval")` and walked against a strict allowlist of node
types, operators, functions and constants — anything else (attribute access,
arbitrary names/calls, comprehensions, lambdas, statements) is rejected with a
friendly ERROR string. Two DoS guards cap the input length and the exponent
magnitude so a pathological power (e.g. 9**9**9) can't hang the process.

No dependency beyond the stdlib (`ast` + `math`).
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from .base import LocalToolSpec

MAX_EXPR_LEN = 500  # reject absurdly long expressions outright
MAX_EXPONENT = 1000  # reject huge powers so evaluation can't blow up / hang

# Allowed binary and unary operators -> their implementations.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Allowed named functions and constants. Deliberately a fixed, small surface:
# arithmetic + the common math the model actually reaches for.
_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,      # natural log (or log(x, base))
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "pow": math.pow,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
_CONSTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


class _UnsafeExpression(Exception):
    """Raised when the AST contains something outside the allowlist."""


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    # Numeric literals only (reject strings, bytes, etc.).
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _UnsafeExpression("only numeric literals are allowed")
        return node.value

    # Named constants (pi, e, tau) — nothing else may resolve a name.
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise _UnsafeExpression(f"unknown name '{node.id}'")

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise _UnsafeExpression("unsupported operator")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if type(node.op) is ast.Pow and abs(right) > MAX_EXPONENT:
            raise _UnsafeExpression("exponent too large")
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise _UnsafeExpression("unsupported unary operator")
        return op(_eval_node(node.operand))

    # Calls only to allowlisted functions, with plain positional args.
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise _UnsafeExpression("unknown function")
        if node.keywords:
            raise _UnsafeExpression("keyword arguments are not allowed")
        args = [_eval_node(a) for a in node.args]
        return _FUNCS[node.func.id](*args)

    # Anything else (Attribute, Subscript, comprehensions, lambdas, ...) is out.
    raise _UnsafeExpression(f"disallowed expression element: {type(node).__name__}")


def _evaluate(expression: str) -> float | int:
    tree = ast.parse(expression, mode="eval")  # mode="eval" => single expression only
    return _eval_node(tree)


def _format(value: Any) -> str:
    # Render whole-valued floats without a trailing .0 (4.0 -> "4"); keep real floats.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


async def _calculator(args: dict[str, Any]) -> str:
    expression = args.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        return "ERROR: 'expression' is required and must be a non-empty string."
    if len(expression) > MAX_EXPR_LEN:
        return f"ERROR: expression too long (max {MAX_EXPR_LEN} characters)."

    try:
        value = _evaluate(expression)
    except _UnsafeExpression as exc:
        return f"ERROR: unsafe or unsupported expression ({exc})."
    except SyntaxError:
        return "ERROR: could not parse the expression (syntax error)."
    except ZeroDivisionError:
        return "ERROR: division by zero."
    except (ValueError, OverflowError) as exc:
        return f"ERROR: math error: {exc}."
    except Exception as exc:  # noqa: BLE001 - report back, never raise into the loop
        return f"ERROR: {exc}"

    return f"{expression.strip()} = {_format(value)}"


SPEC = LocalToolSpec(
    name="calculator",
    description=(
        "Evaluate a mathematical expression and return the exact result. Use "
        "this for any arithmetic instead of computing it yourself. Supports "
        "+ - * / // % ** and parentheses, and the functions sqrt, abs, round, "
        "min, max, floor, ceil, log, log10, log2, exp, pow, sin, cos, tan, plus "
        "the constants pi, e, tau. Example: 'sqrt(16) + 2 * pi'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The math expression to evaluate, e.g. '(3 + 4) * 2'.",
            },
        },
        "required": ["expression"],
    },
    func=_calculator,
)
