"""Offline tests for STEP 3 — SHFE copper inventory parsing.

No network. Fixtures captured 2026-09-01:
  * shfe_weekly_stock_sample.html   -> weekly 库存周报 for 2026-08-28
  * shfe_daily_warrant_sample.html  -> daily 仓单日报 for 2026-09-01

Run:  python -m pytest tests/test_shfe_scraper.py -q
 or:  python tests/test_shfe_scraper.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.shfe_scraper import (  # noqa: E402
    _to_number,
    parse_shfe_daily_warrant,
    parse_shfe_weekly_stock,
)

FIX = ROOT / "tests" / "fixtures"


def test_parse_weekly() -> None:
    html = (FIX / "shfe_weekly_stock_sample.html").read_text(encoding="utf-8")
    rec = parse_shfe_weekly_stock(html).as_record()
    assert rec["source"] == "SHFE" and rec["report_type"] == "weekly"
    assert rec["metal"] == "Copper"
    assert rec["unit"] == "metric_tonne"
    assert rec["report_date"] == dt.date(2026, 8, 28)
    assert rec["physical_inventory_tonnes"] == 72_428.0     # This Week / Delivery-able
    assert rec["futures_warrant_tonnes"] == 31_462.0        # This Week / On Warrant
    assert rec["implied_non_warranted_tonnes"] == 40_966.0  # inventory - warrant
    assert rec["inventory_change_tonnes"] == -17_120.0
    assert rec["warrant_change_tonnes"] == -14_643.0
    print("test_parse_weekly: OK", rec)


def test_parse_daily() -> None:
    html = (FIX / "shfe_daily_warrant_sample.html").read_text(encoding="utf-8")
    rec = parse_shfe_daily_warrant(html).as_record()
    assert rec["source"] == "SHFE" and rec["report_type"] == "daily_warrant"
    assert rec["report_date"] == dt.date(2026, 9, 1)
    assert rec["futures_warrant_tonnes"] == 29_766.0
    assert rec["warrant_change_tonnes"] == -574.0
    print("test_parse_daily: OK", rec)


def test_weekly_excludes_copper_bc() -> None:
    """COPPER(BC) is a separate contract; its section must not bleed in."""
    html = (FIX / "shfe_weekly_stock_sample.html").read_text(encoding="utf-8")
    rec = parse_shfe_weekly_stock(html).as_record()
    # BC copper totals are an order of magnitude smaller; the plain-COPPER
    # inventory here is ~72k t, which is the main-contract figure.
    assert rec["physical_inventory_tonnes"] > 60_000


def test_to_number() -> None:
    assert _to_number("119775") == 119775.0
    assert _to_number("-124") == -124.0
    assert _to_number("1,234") == 1234.0
    assert _to_number("") is None
    assert _to_number("-") is None
    print("test_to_number: OK")


if __name__ == "__main__":
    test_to_number()
    test_parse_weekly()
    test_parse_daily()
    test_weekly_excludes_copper_bc()
    print("\nAll offline SHFE tests passed.")
