"""Offline tests for the nepali_date tool. No DB, no file store, no model."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.tools.local import nepali_date as tool


def _call(args):
    return asyncio.run(tool.SPEC.func(args))


def test_an_ad_date_converts_to_bs():
    out = _call({"date": "2025-05-15", "to": "bs"})
    assert "2082-02-01" in out
    assert "Jestha" in out


def test_a_bs_date_converts_to_ad():
    out = _call({"date": "2082-01-31", "to": "ad"})
    assert "2025-05-14" in out


def test_omitting_the_date_gives_today_in_both_calendars(monkeypatch):
    import app.localtime as localtime

    monkeypatch.setattr(localtime, "today", lambda: date(2025, 7, 17))
    out = _call({})
    assert "2082-04-01" in out
    assert "2025-07-17" in out


def test_every_answer_states_the_fiscal_year():
    """The reason this tool exists for NRB work: a document's fiscal year is
    what the catalog files it under."""
    out = _call({"date": "2025-07-17", "to": "bs"})
    assert "2082/83" in out
    out = _call({"date": "2025-07-16", "to": "bs"})
    assert "2081/82" in out


def test_a_fiscal_year_label_resolves_to_its_gregorian_span():
    out = _call({"fiscal_year": "2082/83"})
    assert "2025-07-17" in out
    # Ashar 2083 has THIRTY-TWO days, so the year ends on Ashar 32, not 31.
    assert "2026-07-16" in out
    assert "32 Ashar 2083" in out


def test_the_catalog_slug_spelling_of_a_fiscal_year_is_accepted():
    assert _call({"fiscal_year": "2082-83"}) == _call({"fiscal_year": "2082/83"})


def test_devanagari_digits_are_accepted():
    out = _call({"date": "२०८२-०१-३१", "to": "ad"})
    assert "2025-05-14" in out


def test_a_date_outside_the_table_is_an_error_not_a_guess():
    out = _call({"date": "1850-01-01", "to": "bs"})
    assert out.startswith("ERROR:")
    assert "range" in out


def test_an_unparseable_date_is_an_error_naming_the_expected_shape():
    out = _call({"date": "next tuesday", "to": "bs"})
    assert out.startswith("ERROR:")
    assert "2082" in out


def test_a_bs_day_that_does_not_exist_is_refused():
    """Baisakh 2082 has 31 days; month lengths vary and only the table knows."""
    out = _call({"date": "2082-01-32", "to": "ad"})
    assert out.startswith("ERROR:")


def test_the_answer_names_the_weekday():
    out = _call({"date": "2025-05-15", "to": "bs"})
    assert "Thursday" in out


def test_the_schema_stays_small():
    """This tool was chosen over bigger candidates BECAUSE it is cheap: every
    schema is re-sent on every turn of every conversation."""
    import json

    props = tool.SPEC.parameters["properties"]
    assert set(props) == {"date", "to", "fiscal_year"}
    assert len(json.dumps(tool.SPEC.parameters)) < 900


def test_date_math_routes_nepali_dates_to_this_tool():
    """date_math is Gregorian-only. Handed '2082-01-31' it would either fail or
    do calendar arithmetic on a year 2082 that is not the year meant — so its
    description has to name this tool, the aggregate_excel cross-reference rule."""
    from app.tools.local import date_math

    assert "nepali_date" in date_math.SPEC.description


def test_a_bs_date_sent_without_to_is_told_how_to_fix_the_call():
    """to defaults to 'bs' (input is Gregorian), so the obvious call for
    'convert 2082-01-31' answered with an AD range — nothing the model can act
    on, so it retries or gives up instead of resending with to='ad'."""
    out = _call({"date": "2082-01-31"})
    assert out.startswith("ERROR:")
    assert "to='ad'" in out
    assert "Bikram Sambat" in out
