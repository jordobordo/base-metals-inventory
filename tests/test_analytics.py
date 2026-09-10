"""Offline tests for scripts/analytics.py — warrant lifecycle, rolling Z-scores,
and the CME-LME arbitrage hurdle model.  No network; synthetic frames only.

Run:  python -m pytest tests/test_analytics.py -q   |   python tests/test_analytics.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.analytics import (  # noqa: E402
    ArbCostBand,
    anomaly_scan,
    arb_hurdle_frame,
    cancellation_concentration,
    cancelled_share,
    classify_arb_regime,
    diagnose_anomalies,
    hub_of,
    hub_warrant_status,
    loadout_response,
    location_warrant_flows,
    location_warrant_status,
    net_arb_margin,
    net_draw_rate,
    rolling_zscore,
    scarcity_scorecard,
    zscore_frame,
)

_TS = pd.Timestamp("2026-09-09 12:00:00+00:00")


def _geo() -> pd.DataFrame:
    """Two LME locations over four reports.

    Rotterdam: 09-02 phantom drawdown (cancelled up, on-warrant down, no load-out),
               09-03 re-warranting (cancelled down, on-warrant up, no load-out),
               09-04 a real load-out.
    Busan:     steady, cancelled metal actually leaving.
    """
    rows = [
        # loc,        rd,     on,     canc,   d_in, d_out, close
        ("Rotterdam", "2026-09-01", 10000, 5000, 0, 0, 15000),
        ("Rotterdam", "2026-09-02", 8000, 10000, 0, 0, 18000),
        ("Rotterdam", "2026-09-03", 13000, 4000, 0, 100, 17000),
        ("Rotterdam", "2026-09-04", 12000, 3000, 0, 2000, 15000),
        ("Busan", "2026-09-01", 6000, 2000, 0, 500, 8000),
        ("Busan", "2026-09-02", 6000, 3000, 0, 1000, 9000),
        ("Busan", "2026-09-03", 5500, 2500, 0, 900, 8000),
        ("Busan", "2026-09-04", 5000, 2000, 0, 800, 7000),
    ]
    recs = []
    for loc, rd, on, canc, d_in, d_out, close in rows:
        recs.append({
            "run_date": pd.Timestamp(rd), "report_date": pd.Timestamp(rd),
            "report_type": "breakdown",
            "region": "Netherlands" if loc == "Rotterdam" else "Korea (South)",
            "location": loc,
            "on_warrant_t": float(on), "cancelled_t": float(canc),
            "opening_t": np.nan, "delivered_in_t": float(d_in),
            "delivered_out_t": float(d_out), "closing_t": float(close),
            "off_warrant_t": np.nan, "retrieved_at": _TS,
        })
    return pd.DataFrame(recs)


def _runs() -> pd.DataFrame:
    dates = pd.bdate_range("2026-08-28", periods=8)
    cash = [14200, 14250, 14535, 14690, 14535, 14535, 14540, 14737]
    m3 = [14180, 14210, 14215, 14333, 14415, 14415, 14509, 14708]
    return pd.DataFrame({
        "run_date": dates,
        "lme_price_date": dates,
        "lme_copper_cash_usd_t": cash,
        "lme_copper_3m_usd_t": m3,
        "lme_cash_3m_spread_usd_t": [np.nan] * 7 + [29.0],
        "comex_copper_usd_t": [c + 300 for c in cash],
        "lme_copper_3m_usd_t_dup": m3,  # ignored
    })


# --------------------------------------------------------------------------- #
# 1. Warrant lifecycle
# --------------------------------------------------------------------------- #
def test_cancelled_share() -> None:
    assert abs(cancelled_share(10000, 5000) - 100 / 3) < 1e-9
    assert np.isnan(cancelled_share(0, 0))
    s = cancelled_share(pd.Series([10000.0, 0.0]), pd.Series([10000.0, 0.0]))
    assert s.iloc[0] == 50.0 and np.isnan(s.iloc[1])
    print("test_cancelled_share: OK")


def test_location_warrant_flows() -> None:
    flows = location_warrant_flows(_geo(), level="location")
    rot = flows[flows["location"] == "Rotterdam"].set_index("report_date")

    # 09-02: cancelled +5000, on-warrant -2000, no load-out -> phantom tightness
    r2 = rot.loc["2026-09-02"]
    assert r2["d_cancelled_t"] == 5000 and r2["d_on_warrant_t"] == -2000
    assert r2["new_cancellations_t"] == 5000
    assert bool(r2["phantom_tightness"]) is True
    assert bool(r2["is_rewarranting"]) is False

    # 09-03: cancelled -6000, on-warrant +5000, only 100 t out -> re-warranting
    r3 = rot.loc["2026-09-03"]
    assert r3["uncancelled_t"] == 6000
    assert r3["implied_rewarrant_t"] == 6000 - 100
    assert bool(r3["is_rewarranting"]) is True
    assert bool(r3["phantom_tightness"]) is False

    # 09-04: a genuine 2000 t load-out, cancelled only -1000 -> no re-warranting
    r4 = rot.loc["2026-09-04"]
    assert r4["withdrawals_t"] == 2000
    assert r4["implied_rewarrant_t"] == 0
    assert bool(r4["is_rewarranting"]) is False

    # cancelled_share level check (09-01: 5000 / 15000)
    assert abs(rot.loc["2026-09-01", "cancelled_share"] - 100 / 3) < 1e-9

    # Busan: cancelled metal actually leaves -> healthy conversion ratio
    bus = flows[flows["location"] == "Busan"].set_index("report_date")
    assert bus.loc["2026-09-02", "cancellation_drawdown_ratio"] == 1000 / 1000
    print("test_location_warrant_flows: OK")


def test_global_rollup() -> None:
    g = location_warrant_flows(_geo(), level="global").set_index("report_date")
    # global cancelled at 09-01 = 5000 + 2000 = 7000
    assert g.loc["2026-09-01", "cancelled_t"] == 7000
    # global on-warrant at 09-03 = 13000 + 5500
    assert g.loc["2026-09-03", "on_warrant_t"] == 18500
    assert list(g.index) == list(pd.to_datetime(
        ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]))
    print("test_global_rollup: OK")


# --------------------------------------------------------------------------- #
# 2. Rolling Z-score / anomaly detection
# --------------------------------------------------------------------------- #
def test_rolling_zscore_value() -> None:
    # fixed-count window (no DatetimeIndex): last of [0,0,0,3] over window 4.
    s = pd.Series([0.0, 0.0, 0.0, 0.0, 3.0])
    z = rolling_zscore(s, 4)
    # ddof=0, n=4, one outlier -> z == sqrt(3)
    assert abs(z.iloc[4] - np.sqrt(3)) < 1e-9
    # zero-variance window -> NaN, never inf
    assert np.isnan(rolling_zscore(pd.Series([5.0, 5.0, 5.0, 5.0]), 4).iloc[-1])
    print("test_rolling_zscore_value: OK")


def test_zscore_frame_alert() -> None:
    idx = pd.bdate_range("2026-08-03", periods=20)
    vals = np.where(np.arange(20) % 2, 10.0, 11.0)
    vals[-1] = 40.0  # blow-out
    zf = zscore_frame(pd.Series(vals, index=idx), windows=(30,), threshold=2.0)
    assert set(zf.columns) == {"value", "z30d", "alert_30d", "alert"}
    assert bool(zf["alert"].iloc[-1]) is True
    assert bool(zf["alert"].iloc[5]) is False
    print("test_zscore_frame_alert: OK")


def test_anomaly_scan_shape() -> None:
    scan = anomaly_scan(_runs(), _geo(), windows=(30, 90))
    assert set(scan) == {"net_cancellations", "load_outs", "cash_3m_spread"}
    lo = scan["load_outs"]
    assert not lo.empty and "location" in lo.columns
    assert "GLOBAL" in set(lo["location"])
    assert "alert" in lo.columns
    # spread series is built even though the stored column is mostly NaN
    assert not scan["cash_3m_spread"].empty
    print("test_anomaly_scan_shape: OK")


# --------------------------------------------------------------------------- #
# 3. Arbitrage hurdle model
# --------------------------------------------------------------------------- #
def test_arb_cost_band() -> None:
    band = ArbCostBand()  # 120 + 25 flat
    assert band.transfer_cost(lme_3m_price_mt=10000) == 145.0
    duty = ArbCostBand(tariff_pct=25, tariff_basis="lme_3m")
    assert duty.transfer_cost(lme_3m_price_mt=10000) == 145.0 + 2500.0
    duty_cme = ArbCostBand(tariff_pct=10, tariff_basis="cme")
    assert duty_cme.transfer_cost(lme_3m_price_mt=10000, cme_price_mt=12000) == 145.0 + 1200.0
    assert net_arb_margin(10400, 10000, band) == 400 - 145
    print("test_arb_cost_band: OK")


def test_classify_arb_regime() -> None:
    assert classify_arb_regime(400, 255) == "Open Physical Arb"
    assert classify_arb_regime(250, -50) == "Paper Dislocation"
    assert classify_arb_regime(-30, -175) == "No Dislocation"
    out = classify_arb_regime(pd.Series([400.0, 250.0, -30.0]),
                              pd.Series([255.0, -50.0, -175.0]))
    assert list(out) == ["Open Physical Arb", "Paper Dislocation", "No Dislocation"]
    print("test_classify_arb_regime: OK")


def test_arb_hurdle_frame() -> None:
    band = ArbCostBand(freight_usd_mt=100, finance_insurance_usd_mt=50)  # 150 flat
    arb = arb_hurdle_frame(_runs(), band=band)
    assert list(arb.columns) == [
        "cme_price_mt", "lme_3m_price_mt", "gross_spread_usd_mt",
        "transfer_cost_usd_mt", "net_arb_margin_usd_mt", "regime",
    ]
    # every row here has cme = lme_cash + 300 and a positive gross spread
    assert (arb["transfer_cost_usd_mt"] == 150.0).all()
    row = arb.iloc[0]
    assert row["net_arb_margin_usd_mt"] == row["gross_spread_usd_mt"] - 150.0
    assert row["regime"] in {"Open Physical Arb", "Paper Dislocation", "No Dislocation"}
    print("test_arb_hurdle_frame: OK")


# --------------------------------------------------------------------------- #
# Combined verdict
# --------------------------------------------------------------------------- #
def test_scarcity_scorecard() -> None:
    card = scarcity_scorecard(_runs(), _geo(), band=ArbCostBand())
    assert -1.0 <= card.score <= 1.0
    assert card.verdict in {
        "Physical scarcity", "Warehouse reshuffling / financing", "Mixed / inconclusive",
    }
    assert card.asof == dt.date(2026, 9, 4)
    # re-warranting in the synthetic geo should register as a reshuffling signal
    assert card.signals["rewarranting_events"] >= 1
    # round-trips through JSON
    json.dumps(card.as_dict(), default=str)
    print("test_scarcity_scorecard: OK", card.verdict, round(card.score, 2))


def test_empty_inputs() -> None:
    assert location_warrant_flows(pd.DataFrame()).empty
    assert arb_hurdle_frame(pd.DataFrame()).empty
    scan = anomaly_scan(_runs(), None)
    assert scan["net_cancellations"].empty and scan["load_outs"].empty
    print("test_empty_inputs: OK")


# --------------------------------------------------------------------------- #
# Scarcity-page helpers: hubs, concentration, load-out response, diagnostics
# --------------------------------------------------------------------------- #
def _mk_geo(rows: list[dict]) -> pd.DataFrame:
    recs = []
    for r in rows:
        on, canc = float(r["on"]), float(r["canc"])
        recs.append({
            "run_date": pd.Timestamp(r["rd"]), "report_date": pd.Timestamp(r["rd"]),
            "report_type": "breakdown", "region": r["region"], "location": r["loc"],
            "on_warrant_t": on, "cancelled_t": canc, "opening_t": np.nan,
            "delivered_in_t": 0.0, "delivered_out_t": float(r.get("dout", 0.0)),
            "closing_t": on + canc, "off_warrant_t": np.nan, "retrieved_at": _TS,
        })
    return pd.DataFrame(recs)


def _series_geo(specs: dict[tuple[str, str], tuple[list[float], list[float]]]) -> pd.DataFrame:
    """{(region, location): ([cancelled...], [delivered_out...])} -> tidy geo over
    consecutive business days, on-warrant held flat at 20,000."""
    dates = pd.bdate_range("2026-08-01", periods=len(next(iter(specs.values()))[0]))
    rows = []
    for (region, loc), (canc, dout) in specs.items():
        for d, c, o in zip(dates, canc, dout):
            rows.append({"rd": d, "region": region, "loc": loc,
                         "on": 20000.0, "canc": c, "dout": o})
    return _mk_geo(rows)


def test_hub_of() -> None:
    assert hub_of("Singapore", "Singapore") == "Singapore"
    assert hub_of("Netherlands", "Rotterdam") == "Rotterdam"
    assert hub_of("Korea (South)", "Busan") == "Busan"
    assert hub_of("Malaysia", "Port Klang") == "Port Klang"
    assert hub_of("USA", "New Orleans") == "US"
    assert hub_of("Belgium", "Antwerp") == "Other"
    assert hub_of(np.nan, None) == "Other"
    print("test_hub_of: OK")


def test_hub_warrant_status_and_concentration() -> None:
    geo = _mk_geo([
        {"rd": "2026-09-07", "region": "USA", "loc": "New Orleans", "on": 10000, "canc": 60000},
        {"rd": "2026-09-07", "region": "USA", "loc": "Baltimore", "on": 5000, "canc": 20000},
        {"rd": "2026-09-07", "region": "Singapore", "loc": "Singapore", "on": 18000, "canc": 2000},
        {"rd": "2026-09-07", "region": "Belgium", "loc": "Antwerp", "on": 4000, "canc": 1000},
    ])
    hs = hub_warrant_status(geo).set_index("hub")
    assert hs.loc["US", "cancelled_t"] == 80000  # New Orleans + Baltimore
    assert hs.loc["US", "on_warrant_t"] == 15000
    assert abs(hs.loc["US", "cancelled_share"] - 80000 / 95000 * 100) < 1e-9
    # ordered by total desc -> US first
    assert hub_warrant_status(geo).iloc[0]["hub"] == "US"

    conc = cancellation_concentration(geo)
    assert conc["top_location"] == "New Orleans"
    assert conc["global_cancelled_t"] == 83000
    assert conc["top_share_pct"] == round(60000 / 83000 * 100, 1)
    assert conc["top_hub"] == "US" and conc["top_hub_share_pct"] == round(80000 / 83000 * 100, 1)
    print("test_hub_warrant_status_and_concentration: OK")


def test_location_warrant_status() -> None:
    geo = _mk_geo([
        {"rd": "2026-09-07", "region": "USA", "loc": "New Orleans", "on": 10000, "canc": 60000},
        {"rd": "2026-09-07", "region": "Singapore", "loc": "Singapore", "on": 18000, "canc": 2000},
        {"rd": "2026-09-07", "region": "UK", "loc": "Liverpool", "on": 0, "canc": 0},  # empty
    ])
    ls = location_warrant_status(geo)
    assert list(ls["location"]) == ["New Orleans", "Singapore"]  # zero-stock dropped, total desc
    assert ls.iloc[0]["hub"] == "US" and ls.iloc[1]["hub"] == "Singapore"
    # reconciles to the LME warrant totals (bars sum == lme_on_warrant / lme_cancelled)
    assert ls["on_warrant_t"].sum() == 28000 and ls["cancelled_t"].sum() == 62000
    assert location_warrant_status(geo, drop_zero=False).shape[0] == 3
    print("test_location_warrant_status: OK")


def test_loadout_response() -> None:
    # 14 quiet reports then a 40k cancellation spike; no metal leaves afterwards
    canc = [5000.0] * 14 + [45000.0]
    dout = [0.0] * 15
    unmet = loadout_response(_series_geo({("USA", "New Orleans"): (canc, dout)}),
                             level="global", lag_bdays=10)
    assert len(unmet) == 1
    assert bool(unmet.iloc[0]["responded"]) is False
    assert unmet.iloc[0]["new_cancellations_t"] == 40000.0
    assert unmet.iloc[0]["unmet_t"] == 40000.0

    # same spike, but 30k loads out within the window -> responded
    dout2 = [0.0] * 14 + [0.0]
    geo2 = _series_geo({("USA", "New Orleans"): (canc + [45000.0, 45000.0],
                                                 dout2 + [20000.0, 15000.0])})
    resp = loadout_response(geo2, level="global", lag_bdays=10)
    assert bool(resp.iloc[0]["responded"]) is True
    print("test_loadout_response: OK")


def test_net_draw_rate() -> None:
    # needs the exchange-native inputs; a frame without them yields an empty series
    assert net_draw_rate(pd.DataFrame({"run_date": [dt.date(2026, 9, 1)]})).empty
    dates = pd.bdate_range("2026-09-01", periods=4)
    runs = pd.DataFrame({
        "run_date": dates,
        "cme_data_date": dates, "lme_warrant_data_date": dates, "shfe_data_date": dates,
        "cme_total_t": [300.0, 300.0, 300.0, 300.0],
        "lme_total_t": [200.0, 195.0, 180.0, 150.0],
        "shfe_total_t": [100.0, 100.0, 100.0, 100.0],
    })
    ndr = net_draw_rate(runs)
    assert not ndr.empty and (ndr < 0).all()  # inventory only draws down here
    print("test_net_draw_rate: OK")


def test_diagnose_anomalies_tags() -> None:
    quiet, spike = [5000.0] * 14 + [60000.0], [0.0] * 15
    lo_quiet, lo_spike = [5000.0] * 15, [100.0] * 14 + [50000.0]
    geo = _series_geo({
        ("USA", "New Orleans"): (quiet, spike),          # isolated US cancellation
        ("Korea (South)", "Busan"): (quiet, spike),      # regional pair ...
        ("Korea (South)", "Gwangyang"): (quiet, spike),  # ... same region
        ("Singapore", "Singapore"): (lo_quiet, lo_spike),  # load-out spike, arb open
    })
    diag = diagnose_anomalies(_runs(), geo, band=ArbCostBand()).set_index("location")
    assert diag.loc["New Orleans", "interpretation"] == "Isolated Cancellation - Low Physical Drain"
    assert diag.loc["Busan", "interpretation"] == "Broad Regional Tightening"
    assert diag.loc["Gwangyang", "interpretation"] == "Broad Regional Tightening"
    assert diag.loc["Singapore", "interpretation"] == "Transpacific Arb Delivery Candidate"
    assert diag.loc["New Orleans", "hub"] == "US"
    print("test_diagnose_anomalies_tags: OK")


def test_diagnose_anomalies_empty() -> None:
    assert diagnose_anomalies(_runs(), None).empty
    # _geo() has too little history to trip |Z|>2, but its Rotterdam re-warranting
    # event still surfaces (that is itself an alert condition).
    d = diagnose_anomalies(_runs(), _geo())
    assert list(d["location"]) == ["Rotterdam"]
    assert d.iloc[0]["rewarrant_events"] >= 1
    assert d.iloc[0]["interpretation"] == "Re-warranting / Paper Hold"
    print("test_diagnose_anomalies_empty: OK")


if __name__ == "__main__":
    test_cancelled_share()
    test_location_warrant_flows()
    test_global_rollup()
    test_rolling_zscore_value()
    test_zscore_frame_alert()
    test_anomaly_scan_shape()
    test_arb_cost_band()
    test_classify_arb_regime()
    test_arb_hurdle_frame()
    test_scarcity_scorecard()
    test_empty_inputs()
    test_hub_of()
    test_hub_warrant_status_and_concentration()
    test_location_warrant_status()
    test_loadout_response()
    test_net_draw_rate()
    test_diagnose_anomalies_tags()
    test_diagnose_anomalies_empty()
    print("\nAll offline analytics tests passed.")
