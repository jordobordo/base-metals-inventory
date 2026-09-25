"""Offline tests for STEP 2 — LME stock-breakdown parsing.

No network. Two committed fixtures:

  * lme_stock_breakdown_sample.xls  -> real "Metals Reports 26 Aug 2026" file
  * lme_stock_breakdown_2017.xls    -> real 2017 file (older header wording),
                                       proves the parser is layout-tolerant

Run:  python -m pytest tests/test_lme_scraper.py -q
 or:  python tests/test_lme_scraper.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lme_scraper import (  # noqa: E402
    LMEBlockedError,
    _date_from_name,
    _get,
    _to_number,
    parse_lme_offwarrant,
    parse_lme_offwarrant_regions,
    parse_lme_stock_breakdown,
    parse_lme_stock_breakdown_locations,
)

FIX = ROOT / "tests" / "fixtures"


class _FakeResp:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


class _FakeSession:
    """Returns canned responses in order, one per ``.get()`` call."""

    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, *, params=None, timeout=None):
        self.calls += 1
        return self._responses[min(self.calls, len(self._responses)) - 1]


def test_parse_2026_sample() -> None:
    rec = parse_lme_stock_breakdown(
        (FIX / "lme_stock_breakdown_sample.xls").read_bytes(),
        source_report_name="Metals Reports 26 Aug 2026",
    ).as_record()
    assert rec["source"] == "LME"
    assert rec["metal"] == "Copper"
    assert rec["unit"] == "metric_tonne"
    assert rec["report_date"] == dt.date(2026, 8, 26)
    assert rec["live_warrant_tonnes"] == 107_050.0
    assert rec["cancelled_warrant_tonnes"] == 128_525.0
    assert rec["total_on_warrant_tonnes"] == 235_575.0
    assert rec["opening_stock_tonnes"] == 237_475.0
    assert rec["delivered_in_tonnes"] == 150.0
    assert rec["delivered_out_tonnes"] == 2_050.0
    assert rec["live_warrant_tonnes"] + rec["cancelled_warrant_tonnes"] == rec["total_on_warrant_tonnes"]
    print("test_parse_2026_sample: OK", rec)


def test_parse_2017_format() -> None:
    rec = parse_lme_stock_breakdown(
        (FIX / "lme_stock_breakdown_2017.xls").read_bytes(),
        source_report_name="Metals-Reports-21-Sep-2017.xls",
    ).as_record()
    assert rec["report_date"] == dt.date(2017, 9, 21)
    assert rec["live_warrant_tonnes"] == 238_525.0
    assert rec["cancelled_warrant_tonnes"] == 70_525.0
    assert rec["total_on_warrant_tonnes"] == 309_050.0
    print("test_parse_2017_format: OK", rec)


def test_parse_offwarrant() -> None:
    rec = parse_lme_offwarrant(
        (FIX / "lme_owsr_sample.xlsx").read_bytes(),
        source_report_name="Daily_OWSR 26 Aug 2026",
    ).as_record()
    assert rec["source"] == "LME"
    assert rec["unit"] == "metric_tonne"
    assert rec["report_date"] == dt.date(2026, 8, 26)
    assert rec["off_warrant_tonnes"] == 117_155.0  # GLOBAL TOTAL, CU column
    print("test_parse_offwarrant: OK", rec)


def test_parse_locations() -> None:
    locs = parse_lme_stock_breakdown_locations(
        (FIX / "lme_stock_breakdown_sample.xls").read_bytes(),
        report_date=dt.date(2026, 8, 26),
    )
    assert len(locs) == 25
    # per-location on-warrant / cancelled reconcile to the Total row
    assert sum(r["on_warrant_t"] for r in locs) == 107_050.0
    assert sum(r["cancelled_t"] for r in locs) == 128_525.0
    assert sum(r["closing_t"] for r in locs) == 235_575.0
    r0 = next(r for r in locs if r["location"] == "Hamburg")
    assert r0["country"] == "Germany"
    assert r0["report_date"] == dt.date(2026, 8, 26)
    print("test_parse_locations: OK", len(locs), "locations")


def test_parse_offwarrant_regions() -> None:
    regs = parse_lme_offwarrant_regions(
        (FIX / "lme_owsr_sample.xlsx").read_bytes(), report_date=dt.date(2026, 8, 26)
    )
    by = {r["region"]: r["off_warrant_t"] for r in regs}
    assert set(by) == {"ASIA", "EUROPE", "NORTH AMERICAS", "GLOBAL"}
    assert by["ASIA"] + by["EUROPE"] + by["NORTH AMERICAS"] == by["GLOBAL"] == 117_155.0
    print("test_parse_offwarrant_regions: OK", by)


def test_get_retries_cloudflare_challenge() -> None:
    """A Cloudflare-marker response used to raise immediately with no retry;
    it now gets the same retry+backoff as any other transient failure, since
    (like CME/Barchart) the challenge is often per-request, not a hard block."""
    cf_body = b"<html>Just a moment...</html>"
    ok_body = b'{"ok": true}'
    sess = _FakeSession([_FakeResp(403, cf_body), _FakeResp(403, cf_body), _FakeResp(200, ok_body)])
    resp = _get(sess, "https://example.test/x", retries=3, backoff=0.001, expect="json")
    assert resp.content == ok_body
    assert sess.calls == 3  # two challenges, then it got through
    print("test_get_retries_cloudflare_challenge: OK")


def test_get_raises_blocked_after_exhausting_retries() -> None:
    """Still genuinely blocked after every retry -> LMEBlockedError (not a
    generic wrapped error), so fail-fast callers can tell blocked apart from
    other failures."""
    cf_body = b"<html>Just a moment... cf-chl</html>"
    sess = _FakeSession([_FakeResp(403, cf_body)] * 3)
    try:
        _get(sess, "https://example.test/x", retries=3, backoff=0.001)
        raise AssertionError("expected LMEBlockedError")
    except LMEBlockedError:
        pass
    assert sess.calls == 3
    print("test_get_raises_blocked_after_exhausting_retries: OK")


def test_date_from_name() -> None:
    assert _date_from_name("Metals Reports 26 Aug 2026") == dt.date(2026, 8, 26)
    assert _date_from_name("Metals Reports_20260826.xls") == dt.date(2026, 8, 26)
    assert _date_from_name("Metals-Reports-1-Sep-2017.xls") == dt.date(2017, 9, 1)
    assert _date_from_name("no date here") is None
    print("test_date_from_name: OK")


def test_to_number() -> None:
    assert _to_number("1,234") == 1234.0
    assert _to_number("(50)") == -50.0
    assert _to_number(0) == 0.0
    assert _to_number("") is None
    assert _to_number("-") is None
    print("test_to_number: OK")


if __name__ == "__main__":
    test_to_number()
    test_date_from_name()
    test_parse_2026_sample()
    test_parse_2017_format()
    test_parse_offwarrant()
    test_parse_locations()
    test_parse_offwarrant_regions()
    test_get_retries_cloudflare_challenge()
    test_get_raises_blocked_after_exhausting_retries()
    print("\nAll offline LME tests passed.")
