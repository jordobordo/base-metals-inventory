"""Offline tests for scripts/schema.py — staleness, the native as-of series,
and the tidy geo-parquet upsert.

Run:  python -m pytest tests/test_schema.py -q   |   python tests/test_schema.py
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.schema import (  # noqa: E402
    LME_GEO_SCHEMA,
    build_asof_series,
    build_native_asof_series,
    staleness,
    upsert_geo,
)


def _runs() -> pd.DataFrame:
    """3 runs: CME frozen at 08-31 (stale), LME/SHFE advancing."""
    base = {
        "cme_on_warrant_t": 400_000.0, "cme_cancelled_t": 0.0,
        "cme_off_warrant_t": 250_000.0, "cme_total_t": 650_000.0,
        "lme_on_warrant_t": 110_000.0, "lme_cancelled_t": 120_000.0,
        "lme_off_warrant_t": 115_000.0, "lme_total_t": 230_000.0,
        "shfe_on_warrant_t": 30_000.0, "shfe_cancelled_t": 40_000.0,
        "shfe_off_warrant_t": float("nan"), "shfe_total_t": 70_000.0,
    }
    rows = [
        {**base, "run_date": dt.date(2026, 8, 31), "cme_data_date": dt.date(2026, 8, 31),
         "lme_warrant_data_date": dt.date(2026, 8, 28), "lme_offwarrant_data_date": dt.date(2026, 8, 27),
         "shfe_data_date": dt.date(2026, 8, 28)},
        {**base, "run_date": dt.date(2026, 9, 4), "cme_data_date": dt.date(2026, 8, 31),
         "lme_warrant_data_date": dt.date(2026, 9, 2), "lme_offwarrant_data_date": dt.date(2026, 8, 31),
         "shfe_data_date": dt.date(2026, 9, 4), "lme_total_t": 232_000.0},
        {**base, "run_date": dt.date(2026, 9, 9), "cme_data_date": dt.date(2026, 8, 31),
         "lme_warrant_data_date": dt.date(2026, 9, 4), "lme_offwarrant_data_date": dt.date(2026, 9, 3),
         "shfe_data_date": dt.date(2026, 9, 4), "lme_total_t": 235_000.0},
    ]
    return pd.DataFrame(rows)


def test_staleness() -> None:
    st = staleness(_runs(), ref=dt.date(2026, 9, 9))
    assert st["cme"]["as_of"] == dt.date(2026, 8, 31)
    assert st["cme"]["bdays_stale"] == 7 and st["cme"]["stale"] is True
    assert st["lme"]["as_of"] == dt.date(2026, 9, 4) and st["lme"]["stale"] is False
    assert st["shfe"]["stale"] is False
    print("test_staleness: OK", {k: v["bdays_stale"] for k, v in st.items()})


def test_native_series_stops_at_stale() -> None:
    a = build_asof_series(_runs(), end=dt.date(2026, 9, 9)).set_index("date")
    n = build_native_asof_series(_runs(), end=dt.date(2026, 9, 9)).set_index("date")

    # synced view carries CME forward; native view does not
    assert a.loc["2026-09-09", "cme_total_t"] == 650_000.0
    assert a.loc["2026-09-09", "cme_stale"]  # tail flagged
    assert pd.isna(n.loc["2026-09-04", "cme_total_t"])          # past CME's last report
    assert n.loc["2026-08-31", "cme_total_t"] == 650_000.0       # on it

    # native global reported stock stops once CME drops out (needs all 3 legs)
    assert pd.notna(n.loc["2026-08-31", "global_reported_stock_t"])
    assert pd.isna(n.loc["2026-09-04", "global_reported_stock_t"])
    # synced global keeps going
    assert pd.notna(a.loc["2026-09-09", "global_reported_stock_t"])
    print("test_native_series_stops_at_stale: OK")


def test_upsert_geo() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lme_geo.parquet"
        r1 = [{"run_date": dt.date(2026, 9, 4), "report_date": dt.date(2026, 9, 2),
               "report_type": "breakdown", "region": "Germany", "location": "Hamburg",
               "on_warrant_t": 4000.0, "cancelled_t": 1200.0, "opening_t": None,
               "delivered_in_t": None, "delivered_out_t": None, "closing_t": 5200.0,
               "off_warrant_t": None, "retrieved_at": dt.datetime.now(dt.timezone.utc)}]
        upsert_geo(r1, p)
        # same key again with a new value -> replace, not duplicate
        r2 = [{**r1[0], "on_warrant_t": 4500.0, "closing_t": 5700.0}]
        out = upsert_geo(r2, p)
        assert list(out.columns) == LME_GEO_SCHEMA
        assert len(out) == 1
        assert out.iloc[0]["on_warrant_t"] == 4500.0
        # a different location adds a row
        r3 = [{**r1[0], "location": "Antwerp", "region": "Belgium"}]
        out = upsert_geo(r3, p)
        assert len(out) == 2
        assert upsert_geo([], p).equals(out)  # empty rows -> no-op
        print("test_upsert_geo: OK", len(out), "rows")


if __name__ == "__main__":
    test_staleness()
    test_native_series_stops_at_stale()
    test_upsert_geo()
    print("\nAll offline schema tests passed.")
