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
    _date_from_name,
    _to_number,
    parse_lme_offwarrant,
    parse_lme_stock_breakdown,
)

FIX = ROOT / "tests" / "fixtures"


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
    print("\nAll offline LME tests passed.")
