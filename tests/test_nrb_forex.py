"""get_nrb_forex + the NRB client. Offline: no test touches the NRB website.

Two layers are covered separately:
  * the tool's validation/formatting, with `fetch_forex_rates` faked;
  * the client's body parsing and HTTP handling, against a mocked httpx transport.

The response fixtures below are trimmed copies of real bodies captured from
https://www.nrb.org.np/api/forex/v1/rates on 2026-08-10 — including the quirk
that an empty-but-valid query answers with `status.code: 400`, `validation: null`
and `payload: []`, which must NOT be reported as a rejected request.
"""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app import localtime
from app.agent.loop import MAX_TOOL_RESULT_CHARS
from app.nrb import client as nrb_client
from app.nrb.client import ForexDay, NRBError, Rate
from app.tools.local import get_nrb_forex as tool


def _run(args):
    return asyncio.run(tool._get_nrb_forex(args))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _rate(iso3="USD", name="U.S. Dollar", unit="1", buy="152.04", sell="152.64"):
    return Rate(iso3=iso3, name=name, unit=unit, buy=buy, sell=sell)


def _day(day="2026-08-08", rates=None):
    return ForexDay(
        date=day,
        published_on=f"{day} 00:00:18",
        modified_on=f"{day} 16:13:04",
        rates=tuple(rates if rates is not None else [
            _rate("INR", "Indian Rupee", "100", "160.00", "160.15"),
            _rate("USD", "U.S. Dollar", "1", "152.04", "152.64"),
            _rate("EUR", "European Euro", "1", "175.27", "175.96"),
        ]),
    )


@pytest.fixture()
def faked(monkeypatch):
    """Replace the network layer; record the arguments it was called with."""
    seen = {"days": [_day()]}

    async def fake_fetch(from_date, to_date):
        seen["from"] = from_date
        seen["to"] = to_date
        if isinstance(seen["days"], Exception):
            raise seen["days"]
        return seen["days"]

    monkeypatch.setattr(tool, "fetch_forex_rates", fake_fetch)
    return seen


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_single_date_all_currencies(faked):
    out = _run({"from": "2026-08-08"})
    assert not out.startswith("ERROR")
    assert faked["from"] == "2026-08-08" and faked["to"] == "2026-08-08"
    assert "Date: 2026-08-08" in out
    for row in ("USD | unit 1 | buy 152.04 | sell 152.64",
                "INR | unit 100 | buy 160.00 | sell 160.15",
                "EUR | unit 1 | buy 175.27 | sell 175.96"):
        assert row in out
    assert "Source: Nepal Rastra Bank" in out


def test_single_date_single_currency_is_filtered(faked):
    out = _run({"from": "2026-08-08", "currency": "USD"})
    assert "Currency: USD — U.S. Dollar" in out
    assert "Buy: NPR 152.04" in out and "Sell: NPR 152.64" in out
    assert "Unit: 1" in out
    assert "EUR" not in out and "INR" not in out  # the others are filtered out


def test_lowercase_currency_is_normalized(faked):
    out = _run({"from": "2026-08-08", "currency": "  eur "})
    assert "Currency: EUR — European Euro" in out
    assert "175.27" in out


def test_unit_is_always_reported_because_inr_is_per_100(faked):
    """A rate printed without its unit is off by 100x for INR (and 10x for JPY)."""
    out = _run({"from": "2026-08-08", "currency": "inr"})
    assert "Unit: 100" in out
    assert "per 100 INR" in out


def test_date_range_for_one_currency_is_chronological(faked):
    faked["days"] = [
        _day("2026-08-06", [_rate(buy="151.90", sell="152.50")]),
        _day("2026-08-07", [_rate(buy="152.00", sell="152.60")]),
        _day("2026-08-08", [_rate(buy="152.04", sell="152.64")]),
    ]
    out = _run({"from": "2026-08-06", "to": "2026-08-08", "currency": "USD"})
    assert faked["to"] == "2026-08-08"
    assert "3 day(s) published" in out
    # Row order, not header order — the header itself names the first and last date.
    rows = [line for line in out.splitlines() if " | buy " in line]
    assert [line.split(" | ")[0] for line in rows] == [
        "2026-08-06", "2026-08-07", "2026-08-08",
    ]
    assert "2026-08-07 | buy 152.00 | sell 152.60" in out


def test_range_with_no_currency_lists_each_day_separately(faked):
    faked["days"] = [_day("2026-08-07"), _day("2026-08-08")]
    out = _run({"from": "2026-08-07", "to": "2026-08-08"})
    assert "Date: 2026-08-07" in out and "Date: 2026-08-08" in out
    assert out.count("USD | unit 1") == 2


def test_rate_values_are_passed_through_verbatim(faked):
    """Official figures: no float round-trip, no rounding, no arithmetic."""
    faked["days"] = [_day("2026-08-08", [_rate(buy="152.00", sell="9.60")])]
    out = _run({"from": "2026-08-08", "currency": "USD"})
    assert "152.00" in out and "9.60" in out  # trailing zeros survive


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    ["08/08/2026", "2026-8-8", "20260808", "2026-13-01", "2026-02-30", "yesterday", 20260808],
)
def test_malformed_from_date_is_rejected(faked, bad):
    out = _run({"from": bad})
    assert out.startswith("ERROR") and "'from'" in out
    assert "from" not in faked or faked.get("from") is None  # never called out


@pytest.mark.parametrize("bad", ["08/08/2026", "2026-13-01", "not-a-date", 5])
def test_malformed_to_date_is_rejected(faked, bad):
    out = _run({"from": "2026-08-08", "to": bad})
    assert out.startswith("ERROR") and "'to'" in out


def test_from_after_to_is_rejected(faked):
    out = _run({"from": "2026-08-09", "to": "2026-08-01"})
    assert out.startswith("ERROR")
    assert "after" in out and "2026-08-09" in out


# The date the model does NOT supply is the one that used to come from its
# training data. These lock the server-clock default in place.
@pytest.mark.parametrize("args", [{}, {"from": None}, {"from": ""}, {"from": "   "}])
def test_absent_from_date_defaults_to_today_in_nepal(faked, args):
    out = _run(dict(args))
    assert not out.startswith("ERROR")
    assert faked["from"] == localtime.today_iso()
    assert faked["to"] == localtime.today_iso()


def test_todays_default_is_the_nepal_date_not_the_utc_date(monkeypatch, faked):
    """At 19:00 UTC it is already tomorrow in Kathmandu (UTC+05:45)."""
    evening_utc = datetime(2026, 8, 10, 19, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return evening_utc.astimezone(tz)

    monkeypatch.setattr(localtime, "datetime", _FrozenDatetime)
    _run({"currency": "USD"})
    assert faked["from"] == "2026-08-11"  # Nepal date, not 2026-08-10


def test_a_currency_alone_still_means_today(faked):
    out = _run({"currency": "USD"})
    assert not out.startswith("ERROR")
    assert faked["from"] == localtime.today_iso()


def test_the_schema_requires_nothing_so_todays_rates_need_no_arguments():
    assert tool.SPEC.parameters["required"] == []


def test_blank_to_falls_back_to_from(faked):
    out = _run({"from": "2026-08-08", "to": "   ", "currency": "USD"})
    assert not out.startswith("ERROR")
    assert faked["to"] == "2026-08-08"


@pytest.mark.parametrize("bad", ["US", "USDX", "US$", "dollar", "1", "", 42])
def test_malformed_currency_is_rejected(faked, bad):
    out = _run({"from": "2026-08-08", "currency": bad})
    assert out.startswith("ERROR") and "currency" in out


def test_range_longer_than_the_cap_is_refused_with_an_actionable_message(faked):
    out = _run({"from": "2026-01-01", "to": "2026-08-08", "currency": "USD"})
    assert out.startswith("ERROR")
    assert str(tool.MAX_RANGE_DAYS) in out and "shorter" in out


def test_multi_day_all_currencies_asks_for_a_currency(faked):
    out = _run({"from": "2026-08-01", "to": "2026-08-10"})
    assert out.startswith("ERROR")
    assert "'currency'" in out and str(tool.MAX_DAYS_ALL_CURRENCIES) in out


# --------------------------------------------------------------------------- #
# No results — never fabricate
# --------------------------------------------------------------------------- #
def test_requested_currency_absent_says_so_and_names_what_exists(faked):
    out = _run({"from": "2026-08-08", "currency": "ZAR"})
    assert "no rate for 'ZAR'" in out
    assert "USD" in out and "INR" in out  # what NRB did publish
    assert "Do not estimate" in out


def test_no_rates_published_for_the_date(faked):
    faked["days"] = []
    out = _run({"from": "2027-01-01", "currency": "USD"})
    assert "published no foreign exchange rates" in out
    assert "2027-01-01" in out
    assert "Do not estimate" in out


def test_a_non_trading_day_says_no_rates_were_quoted(faked):
    """2026-08-06 is real: NRB published every currency with buy/sell null. Without
    this branch the all-currency path renders a rate table with no rows."""
    faked["days"] = [ForexDay(date="2026-08-06", published_on="2026-08-06 00:00:10",
                              modified_on=None, rates=())]
    out = _run({"from": "2026-08-06"})
    assert "quoted no exchange rates" in out
    assert "2026-08-06" in out and "public holidays" in out
    assert "Do not estimate" in out


def test_a_non_trading_day_inside_a_range_is_skipped_not_shown_empty(faked):
    faked["days"] = [
        ForexDay(date="2026-08-06", published_on=None, modified_on=None, rates=()),
        _day("2026-08-07", [_rate(buy="152.00", sell="152.60")]),
    ]
    out = _run({"from": "2026-08-06", "to": "2026-08-07", "currency": "USD"})
    assert "Date: 2026-08-07" in out and "152.00" in out
    assert "2026-08-06" not in out


def test_no_rates_published_for_a_range_names_the_period(faked):
    faked["days"] = []
    out = _run({"from": "2027-01-01", "to": "2027-01-02", "currency": "USD"})
    assert "2027-01-01 to 2027-01-02" in out


# --------------------------------------------------------------------------- #
# Upstream failures reach the model as readable errors
# --------------------------------------------------------------------------- #
def test_api_timeout_is_a_readable_error(faked):
    faked["days"] = NRBError("The Nepal Rastra Bank API timed out.")
    out = _run({"from": "2026-08-08", "currency": "USD"})
    assert out == "ERROR: The Nepal Rastra Bank API timed out."


def test_api_http_failure_is_a_readable_error(faked):
    faked["days"] = NRBError("The Nepal Rastra Bank API returned HTTP 503.")
    out = _run({"from": "2026-08-08"})
    assert out.startswith("ERROR") and "503" in out
    assert "Traceback" not in out


# --------------------------------------------------------------------------- #
# Output size
# --------------------------------------------------------------------------- #
def test_output_is_concise_for_a_normal_single_day(faked):
    out = _run({"from": "2026-08-08"})
    assert len(out) < 2000


def test_output_stays_under_the_agent_loop_cap_and_announces_trimming(faked):
    """A day with far more currencies than today's ~22 must not silently halve."""
    many = [
        _rate(f"C{i:02d}", f"Currency number {i}", "100", "123.45", "123.99")
        for i in range(80)
    ]
    faked["days"] = [_day("2026-08-06", many), _day("2026-08-07", many), _day("2026-08-08", many)]
    out = _run({"from": "2026-08-06", "to": "2026-08-08"})
    assert len(out) <= tool.MAX_OUTPUT_CHARS
    assert len(out) <= MAX_TOOL_RESULT_CHARS
    assert "TRUNCATED" in out


def test_a_full_month_for_one_currency_fits(faked):
    faked["days"] = [
        _day(f"2026-07-{d:02d}", [_rate(buy="152.04", sell="152.64")]) for d in range(1, 32)
    ]
    out = _run({"from": "2026-07-01", "to": "2026-07-31", "currency": "USD"})
    assert len(out) <= tool.MAX_OUTPUT_CHARS
    assert "TRUNCATED" not in out
    assert "31 day(s) published" in out


# --------------------------------------------------------------------------- #
# The SPEC (the routing prompt is part of the contract)
# --------------------------------------------------------------------------- #
def test_spec_name_and_schema():
    assert tool.SPEC.name == "get_nrb_forex"
    props = tool.SPEC.parameters["properties"]
    assert set(props) == {"from", "to", "currency"}
    assert tool.SPEC.parameters["required"] == []  # today's rates take no arguments


def test_schema_has_no_url_or_pagination_parameter():
    """The endpoint is application config. Pagination is the client's business."""
    schema = json.dumps(tool.SPEC.parameters).lower()
    for forbidden in ("url", "host", "page", "per_page", "endpoint"):
        assert forbidden not in schema


def test_description_routes_documents_away_from_this_tool():
    """Descriptions are routing prompts: this tool is rates only, and it points
    NRB policy/circular/directive questions at search_department_docs (§29 — NRB
    documents are searched there, not by a separate tool)."""
    desc = tool.SPEC.description.lower()
    assert "nepal rastra bank" in desc and "buying" in desc and "selling" in desc
    for negative in ("monetary policy", "circular", "directive", "law", "regulation"):
        assert negative in desc
    assert "search_department_docs" in desc  # the tool that DOES handle them
    assert "fetch_url" in desc  # don't let the model reach for the generic fetcher


def test_description_tells_the_model_to_omit_from_for_today():
    """The gateway supplies today's date (app/localtime). The description must not
    drift back to telling the model to fetch or guess it: a model that produces
    its own 'today' produces a stale one, and NRB answers for a stale date quite
    happily, so the wrong answer looks right."""
    desc = tool.SPEC.description.lower()
    assert "omit 'from'" in desc and "today" in desc
    assert "do not need get_current_time" in desc
    assert "never state a rate from memory" in desc


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "get_nrb_forex" for spec in LOCAL_TOOLS)


# --------------------------------------------------------------------------- #
# The client: body parsing (pure)
# --------------------------------------------------------------------------- #
def _body(payload, *, status=200, validation=None, pages=1):
    return {
        "status": {"code": status},
        "errors": {"validation": validation},
        "params": {},
        "data": {"payload": payload},
        "pagination": {"page": 1, "pages": pages, "per_page": 100, "total": 1,
                       "links": {"prev": None, "next": None}},
    }


REAL_DAY = {
    "date": "2026-08-08",
    "published_on": "2026-08-08 00:00:18",
    "modified_on": "2026-08-07 16:13:04",
    "rates": [
        {"currency": {"iso3": "INR", "name": "Indian Rupee", "unit": 100},
         "buy": "160.00", "sell": "160.15"},
        {"currency": {"iso3": "USD", "name": "U.S. Dollar", "unit": 1},
         "buy": "152.04", "sell": "152.64"},
    ],
}


def test_parse_reads_a_real_body():
    days, pages = nrb_client.parse_rates_body(_body([REAL_DAY]))
    assert pages == 1 and len(days) == 1
    assert days[0].date == "2026-08-08"
    assert days[0].published_on == "2026-08-08 00:00:18"
    inr, usd = days[0].rates
    assert (inr.iso3, inr.unit, inr.buy, inr.sell) == ("INR", "100", "160.00", "160.15")
    assert (usd.iso3, usd.name, usd.unit) == ("USD", "U.S. Dollar", "1")


def test_empty_payload_with_status_400_is_no_data_not_a_rejection():
    """The live API's quirk: a valid query with nothing published answers 400 with
    `validation: null` and `payload: []`. Treating that as an error would report a
    rejected request for a date that simply has no rates."""
    days, _ = nrb_client.parse_rates_body(_body([], status=400, pages=0))
    assert days == []


def test_validation_errors_raise_with_the_field_in_the_message():
    body = _body(None, status=400, validation={"from": ["From must be date with format 'Y-m-d'"]})
    with pytest.raises(NRBError) as exc:
        nrb_client.parse_rates_body(body)
    assert "from" in exc.value.message and "rejected" in exc.value.message


@pytest.mark.parametrize(
    "body",
    [
        "not a dict",
        {"data": None},
        {"data": {"payload": "nope"}},
        {},
        {"status": {"code": 200}, "data": {}},
    ],
)
def test_malformed_bodies_raise_nrb_error(body):
    with pytest.raises(NRBError):
        nrb_client.parse_rates_body(body)


def test_unreadable_rate_entries_are_skipped_not_fatal():
    day = {
        "date": "2026-08-08",
        "rates": [
            {"currency": {"iso3": "USD", "unit": 1}, "buy": "152.04", "sell": "152.64"},
            {"currency": {"name": "no iso3"}, "buy": "1", "sell": "2"},   # unusable
            {"currency": {"iso3": "EUR"}, "buy": None, "sell": "175.96"},  # half-quoted
            "garbage",
        ],
    }
    days, _ = nrb_client.parse_rates_body(_body([day]))
    assert [r.iso3 for r in days[0].rates] == ["USD"]
    assert days[0].rates[0].name == "USD"  # falls back to the code
    assert days[0].rates[0].unit == "1"


def test_a_non_trading_day_parses_to_a_day_with_no_rates():
    """Real 2026-08-06 shape: every currency present, both quotes null. No rate may
    be invented, and this is an ordinary day-to-day condition, not a broken body."""
    day = {
        "date": "2026-08-06",
        "published_on": "2026-08-06 00:00:10",
        "rates": [
            {"currency": {"iso3": "INR", "name": "Indian Rupee", "unit": 100},
             "buy": None, "sell": None},
            {"currency": {"iso3": "USD", "name": "U.S. Dollar", "unit": 1},
             "buy": None, "sell": None},
        ],
    }
    days, _ = nrb_client.parse_rates_body(_body([day]))
    assert len(days) == 1
    assert days[0].date == "2026-08-06" and days[0].rates == ()


def test_null_quotes_are_classified_as_unquoted_not_unreadable():
    """The distinction keeps a public holiday out of the warning log."""
    quoted = {"currency": {"iso3": "USD", "unit": 1}, "buy": None, "sell": None}
    assert nrb_client._parse_rate(quoted) == (None, nrb_client.UNQUOTED)
    assert nrb_client._parse_rate({"currency": {}}) == (None, nrb_client.UNREADABLE)
    assert nrb_client._parse_rate("nope") == (None, nrb_client.UNREADABLE)


# --------------------------------------------------------------------------- #
# The client: HTTP behaviour (mocked transport — never the live site)
# --------------------------------------------------------------------------- #
def _with_transport(monkeypatch, handler):
    """Route every httpx.AsyncClient the client opens through `handler`."""
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _fetch(from_date="2026-08-08", to_date="2026-08-08"):
    return asyncio.run(nrb_client.fetch_forex_rates(from_date, to_date))


def test_client_sends_the_mandatory_page_and_per_page(monkeypatch):
    """Omitting them makes the API answer with validation errors and payload:null."""
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx.Response(200, json=_body([REAL_DAY]))

    _with_transport(monkeypatch, handler)
    _fetch()
    params = seen["url"].params
    assert params["page"] == "1" and params["per_page"] == str(nrb_client.PER_PAGE)
    assert params["from"] == "2026-08-08" and params["to"] == "2026-08-08"
    assert seen["url"].path.endswith("/rates")
    assert seen["url"].host == "www.nrb.org.np"


def test_client_paginates_internally(monkeypatch):
    """The model never sees pages; the client walks them."""
    pages_seen = []

    def handler(request):
        page = int(request.url.params["page"])
        pages_seen.append(page)
        day = {**REAL_DAY, "date": f"2026-08-{page:02d}"}
        return httpx.Response(200, json=_body([day], pages=3))

    _with_transport(monkeypatch, handler)
    days = _fetch("2026-08-01", "2026-08-03")
    assert pages_seen == [1, 2, 3]
    assert [d.date for d in days] == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_pagination_is_bounded_against_a_lying_pages_count(monkeypatch):
    """`pages: 9999` must not become an unbounded download loop."""
    calls = []

    def handler(request):
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200, json=_body([{**REAL_DAY, "date": f"2026-08-{page:02d}"}], pages=9999)
        )

    _with_transport(monkeypatch, handler)
    _fetch("2026-08-01", "2026-08-31")
    assert len(calls) == nrb_client.MAX_PAGES


def test_pagination_stops_on_an_empty_page(monkeypatch):
    calls = []

    def handler(request):
        calls.append(int(request.url.params["page"]))
        payload = [REAL_DAY] if len(calls) == 1 else []
        return httpx.Response(200, json=_body(payload, pages=9999))

    _with_transport(monkeypatch, handler)
    days = _fetch()
    assert calls == [1, 2] and len(days) == 1


def test_http_error_status_becomes_nrb_error(monkeypatch):
    _with_transport(monkeypatch, lambda request: httpx.Response(503, text="down"))
    with pytest.raises(NRBError) as exc:
        _fetch()
    assert "503" in exc.value.message


def test_invalid_json_becomes_nrb_error(monkeypatch):
    _with_transport(monkeypatch, lambda request: httpx.Response(200, text="<html>oops"))
    with pytest.raises(NRBError) as exc:
        _fetch()
    assert "could not be read" in exc.value.message


def test_timeout_becomes_a_timeout_message(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    _with_transport(monkeypatch, handler)
    with pytest.raises(NRBError) as exc:
        _fetch()
    assert "timed out" in exc.value.message


def test_connect_failure_becomes_unreachable_message(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    _with_transport(monkeypatch, handler)
    with pytest.raises(NRBError) as exc:
        _fetch()
    assert "Could not reach" in exc.value.message


def test_redirects_are_not_followed(monkeypatch):
    """The host is application config; a 3xx off this endpoint is not chased."""
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/rates"})

    _with_transport(monkeypatch, handler)
    with pytest.raises(NRBError) as exc:
        _fetch()
    assert "redirect" in exc.value.message


def test_oversized_response_is_refused(monkeypatch):
    big = json.dumps(_body([REAL_DAY])) + " " * (nrb_client.MAX_RESPONSE_BYTES + 10)
    _with_transport(monkeypatch, lambda request: httpx.Response(200, text=big))
    with pytest.raises(NRBError) as exc:
        _fetch()
    assert "large" in exc.value.message


def test_base_url_comes_from_settings(monkeypatch):
    """No host ever originates from the model."""
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        return httpx.Response(200, json=_body([REAL_DAY]))

    class _S:
        nrb_api_base_url = "https://nrb-mirror.example.org/api/forex/v1"

    monkeypatch.setattr(nrb_client, "get_settings", lambda: _S())
    _with_transport(monkeypatch, handler)
    _fetch()
    assert seen["host"] == "nrb-mirror.example.org"
