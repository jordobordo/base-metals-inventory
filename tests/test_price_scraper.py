"""Offline tests for the CME–LME copper price spread scraper."""

from __future__ import annotations

import datetime as dt
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.price_scraper import (  # noqa: E402
    LB_PER_TONNE,
    ComexCopperPrice,
    _contract_code,
    _parse_westmetall,
    _parse_westmetall_date,
    _to_price,
    _to_settle,
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
    # cash − 3-month term-structure spread (positive = backwardation)
    _, cash, m3 = rows[0]
    assert round(cash - m3, 2) == 180.50
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
    p = ComexCopperPrice(price_date=dt.date(2026, 9, 1), usd_per_lb=6.6005, contract="HGZ26")
    assert abs(p.usd_per_tonne - 6.6005 * LB_PER_TONNE) < 0.01
    assert 14_500 < p.usd_per_tonne < 14_600
    print("test_lb_to_tonne_conversion: OK", p.usd_per_tonne)


def test_cme_settlement_parsing() -> None:
    assert _to_settle("6.6005") == 6.6005
    assert _to_settle("6.4310A") == 6.4310   # trailing quote-type letter
    assert _to_settle("162,826") == 162826.0
    assert _to_settle("-") is None
    assert _contract_code("DEC 26") == "HGZ26"
    assert _contract_code("SEP 26") == "HGU26"
    assert _contract_code("MAR 27") == "HGH27"
    print("test_cme_settlement_parsing: OK")


def test_westmetall_is_sole_lme_source() -> None:
    """The lme.com day-delayed path is gone — Westmetall is the only LME feed."""
    import scripts.price_scraper as ps

    for removed in ("_lme_from_website", "_lme_daydelayed", "_lme_row_value"):
        assert not hasattr(ps, removed), f"{removed} should have been removed"
    src = inspect.getsource(ps.get_lme_copper_price)
    assert "_fetch_westmetall" in src and "_lme_daydelayed" not in src
    print("test_westmetall_is_sole_lme_source: OK")


def test_lme_price_on_date_nearest_prior(monkeypatch) -> None:
    """`on=<date>` returns that trading session's row, or the nearest earlier one
    if the date was a holiday — the hook the spread uses to date-align."""
    import scripts.price_scraper as ps

    monkeypatch.setattr(ps, "_fetch_westmetall", lambda url: _WESTMETALL_SNIPPET)
    exact = ps.get_lme_copper_price(on=dt.date(2026, 8, 28))
    assert exact.price_date == dt.date(2026, 8, 28)
    assert exact.three_month_usd_per_tonne == 14_370.0
    holiday = ps.get_lme_copper_price(on=dt.date(2026, 8, 31))  # 29-31 Aug absent
    assert holiday.price_date == dt.date(2026, 8, 28)
    try:
        ps.get_lme_copper_price(on=dt.date(2026, 8, 1))
        raise AssertionError("expected PriceScraperError for a date before all rows")
    except ps.PriceScraperError:
        pass
    print("test_lme_price_on_date_nearest_prior: OK")


def test_spread_aligns_lme_to_comex_session(monkeypatch) -> None:
    """The MOC spread compares COMEX and LME from the **same** session — the LME
    leg is fetched as-of the COMEX settlement date, not its own latest row."""
    import scripts.price_scraper as ps

    monkeypatch.setattr(ps, "_fetch_westmetall", lambda url: _WESTMETALL_SNIPPET)
    monkeypatch.setattr(
        ps, "get_comex_copper_price",
        lambda **kw: ps.ComexCopperPrice(price_date=dt.date(2026, 8, 28),
                                         usd_per_lb=6.60, contract="HGZ26"),
    )
    rec = ps.get_cme_lme_copper_spread()
    assert rec["comex_price_date"] == dt.date(2026, 8, 28)
    assert rec["lme_price_date"] == dt.date(2026, 8, 28)      # aligned, not 2026-09-01
    assert rec["lme_copper_3m_usd_t"] == 14_370.00            # the 28 Aug row
    cx_t = round(6.60 * LB_PER_TONNE, 2)
    assert rec["cme_lme_spread_3m_usd_t"] == round(cx_t - 14_370.00, 2)
    assert rec["cme_lme_spread_usd_t"] == round(cx_t - 14_535.00, 2)
    assert rec["lme_cash_3m_spread_usd_t"] == round(14_535.00 - 14_370.00, 2)
    print("test_spread_aligns_lme_to_comex_session: OK")


if __name__ == "__main__":
    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)
            self._undo.clear()

    test_parse_westmetall()
    test_parse_date_and_price()
    test_cme_settlement_parsing()
    test_westmetall_is_sole_lme_source()
    test_lb_to_tonne_conversion()
    for fn in (test_lme_price_on_date_nearest_prior, test_spread_aligns_lme_to_comex_session):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    print("\nAll offline price-scraper tests passed.")
