"""Bikram Sambat <-> Gregorian conversion (`app/nepali_date.py`).

Pure module, no DB/HTTP/model. The known pairs below were verified against
PRIMARY sources, not against the library the table came from — see the
provenance note in `app/nepali_calendar_data.py`.
"""

from __future__ import annotations

from datetime import date

import pytest

from app import nepali_date as nd


# --------------------------------------------------------------------------- #
# Known pairs, each verified against a primary source
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bs, ad",
    [
        # The table's anchor.
        ((1975, 1, 1), date(1918, 4, 13)),
        # Nepali New Year 2081 — widely published as 13 April 2024.
        ((2081, 1, 1), date(2024, 4, 13)),
        # Nepali New Year 2082.
        ((2082, 1, 1), date(2025, 4, 14)),
        # Baisakh 2082 has THIRTY-ONE days: hamropatro.com/date/2082-1-31 shows
        # Wed 14 May 2025. This is the day two published tables disagree on.
        ((2082, 1, 31), date(2025, 5, 14)),
        # ...so Jestha 1 is 15 May 2025, as ADBL Bank published in its own
        # interest-rate notice ("Jestha 01, 2082 (15 May, 2025)").
        ((2082, 2, 1), date(2025, 5, 15)),
        # Nepal's fiscal year 2082/83 starts on Shrawan 1.
        ((2082, 4, 1), date(2025, 7, 17)),
    ],
)
def test_known_pairs_convert_both_ways(bs, ad):
    assert nd.to_ad(nd.BsDate(*bs)) == ad
    assert nd.from_ad(ad) == nd.BsDate(*bs)


def test_every_date_in_the_table_round_trips():
    """The property that catches a transcription error anywhere in 126 years of
    month lengths: BS -> AD -> BS must be the identity for every day."""
    checked = 0
    for year in nd.supported_years():
        for month in range(1, 13):
            for day in range(1, nd.days_in_month(year, month) + 1):
                bs = nd.BsDate(year, month, day)
                assert nd.from_ad(nd.to_ad(bs)) == bs
                checked += 1
    assert checked == 46022, checked


def test_consecutive_bs_days_are_consecutive_ad_days():
    """Catches a table whose months are individually plausible but whose totals
    drift — a round trip alone would not notice a uniform shift."""
    previous = None
    for year in (2081, 2082, 2083):
        for month in range(1, 13):
            for day in range(1, nd.days_in_month(year, month) + 1):
                current = nd.to_ad(nd.BsDate(year, month, day))
                if previous is not None:
                    assert (current - previous).days == 1, (year, month, day)
                previous = current


# --------------------------------------------------------------------------- #
# Range: refuse, never extrapolate
# --------------------------------------------------------------------------- #
def test_a_bs_year_below_the_table_is_refused():
    with pytest.raises(nd.OutOfRange):
        nd.to_ad(nd.BsDate(1974, 12, 30))


def test_a_bs_year_above_the_table_is_refused():
    with pytest.raises(nd.OutOfRange):
        nd.to_ad(nd.BsDate(2101, 1, 1))


def test_an_ad_date_outside_the_table_is_refused():
    with pytest.raises(nd.OutOfRange):
        nd.from_ad(date(1900, 1, 1))
    with pytest.raises(nd.OutOfRange):
        nd.from_ad(date(2099, 1, 1))


def test_a_day_past_the_end_of_its_month_is_refused():
    """Month lengths vary 29-32 and there is no rule — only the table knows."""
    assert nd.days_in_month(2082, 1) == 31
    with pytest.raises(nd.InvalidDate):
        nd.to_ad(nd.BsDate(2082, 1, 32))


def test_an_impossible_month_is_refused():
    with pytest.raises(nd.InvalidDate):
        nd.to_ad(nd.BsDate(2082, 13, 1))


# --------------------------------------------------------------------------- #
# Fiscal year — starts SHRAWAN 1, not Baisakh 1
# --------------------------------------------------------------------------- #
def test_the_fiscal_year_starts_on_shrawan_not_on_new_year():
    """Nepal's FY runs Shrawan 1 -> Ashar end. Anchoring it to the BS year
    boundary is off by three and a half months and mislabels every circular."""
    assert nd.fiscal_year(nd.BsDate(2082, 4, 1)) == "2082/83"
    assert nd.fiscal_year(nd.BsDate(2082, 3, 31)) == "2081/82"
    assert nd.fiscal_year(nd.BsDate(2082, 1, 1)) == "2081/82"
    assert nd.fiscal_year(nd.BsDate(2082, 12, 30)) == "2082/83"


def test_the_fiscal_year_of_an_ad_date_uses_the_nepali_date():
    assert nd.fiscal_year(nd.from_ad(date(2025, 7, 17))) == "2082/83"
    assert nd.fiscal_year(nd.from_ad(date(2025, 7, 16))) == "2081/82"


def test_a_fiscal_year_label_resolves_to_its_ad_span():
    start, end = nd.fiscal_year_span("2082/83")
    assert nd.to_ad(start) == date(2025, 7, 17)
    assert start == nd.BsDate(2082, 4, 1)
    assert end == nd.BsDate(2083, 3, nd.days_in_month(2083, 3))


def test_the_catalogs_own_label_spellings_are_accepted():
    """NRB writes these as '2082/83' and '2082-83' (the category slug)."""
    assert nd.fiscal_year_span("2082-83") == nd.fiscal_year_span("2082/83")
    assert nd.fiscal_year_span("2082/2083") == nd.fiscal_year_span("2082/83")


def test_a_fiscal_year_label_that_is_not_consecutive_is_refused():
    with pytest.raises(nd.InvalidDate):
        nd.fiscal_year_span("2082/84")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_devanagari_digits_are_accepted():
    """NRB writes २०८२; refusing it would make the tool useless on real text."""
    assert nd.parse("२०८२-०१-३१") == nd.BsDate(2082, 1, 31)


def test_a_month_name_is_accepted_in_its_common_romanisations():
    for spelling in ("Shrawan", "Sawan", "Saun", "श्रावण"):
        assert nd.parse(f"1 {spelling} 2082") == nd.BsDate(2082, 4, 1), spelling


def test_an_unknown_month_name_is_refused_rather_than_guessed():
    with pytest.raises(nd.InvalidDate):
        nd.parse("1 Smarch 2082")


# --------------------------------------------------------------------------- #
# Today
# --------------------------------------------------------------------------- #
def test_today_comes_from_nepal_time_not_utc(monkeypatch):
    """From 18:15 UTC it is already tomorrow in Kathmandu. Deriving 'today' from
    UTC is wrong for a quarter of every day — the get_nrb_forex failure."""
    import app.localtime as localtime

    monkeypatch.setattr(localtime, "today", lambda: date(2025, 5, 15))
    assert nd.today() == nd.BsDate(2082, 2, 1)


def test_the_month_carries_both_names():
    bs = nd.BsDate(2082, 4, 1)
    assert bs.month_name == "Shrawan"
    assert bs.month_name_nepali == "श्रावण"
    assert bs.isoformat() == "2082-04-01"


def test_a_fiscal_year_label_at_the_century_boundary_can_be_read_back():
    """'%02d' of 2100 is '00', which the parser then reads as the year 2000 and
    rejects. BS 2099 is AD 2042 — inside the table, so this is reachable."""
    label = nd.fiscal_year(nd.BsDate(2099, 4, 1))
    start, _ = nd.fiscal_year_span(label)
    assert start == nd.BsDate(2099, 4, 1)


def test_every_fiscal_year_label_the_table_can_produce_reads_back():
    """Exhaustive, because the century-boundary bug above was only reachable in
    one year out of 126 and no hand-picked case would have found it."""
    checked = 0
    for year in nd.supported_years():
        label = nd.fiscal_year(nd.BsDate(year, 4, 1))
        if year == max(nd.supported_years()):
            continue  # its END year is past the table; asserted separately below
        start, _ = nd.fiscal_year_span(label)
        assert start.year == year, label
        checked += 1
    assert checked == 125


def test_the_last_fiscal_year_refuses_rather_than_extrapolating_past_the_table():
    """FY 2100/01 ends in BS 2101, which the table does not cover. Its END DATE
    is genuinely unknown, so refusing is the correct answer — not a gap to
    paper over by assuming a month length."""
    label = nd.fiscal_year(nd.BsDate(2100, 4, 1))
    assert label == "2100/01"
    with pytest.raises(nd.OutOfRange):
        nd.fiscal_year_span(label)
