"""Offline tests for STEP 1 — CME copper scraper parsing logic.

No network. Validates `parse_cme_copper_stocks` against two committed fixtures:

  * cme_copper_stocks_sample.xls       -> a real Copper_Stocks.xls captured
                                          2026-09-01 (Activity Date 2026-08-28)
  * cme_copper_stocks_html_legacy.xls  -> a synthetic HTML-as-.xls file that
                                          exercises the generic fallback parser

Run:  python -m pytest tests/test_cme_scraper.py -q
 or:  python tests/test_cme_scraper.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.cme_scraper import (  # noqa: E402
    CMEBlockedError,
    _raise_for_block,
    _to_number,
    parse_cme_copper_stocks,
)

FIX = ROOT / "tests" / "fixtures"


def test_parse_real_xls() -> None:
    rec = parse_cme_copper_stocks((FIX / "cme_copper_stocks_sample.xls").read_bytes()).as_record()
    assert rec["source"] == "CME"
    assert rec["unit"] == "short_ton"
    assert rec["report_date"] == dt.date(2026, 8, 28)      # Activity Date
    assert rec["publish_date"] == dt.date(2026, 8, 31)     # Report Date
    assert rec["registered_short_tons"] == 477_482.0
    assert rec["eligible_short_tons"] == 281_407.0
    assert rec["total_short_tons"] == 758_889.0
    assert rec["registered_short_tons"] + rec["eligible_short_tons"] == rec["total_short_tons"]
    print("test_parse_real_xls: OK", rec)


def test_parse_legacy_html_fallback() -> None:
    rec = parse_cme_copper_stocks((FIX / "cme_copper_stocks_html_legacy.xls").read_bytes()).as_record()
    assert rec["report_date"] == dt.date(2026, 8, 29)
    assert rec["registered_short_tons"] == 11_300.0
    assert rec["eligible_short_tons"] == 26_050.0
    assert rec["total_short_tons"] == 37_350.0
    print("test_parse_legacy_html_fallback: OK", rec)


def test_to_number() -> None:
    assert _to_number("1,234.5") == 1234.5
    assert _to_number("(500)") == -500.0
    assert _to_number("-") is None
    assert _to_number("") is None
    assert _to_number("N/A") is None
    assert _to_number(42) == 42.0
    assert _to_number("$12,000") == 12000.0
    print("test_to_number: OK")


def test_block_detection() -> None:
    body = (
        b'{"message": "This IP address is blocked due to suspected web scraping '
        b'activity associated with it on this CMEgroup.com page."}'
    )
    try:
        _raise_for_block(403, body)
    except CMEBlockedError:
        print("test_block_detection: OK")
    else:  # pragma: no cover
        raise AssertionError("expected CMEBlockedError")


if __name__ == "__main__":
    test_to_number()
    test_block_detection()
    test_parse_real_xls()
    test_parse_legacy_html_fallback()
    print("\nAll offline CME tests passed.")
