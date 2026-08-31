"""Local tool: date_math (calendar arithmetic on supplied dates).

Complements get_current_time (which returns "now"): this does arithmetic on
dates the caller provides. Stdlib `datetime` only — no dependency. Supports:

  - add / subtract: a date (or datetime) +/- a duration in years, months, weeks,
    days, hours, minutes, seconds. Year/month steps use naive calendar math with
    END-OF-MONTH CLAMPING (2026-01-31 + 1 month = 2026-02-28).
  - diff: the gap between two dates/datetimes, as whole days (+ H:MM:SS when a
    time component is involved).

Returns a text result (not a file). Bad input -> friendly ERROR strings.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from numbers import Real
from typing import Any

from .base import LocalToolSpec

_TIMEDELTA_UNITS = ("weeks", "days", "hours", "minutes", "seconds")
_TIME_UNITS = ("hours", "minutes", "seconds")


def _parse(value: Any) -> tuple[date | datetime, bool]:
    """Parse an ISO date/datetime string. Returns (value, is_datetime).

    Raises ValueError on anything unparseable (surfaced as a friendly ERROR)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be an ISO date/datetime string")
    s = value.strip()
    if s.endswith("Z"):  # 3.10's fromisoformat doesn't accept the 'Z' suffix
        s = s[:-1] + "+00:00"
    try:
        return date.fromisoformat(s), False  # pure date (no time part)
    except ValueError:
        return datetime.fromisoformat(s), True  # may raise ValueError -> caller handles


def _as_int(value: Any, unit: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Real) or int(value) != value:
        raise ValueError(f"'{unit}' must be a whole number")
    return int(value)


def _as_number(value: Any, unit: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"'{unit}' must be a number")
    return float(value)


def _add_calendar(base: date | datetime, months: int, years: int) -> date | datetime:
    """Add whole years/months, clamping the day to the target month's length."""
    total = base.year * 12 + (base.month - 1) + years * 12 + months
    y, m0 = divmod(total, 12)
    m = m0 + 1
    day = min(base.day, calendar.monthrange(y, m)[1])
    return base.replace(year=y, month=m, day=day)


def _format_dt(value: date | datetime, want_dt: bool) -> str:
    return value.isoformat()


def _human_delta(delta: timedelta) -> str:
    """A readable magnitude: 'N days' plus 'H:MM:SS' when there's a sub-day part."""
    delta = abs(delta)
    days = delta.days
    rem = delta.seconds
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    day_word = "day" if days == 1 else "days"
    out = f"{days} {day_word}"
    if delta.seconds:
        out += f", {h}:{m:02d}:{s:02d}"
    return out


def _do_add(args: dict[str, Any], subtract: bool) -> str:
    if "date" not in args:
        return "ERROR: 'date' is required for add/subtract."
    try:
        base, is_dt = _parse(args.get("date"))
    except ValueError as exc:
        return f"ERROR: could not parse 'date' ({exc})."

    try:
        months = _as_int(args["months"], "months") if "months" in args else 0
        years = _as_int(args["years"], "years") if "years" in args else 0
        tunits: dict[str, float] = {}
        for u in _TIMEDELTA_UNITS:
            if u in args:
                tunits[u] = _as_number(args[u], u)
    except ValueError as exc:
        return f"ERROR: {exc}."

    if not months and not years and not any(tunits.values()) and not tunits:
        return "ERROR: provide at least one duration (years/months/weeks/days/hours/minutes/seconds)."

    sign = -1 if subtract else 1
    want_dt = is_dt or any(u in args for u in _TIME_UNITS)
    if want_dt and not is_dt:  # promote a bare date to midnight
        base = datetime(base.year, base.month, base.day)

    result = _add_calendar(base, sign * months, sign * years)
    if tunits:
        result = result + sign * timedelta(**tunits)

    op = "-" if subtract else "+"
    parts = []
    for u in ("years", "months", *_TIMEDELTA_UNITS):
        if u in args:
            parts.append(f"{args[u]} {u}")
    return f"{_format_dt(base, want_dt)} {op} {', '.join(parts)} = {_format_dt(result, want_dt)}"


def _do_diff(args: dict[str, Any]) -> str:
    if "from" not in args or "to" not in args:
        return "ERROR: 'diff' requires both 'from' and 'to'."
    try:
        a, a_dt = _parse(args.get("from"))
        b, b_dt = _parse(args.get("to"))
    except ValueError as exc:
        return f"ERROR: could not parse a date ({exc})."

    # If either side has a time, compare as datetimes (promote the other to midnight).
    if a_dt or b_dt:
        if not a_dt:
            a = datetime(a.year, a.month, a.day)
        if not b_dt:
            b = datetime(b.year, b.month, b.day)
    try:
        delta = b - a
    except TypeError:
        return "ERROR: cannot compare an offset-aware and an offset-naive datetime."
    return f"{args['from']} to {args['to']} = {_human_delta(delta)}"


async def _date_math(args: dict[str, Any]) -> str:
    operation = args.get("operation")
    if operation == "add":
        return _do_add(args, subtract=False)
    if operation == "subtract":
        return _do_add(args, subtract=True)
    if operation == "diff":
        return _do_diff(args)
    return "ERROR: 'operation' is required and must be one of: 'add', 'subtract', 'diff'."


SPEC = LocalToolSpec(
    name="date_math",
    description=(
        "Do calendar arithmetic on dates you provide (use get_current_time for "
        "'now'). Three operations:\n"
        "- 'add' / 'subtract': give a 'date' (ISO date or datetime) and a duration "
        "in any of 'years', 'months', 'weeks', 'days', 'hours', 'minutes', "
        "'seconds'. Year/month steps clamp to the end of the month "
        "(2026-01-31 + 1 month = 2026-02-28).\n"
        "- 'diff': give 'from' and 'to' (ISO); returns the gap in whole days "
        "(plus H:MM:SS when times are involved).\n"
        "Times promote the result to a datetime; date-only inputs stay dates.\n"
        "GREGORIAN ONLY — for a Nepali (Bikram Sambat) date such as 2082-01-31, "
        "or a Nepali fiscal year, use nepali_date instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["add", "subtract", "diff"],
                "description": "'add', 'subtract' (need 'date' + a duration), or 'diff' (need 'from' + 'to').",
            },
            "date": {"type": "string", "description": "ISO date/datetime for add/subtract, e.g. '2026-08-05'."},
            "from": {"type": "string", "description": "diff: start ISO date/datetime."},
            "to": {"type": "string", "description": "diff: end ISO date/datetime."},
            "years": {"type": "integer", "description": "Whole years to add/subtract."},
            "months": {"type": "integer", "description": "Whole months to add/subtract (end-of-month clamped)."},
            "weeks": {"type": "number", "description": "Weeks to add/subtract."},
            "days": {"type": "number", "description": "Days to add/subtract."},
            "hours": {"type": "number", "description": "Hours to add/subtract (result becomes a datetime)."},
            "minutes": {"type": "number", "description": "Minutes to add/subtract."},
            "seconds": {"type": "number", "description": "Seconds to add/subtract."},
        },
        "required": ["operation"],
    },
    func=_date_math,
)
