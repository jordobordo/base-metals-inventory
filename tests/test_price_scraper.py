"""Offline tests for the CME–LME copper price spread scraper."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.price_scraper import (  # noqa: E402
    LB_PER_TONNE,
    ComexCopperPrice,
    _parse_westmetall,
    _parse_westmetall_date,
    _to_price,
)

_WESTMETALL_SNIPPET = """
<table>
<tr><td>date</td><td>LME Copper Cash-Settlement</td><td>LME Copper 3-month</td><td>LME Copper stock</td></tr>
<tr><td>01. September 2026</td><td>14,395.50</td><td>14,215.00</td><td>233,500</td></tr>
<tr><td>date</td><td>LME Copper Cash-Settlement</td><td>LME Copper 3-month</td><td>LME Copper stock</td></tr>
<tr><td>28. August 2026</td><td>14,535.00</td><td>14,370.00</td><td>234,275</td></tr>
<tr><td>27. August 2026</td><td>14,490.00</td><td>14,236.00</td><td>235,575</td></tr>
</table>
"""


def test_parse_westmetall() -> None:
    rows = _parse_westmetall(_WESTMETALL_SNIPPET)
    assert rows[0] == (dt.date(2026, 9, 1), 14_395.50, 14_215.00)  # newest first
    assert rows[-1][0] == dt.date(2026, 8, 27)
    assert len(rows) == 3
    print("test_parse_westmetall: OK", rows[0])


def test_parse_date_and_price() -> None:
    assert _parse_westmetall_date("01. September 2026") == dt.date(2026, 9, 1)
    assert _parse_westmetall_date("2026-08-28") == dt.date(2026, 8, 28)
    assert _parse_westmetall_date("garbage") is None
    assert _to_price("14,395.50") == 14_395.50
    assert _to_price("$1,234") == 1234.0
    assert _to_price("n/a") is None
    print("test_parse_date_and_price: OK")


def test_lb_to_tonne_conversion() -> None:
    p = ComexCopperPrice(price_date=dt.date(2026, 9, 1), usd_per_lb=6.5065)
    # 6.5065 USD/lb * 2204.62 lb/t ~= 14,344 USD/t
    assert abs(p.usd_per_tonne - 6.5065 * LB_PER_TONNE) < 0.01
    assert 14_300 < p.usd_per_tonne < 14_400
    print("test_lb_to_tonne_conversion: OK", p.usd_per_tonne)


if __name__ == "__main__":
    test_parse_westmetall()
    test_parse_date_and_price()
    test_lb_to_tonne_conversion()
    print("\nAll offline price-scraper tests passed.")
