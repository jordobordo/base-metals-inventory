"""Offline tests for STEP 4 — the aggregator.

No network: the three scrapers are monkeypatched with dicts shaped like their
real return values (numbers taken from the committed fixtures). Covers the
harmonisation maths, the global total, carry-forward on failure, parquet upsert
idempotency, and the LOCF daily series.

Run:  python -m pytest tests/test_aggregate.py -q
 or:  python tests/test_aggregate.py
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from scripts import aggregate as agg  # noqa: E402

CONV = agg.SHORT_TON_TO_TONNE

_CME = {
    "source": "CME", "report_date": dt.date(2026, 8, 28), "publish_date": dt.date(2026, 8, 31),
    "registered_short_tons": 477_482.0, "eligible_short_tons": 281_407.0,
    "total_short_tons": 758_889.0, "unit": "short_ton",
}
_LME_W = {
    "source": "LME", "report_date": dt.date(2026, 8, 26), "metal": "Copper",
    "live_warrant_tonnes": 107_050.0, "cancelled_warrant_tonnes": 128_525.0,
    "total_on_warrant_tonnes": 235_575.0, "opening_stock_tonnes": 237_475.0,
    "delivered_in_tonnes": 150.0, "delivered_out_tonnes": 2_050.0, "unit": "metric_tonne",
}
_LME_O = {
    "source": "LME", "report_date": dt.date(2026, 8, 26), "metal": "Copper",
    "off_warrant_tonnes": 117_155.0, "unit": "metric_tonne",
}
_SHFE = {
    "source": "SHFE", "report_type": "weekly", "report_date": dt.date(2026, 8, 28), "metal": "Copper",
    "physical_inventory_tonnes": 72_428.0, "futures_warrant_tonnes": 31_462.0,
    "implied_non_warranted_tonnes": 40_966.0, "inventory_change_tonnes": -17_120.0,
    "warrant_change_tonnes": -14_643.0, "unit": "metric_tonne",
    "futures_warrant_tonnes_daily": 29_766.0, "futures_warrant_daily_date": dt.date(2026, 9, 1),
}


def _patch_all(monkeypatch, *, cme=_CME, lme_w=_LME_W, lme_o=_LME_O, shfe=_SHFE):
    def mk(val):
        if isinstance(val, Exception):
            def _raise(*a, **k):
                raise val
            return _raise
        return lambda *a, **k: dict(val)

    monkeypatch.setattr(agg, "get_cme_copper_stocks", mk(cme))
    monkeypatch.setattr(agg, "get_lme_copper_warrants", mk(lme_w))
    monkeypatch.setattr(agg, "get_lme_copper_offwarrant", mk(lme_o))
    monkeypatch.setattr(agg, "get_shfe_copper_stocks", mk(shfe))


def test_harmonisation_and_global_total(monkeypatch) -> None:
    _patch_all(monkeypatch)
    row = agg.collect()

    assert row["cme_on_warrant_t"] == round(477_482.0 * CONV, 3)
    assert row["cme_cancelled_t"] == 0.0
    assert row["cme_off_warrant_t"] == round(281_407.0 * CONV, 3)
    assert row["cme_total_t"] == round(758_889.0 * CONV, 3)

    assert row["lme_on_warrant_t"] == 107_050.0
    assert row["lme_cancelled_t"] == 128_525.0
    assert row["lme_off_warrant_t"] == 117_155.0
    # LME total prefers the report's own closing stock + off-warrant
    assert row["lme_total_t"] == 235_575.0 + 117_155.0

    assert row["shfe_on_warrant_t"] == 31_462.0
    assert row["shfe_cancelled_t"] == 40_966.0
    assert pd.isna(row["shfe_off_warrant_t"])
    assert row["shfe_total_t"] == 72_428.0
    assert row["shfe_warrant_daily_t"] == 29_766.0

    # global buckets
    assert row["global_on_warrant_t"] == round(477_482.0 * CONV + 107_050.0 + 31_462.0, 3)
    assert row["global_cancelled_t"] == round(0.0 + 128_525.0 + 40_966.0, 3)
    assert row["global_off_warrant_t"] == round(281_407.0 * CONV + 117_155.0, 3)  # no SHFE
    assert row["global_total_t"] == round(row["cme_total_t"] + row["lme_total_t"] + row["shfe_total_t"], 3)

    # internal consistency: buckets sum to the total (SHFE off-warrant is 0 here,
    # and its total already == on + cancelled)
    assert abs(
        row["global_total_t"]
        - (row["global_on_warrant_t"] + row["global_cancelled_t"] + row["global_off_warrant_t"])
    ) < 1.0
    assert row["sources_ok"] == "CME,LME,LME_OWSR,SHFE"
    print("test_harmonisation_and_global_total: OK  global_total_t =", row["global_total_t"])


def test_carry_forward_on_failure(monkeypatch) -> None:
    _patch_all(monkeypatch)
    good = agg.collect()
    prev = agg._row_to_frame(good).iloc[0]

    _patch_all(monkeypatch, shfe=agg.SHFEScraperError("WAF challenge"))
    row = agg.collect(prev_row=prev)

    assert row["sources_failed"] == "SHFE"
    assert row["shfe_stale"] is True
    assert row["shfe_total_t"] == 72_428.0            # carried forward
    assert row["shfe_on_warrant_t"] == 31_462.0
    assert row["global_total_t"] is not None          # still computed
    print("test_carry_forward_on_failure: OK")


def test_strict_raises(monkeypatch) -> None:
    _patch_all(monkeypatch, cme=agg.CMEScraperError("blocked"))
    try:
        agg.collect(strict=True)
    except agg.AggregationError:
        print("test_strict_raises: OK")
    else:  # pragma: no cover
        raise AssertionError("expected AggregationError")


def test_parquet_upsert_and_locf(monkeypatch) -> None:
    _patch_all(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "copper_inventory.parquet"

        r1 = agg.collect()
        r1["run_date"] = dt.date(2026, 9, 1)
        agg.upsert_parquet(r1, pq)

        # same run_date again -> replace, not duplicate
        r1b = agg.collect()
        r1b["run_date"] = dt.date(2026, 9, 1)
        r1b["global_total_t"] = 999_999.0
        df = agg.upsert_parquet(r1b, pq)
        assert len(df) == 1
        assert df.iloc[0]["global_total_t"] == 999_999.0

        r2 = agg.collect()
        r2["run_date"] = dt.date(2026, 9, 4)
        r2["global_total_t"] = 1_000_000.0
        df = agg.upsert_parquet(r2, pq)
        assert len(df) == 2

        # default freq="B": weekends (09-05 Sat, 09-06 Sun) are excluded
        daily = agg.build_daily_series(df, end=dt.date(2026, 9, 6))
        assert list(daily["date"]) == list(pd.date_range("2026-09-01", "2026-09-06", freq="B"))
        v = daily.set_index("date")["global_total_t"]
        assert v.loc["2026-09-03"] == 999_999.0   # Thu, ffilled from Tue 09-01
        assert v.loc["2026-09-04"] == 1_000_000.0  # Fri, the 09-04 run
        # freq="D" still available for a true every-day index
        every = agg.build_daily_series(df, end=dt.date(2026, 9, 6), freq="D")
        assert list(every["date"]) == list(pd.date_range("2026-09-01", "2026-09-06", freq="D"))
        assert every.set_index("date")["global_total_t"].loc["2026-09-06"] == 1_000_000.0
        print("test_parquet_upsert_and_locf: OK")


def test_asof_series(monkeypatch) -> None:
    """As-of series places each exchange at its own report date and starts there."""
    from scripts.schema import build_asof_series

    _patch_all(monkeypatch)
    row = agg.collect()
    row["run_date"] = dt.date(2026, 9, 2)
    df = agg._row_to_frame(row)

    a = build_asof_series(df, end=dt.date(2026, 9, 2)).set_index("date")
    # fixture as-of dates: LME warrant 08-26, CME 08-28, SHFE weekly 08-28
    assert a.index.min() == pd.Timestamp("2026-08-26")  # earliest = LME
    assert a.index.max() == pd.Timestamp("2026-09-02")
    # before 08-28 only LME contributes
    assert pd.isna(a.loc["2026-08-27", "shfe_total_t"])
    assert pd.isna(a.loc["2026-08-27", "cme_total_t"])
    assert a.loc["2026-08-27", "lme_total_t"] == 235_575.0 + 117_155.0
    # from 08-28 all three are on the timeline
    assert a.loc["2026-08-28", "shfe_total_t"] == 72_428.0
    assert a.loc["2026-08-28", "cme_total_t"] == round(758_889.0 * CONV, 3)
    # global total re-summed from the aligned legs
    assert a.loc["2026-09-02", "global_total_t"] == round(
        a.loc["2026-09-02", "cme_total_t"]
        + a.loc["2026-09-02", "lme_total_t"]
        + a.loc["2026-09-02", "shfe_total_t"],
        3,
    )
    print("test_asof_series: OK")


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

    for fn in (test_harmonisation_and_global_total, test_carry_forward_on_failure,
               test_strict_raises, test_parquet_upsert_and_locf, test_asof_series):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    print("\nAll offline aggregator tests passed.")
