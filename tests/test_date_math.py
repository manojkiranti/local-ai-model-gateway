"""Offline tests for the date_math local tool (stdlib datetime arithmetic)."""

import asyncio

import pytest

from app.tools.local import date_math


def _run(args):
    return asyncio.run(date_math.SPEC.func(args))


# ---- add / subtract with timedelta units ----

def test_add_days():
    assert _run({"operation": "add", "date": "2026-08-05", "days": 10}).endswith("= 2026-08-15")


def test_subtract_weeks():
    assert _run({"operation": "subtract", "date": "2026-08-15", "weeks": 1}).endswith("= 2026-08-08")


def test_add_hours_gives_datetime():
    # A time component promotes the result to a datetime.
    r = _run({"operation": "add", "date": "2026-08-05T10:00:00", "hours": 5})
    assert r.endswith("= 2026-08-05T15:00:00")


# ---- month / year arithmetic with end-of-month clamping ----

def test_add_one_month():
    assert _run({"operation": "add", "date": "2026-01-15", "months": 1}).endswith("= 2026-02-15")


def test_add_month_clamps_end_of_month():
    # Jan 31 + 1 month -> Feb 28 (2026 is not a leap year).
    assert _run({"operation": "add", "date": "2026-01-31", "months": 1}).endswith("= 2026-02-28")


def test_add_years():
    assert _run({"operation": "add", "date": "2024-02-29", "years": 1}).endswith("= 2025-02-28")


# ---- diff ----

def test_diff_days():
    r = _run({"operation": "diff", "from": "2026-01-01", "to": "2026-08-05"})
    assert "216 days" in r


def test_diff_is_absolute_or_signed_consistently():
    # Order shouldn't crash; reversed gives the same magnitude.
    a = _run({"operation": "diff", "from": "2026-01-01", "to": "2026-01-11"})
    b = _run({"operation": "diff", "from": "2026-01-11", "to": "2026-01-01"})
    assert "10 days" in a and "10 days" in b


def test_diff_with_times_includes_hms():
    r = _run({"operation": "diff", "from": "2026-08-05T00:00:00", "to": "2026-08-06T06:30:00"})
    assert "1 day" in r and "6:30:00" in r


# ---- validation: friendly ERROR strings ----

def test_missing_operation():
    assert _run({"date": "2026-08-05", "days": 1}).startswith("ERROR")


def test_bad_operation():
    assert _run({"operation": "multiply", "date": "2026-08-05"}).startswith("ERROR")


def test_add_missing_date():
    assert _run({"operation": "add", "days": 1}).startswith("ERROR")


def test_add_bad_date():
    assert _run({"operation": "add", "date": "not-a-date", "days": 1}).startswith("ERROR")


def test_add_requires_a_duration():
    assert _run({"operation": "add", "date": "2026-08-05"}).startswith("ERROR")


def test_diff_missing_endpoint():
    assert _run({"operation": "diff", "from": "2026-08-05"}).startswith("ERROR")


def test_non_integer_unit_errors():
    assert _run({"operation": "add", "date": "2026-08-05", "days": "ten"}).startswith("ERROR")


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "date_math" for spec in LOCAL_TOOLS)
