"""Render components for the *Physical vs Paper Scarcity* page.

Each function takes already-loaded frames (`runs` = the wide run log,
`geo` = the tidy LME per-location breakdown) plus display units, and draws one
section with `st.*`.  All charts are hand-built Altair (no auto ``bind:"scales"``
param, matching ``app.py``) and rendered ``width="stretch"``.

Analytics live in ``scripts/analytics.py``; this module is presentation only.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from scripts.analytics import (
    arb_hurdle_frame,
    cancellation_concentration,
    diagnose_anomalies,
    loadout_response,
    location_warrant_flows,
    location_warrant_status,
    net_arb_margin,
    net_draw_rate,
)
from scripts.schema import build_daily_series, build_native_asof_series

_ON, _CANC = "#5b8def", "#e0a458"          # on-warrant / cancelled (app.py palette)
_TERM, _DRAW = "#8a7fc0", "#5aa469"        # term structure / draw rate
_SPREAD, _NET, _GREY = "#d1495b", "#2e86ab", "#9aa0a6"
_H = 320


def _date_axis(dates, **kw) -> alt.Axis:
    """A temporal axis with one labelled tick per real data date (no auto ticks
    at half-day / multi-day intervals, no hidden labels)."""
    vals = sorted({pd.Timestamp(d).normalize() for d in pd.to_datetime(list(dates))})
    return alt.Axis(values=[v.isoformat() for v in vals], format="%b %d",
                    labelAngle=-40, labelOverlap=False, **kw)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _native(runs: pd.DataFrame) -> pd.DataFrame:
    try:
        s = build_native_asof_series(runs)
    except Exception:  # pragma: no cover - defensive
        return pd.DataFrame()
    return s.set_index("date") if not s.empty and "date" in s.columns else pd.DataFrame()


def _dod(runs: pd.DataFrame, col: str) -> float | None:
    """Latest minus the previous *distinct* value. Inventory columns are read off
    the exchange-native as-of series (real report points only, so a stale leg
    can't inject a spike); price columns fall back to the run log."""
    nat = _native(runs)
    if col in getattr(nat, "columns", []):
        vals = pd.to_numeric(nat[col], errors="coerce").dropna()
    else:
        d = build_daily_series(runs)
        if col not in d.columns:
            return None
        vals = pd.to_numeric(d[col], errors="coerce").dropna()
    if vals.empty:
        return None
    cur = float(vals.iloc[-1])
    earlier = vals[vals != cur]
    prev = float(earlier.iloc[-1]) if not earlier.empty else cur
    return cur - prev


def _delta_qty(runs: pd.DataFrame, col: str, unit_div: float, suffix: str) -> str | None:
    d = _dod(runs, col)
    return None if d is None else f"{d / unit_div:+,.0f} {suffix}"


# --------------------------------------------------------------------------- #
# 1. Top KPI row
# --------------------------------------------------------------------------- #
def kpi_row(runs: pd.DataFrame, band, unit_div: float, unit_suffix: str) -> None:
    latest = runs.iloc[-1]

    c = st.columns(4)
    for col, (label, key) in zip(c, [
        ("Total Reported", "global_reported_stock_t"),
        ("On-Warrant", "global_on_warrant_t"),
        ("Cancelled", "global_cancelled_t"),
        ("Off-Warrant", "global_off_warrant_t"),
    ]):
        v = latest.get(key)
        col.metric(label, f"{v / unit_div:,.0f} {unit_suffix}" if pd.notna(v) else "—",
                   _delta_qty(runs, key, unit_div, unit_suffix))
    st.caption("Global inventories — sum across CME (COMEX), LME and SHFE, "
               "day-over-day change on real report points.")

    m = st.columns(2)
    cash3m = latest.get("lme_cash_3m_spread_usd_t")
    if pd.isna(cash3m) and pd.notna(latest.get("lme_copper_cash_usd_t")) \
            and pd.notna(latest.get("lme_copper_3m_usd_t")):
        cash3m = latest["lme_copper_cash_usd_t"] - latest["lme_copper_3m_usd_t"]
    if pd.notna(cash3m):
        m[0].metric("LME Cash–3M spread", f"{cash3m:+,.0f} USD/t",
                    "Backwardation" if cash3m > 0 else "Contango", delta_color="off")
    else:
        m[0].metric("LME Cash–3M spread", "—")

    cme, lme3 = latest.get("comex_copper_usd_t"), latest.get("lme_copper_3m_usd_t")
    if pd.notna(cme) and pd.notna(lme3):
        gross = latest.get("cme_lme_spread_3m_usd_t")
        gross = float(gross) if pd.notna(gross) else float(cme - lme3)
        nm = float(net_arb_margin(cme, lme3, band))
        state = "Arb Open" if nm > 0 else "Arb Closed"
        m[1].metric("CME–LME spread (3M)", f"{gross:+,.0f} USD/t",
                    f"{state} · net {nm:+,.0f} USD/t", delta_color="off")
        hurdle = band.transfer_cost(lme_3m_price_mt=float(lme3))
    else:
        m[1].metric("CME–LME spread (3M)", "—")
        hurdle = band.transfer_cost(lme_3m_price_mt=0.0)
    duty = f" + {band.tariff_pct:g}% duty" if band.tariff_pct else ""
    st.caption(f"Market temperature — physical cost hurdle **{hurdle:,.0f} USD/t** "
               f"(freight {band.freight_usd_mt:,.0f} + finance/insurance "
               f"{band.finance_insurance_usd_mt:,.0f}{duty}; adjust in the sidebar).")


# --------------------------------------------------------------------------- #
# 2. LME spatial concentration & warrant status
# --------------------------------------------------------------------------- #
def chart_spatial_concentration(geo: pd.DataFrame, unit_div: float, unit_suffix: str) -> None:
    ls = location_warrant_status(geo)
    if ls.empty:
        st.info("LME per-location breakdown builds from the next pipeline run "
                "(`data/lme_geo.parquet`).")
        return

    conc = cancellation_concentration(geo)
    if conc:
        cc = st.columns(2)
        cc[0].metric(f"Top location — {conc['top_location']}",
                     f"{conc['top_share_pct']:.0f}%",
                     f"of {conc['global_cancelled_t'] / unit_div:,.0f} {unit_suffix} "
                     "LME cancelled warrants", delta_color="off")
        cc[1].metric(f"Top hub — {conc['top_hub']}", f"{conc['top_hub_share_pct']:.0f}%",
                     "of LME cancelled warrants", delta_color="off")

    order = ls["location"].tolist()
    long = ls.melt(id_vars=["location", "region", "hub"],
                   value_vars=["on_warrant_t", "cancelled_t"],
                   var_name="status", value_name="t")
    long["t"] = long["t"] / unit_div
    long["status"] = long["status"].map({"on_warrant_t": "On-warrant", "cancelled_t": "Cancelled"})
    chart = alt.Chart(long).mark_bar().encode(
        y=alt.Y("location:N", sort=order, title=None,
                axis=alt.Axis(labelLimit=160, labelOverlap=False)),
        x=alt.X("t:Q", title=f"Warranted stock ({unit_suffix})", stack="zero"),
        color=alt.Color("status:N", title=None,
                        scale=alt.Scale(domain=["On-warrant", "Cancelled"], range=[_ON, _CANC]),
                        legend=alt.Legend(orient="bottom")),
        tooltip=["location:N", "region:N", "hub:N", "status:N",
                 alt.Tooltip("t:Q", title=f"Stock ({unit_suffix})", format=",.0f")],
    ).properties(height=max(240, 30 * len(order)))
    st.altair_chart(chart, width="stretch")

    on_sum = ls["on_warrant_t"].sum() / unit_div
    canc_sum = ls["cancelled_t"].sum() / unit_div
    rd = pd.to_datetime(ls["report_date"].iloc[0]).date()
    st.caption(
        f"LME stock-breakdown report of {rd} — {len(order)} delivery points holding stock "
        f"(hub in tooltip: Singapore / Rotterdam / Busan / Port Klang / US / Other). "
        f"Bars sum to LME on-warrant **{on_sum:,.0f} {unit_suffix}** + cancelled "
        f"**{canc_sum:,.0f} {unit_suffix}** — the LME leg only; the KPI row above is the "
        f"CME + LME + SHFE global."
    )


# --------------------------------------------------------------------------- #
# 3. Warrant dynamics vs physical load-out
# --------------------------------------------------------------------------- #
def chart_warrant_vs_loadout(geo: pd.DataFrame, unit_div: float, unit_suffix: str) -> None:
    flows = location_warrant_flows(geo, level="global")
    n = flows["report_date"].nunique() if not flows.empty else 0
    if n < 3:
        st.info(f"Needs ≥ 3 LME breakdown reports for the dual-axis time-series "
                f"(have {n}). Builds as the daily pipeline runs.")
        if not flows.empty:
            st.dataframe(
                flows[["report_date", "d_cancelled_t", "withdrawals_t"]].rename(columns={
                    "report_date": "Report", "d_cancelled_t": "Δ Cancelled (t)",
                    "withdrawals_t": "Delivered-Out (t)"}),
                width="stretch", hide_index=True)
        return

    f = flows.copy()
    f["date"] = pd.to_datetime(f["report_date"])
    f["dcanc"] = f["d_cancelled_t"] / unit_div
    f["dout"] = f["withdrawals_t"] / unit_div
    xx = alt.X("date:T", title=None, axis=_date_axis(f["date"]))
    bars = alt.Chart(f).mark_bar(color=_CANC, opacity=0.75).encode(
        x=xx, y=alt.Y("dcanc:Q", title=f"Δ Cancelled warrants ({unit_suffix})"),
        tooltip=["date:T", alt.Tooltip("dcanc:Q", title="Δ Cancelled", format="+,.0f")])
    line = alt.Chart(f).mark_line(color=_NET, point=True).encode(
        x=xx, y=alt.Y("dout:Q", title=f"Delivered-Out ({unit_suffix})"),
        tooltip=["date:T", alt.Tooltip("dout:Q", title="Delivered-Out", format=",.0f")])
    layers = [bars, line]

    unmet = loadout_response(geo, level="global", lag_bdays=10)
    flagged = unmet[~unmet["responded"]] if not unmet.empty else unmet
    if not flagged.empty:
        md = flagged.copy()
        md["date"] = pd.to_datetime(md["report_date"])
        tri = alt.Chart(md).mark_point(shape="triangle-up", size=150, filled=True,
                                       color=_SPREAD).encode(
            x="date:T", y=alt.value(10),
            tooltip=[alt.Tooltip("date:T", title="Cancellation spike"),
                     alt.Tooltip("new_cancellations_t:Q", title="Cancelled", format=",.0f"),
                     alt.Tooltip("unmet_t:Q", title="Not loaded out", format=",.0f")])
        layers.append(tri)

    st.altair_chart(alt.layer(*layers).resolve_scale(y="independent").properties(height=_H),
                    width="stretch")
    n_flag = 0 if unmet.empty else int((~unmet["responded"]).sum())
    st.caption("Bars = change in cancelled warrants (left axis); line = gross Delivered-Out "
               f"(right axis). ▲ = cancellation spike with < 50% load-out within 10 trading "
               f"days ({n_flag} flagged) → paper hold / re-warranting.")


# --------------------------------------------------------------------------- #
# 4. Term structure & arbitrage band
# --------------------------------------------------------------------------- #
def chart_term_structure_arb(runs: pd.DataFrame, band, unit_div: float, unit_suffix: str) -> None:
    d = build_daily_series(runs).rename(columns={"date": "when"})
    cash = pd.to_numeric(d.get("lme_cash_3m_spread_usd_t"), errors="coerce") \
        if "lme_cash_3m_spread_usd_t" in d.columns else None
    ndr = net_draw_rate(runs)

    st.markdown("**Term structure vs inventory draw rate**")
    if cash is not None and cash.notna().any():
        ts = pd.DataFrame({"when": pd.to_datetime(d["when"]), "spread": cash}).dropna()
        nd = pd.DataFrame(columns=["when", "rate"])
        if not ndr.empty:
            nd = ndr.reset_index()
            nd.columns = ["when", "rate"]
            nd["rate"] = nd["rate"] / unit_div
        xx = alt.X("when:T", title=None,
                   axis=_date_axis(pd.concat([ts["when"], nd["when"]], ignore_index=True)))
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=_GREY).encode(y="y:Q")
        sp = alt.Chart(ts).mark_line(color=_TERM, point=True).encode(
            x=xx, y=alt.Y("spread:Q", title="LME Cash–3M (USD/t)"),
            tooltip=["when:T", alt.Tooltip("spread:Q", format="+,.0f")])
        layers = [zero, sp]
        if not nd.empty:
            dr = alt.Chart(nd).mark_area(color=_DRAW, opacity=0.22,
                                         line={"color": _DRAW}).encode(
                x="when:T", y=alt.Y("rate:Q", title=f"Net draw rate ({unit_suffix}/bday)"),
                tooltip=["when:T", alt.Tooltip("rate:Q", format="+,.1f")])
            layers = [dr, *layers]
        st.altair_chart(
            alt.layer(*layers).resolve_scale(y="independent").properties(height=260),
            width="stretch")
        st.caption("Positive spread = backwardation (tight). Negative draw rate = inventory "
                   "falling. Backwardation *with* a falling inventory ⇒ genuine physical tightness.")
    else:
        st.info("LME Cash–3M spread history builds as the pipeline runs.")

    st.markdown("**CME–LME arbitrage vs physical cost hurdle**")
    arb = arb_hurdle_frame(runs, band=band)
    if arb.empty:
        st.info("No CME/LME price rows yet.")
        return
    a = arb.reset_index().rename(columns={"run_date": "when"})
    a["when"] = pd.to_datetime(a["when"])
    a["zero"] = 0.0
    xx = alt.X("when:T", title=None, axis=_date_axis(a["when"]))
    hurdle_band = alt.Chart(a).mark_area(color=_GREY, opacity=0.18).encode(
        x=xx, y=alt.Y("zero:Q", title="USD/t"), y2="transfer_cost_usd_mt:Q",
        tooltip=[alt.Tooltip("transfer_cost_usd_mt:Q", title="Cost hurdle", format=",.0f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=_GREY).encode(y="y:Q")
    net = alt.Chart(a).mark_line(color=_NET, strokeDash=[4, 3]).encode(
        x=xx, y="net_arb_margin_usd_mt:Q",
        tooltip=["when:T", alt.Tooltip("net_arb_margin_usd_mt:Q", title="Net of hurdle",
                                       format="+,.0f")])
    gross = alt.Chart(a).mark_line(color=_SPREAD, point=True).encode(
        x=xx, y="gross_spread_usd_mt:Q",
        tooltip=["when:T", alt.Tooltip("gross_spread_usd_mt:Q", title="CME–LME 3M",
                                       format="+,.0f"), "regime:N"])
    st.altair_chart((hurdle_band + zero + net + gross).properties(height=300), width="stretch")
    last = arb.iloc[-1]
    st.caption(f"Grey band = physical transfer cost (0 → {last['transfer_cost_usd_mt']:,.0f} "
               "USD/t, adjust in the sidebar). Red = CME–LME 3M spread; blue dashed = spread "
               f"net of the hurdle. Latest regime: **{last['regime']}** "
               f"(net {last['net_arb_margin_usd_mt']:+,.0f} USD/t).")


# --------------------------------------------------------------------------- #
# 5. Anomaly & diagnostics table
# --------------------------------------------------------------------------- #
def anomaly_table(runs: pd.DataFrame, geo: pd.DataFrame, band) -> None:
    diag = diagnose_anomalies(runs, geo, band=band)
    if diag.empty:
        st.info("No |Z| > 2.0 alerts on cancellations or load-outs, and no re-warranting "
                "events. Per-location Z-scores need ~30 days of breakdown history — this "
                "populates as the daily pipeline runs.")
        return
    show = diag.rename(columns={
        "location": "Location", "hub": "Hub", "region": "Region",
        "cancel_z": "Cancel Z", "loadout_z": "Load-out Z",
        "rewarrant_events": "Re-warrant events", "interpretation": "Interpretation"})

    def _hot(s: pd.Series) -> list[str]:
        return ["background-color:#fdecea" if pd.notna(v) and abs(v) > 2.0 else "" for v in s]

    st.dataframe(
        show.style.format({"Cancel Z": "{:.2f}", "Load-out Z": "{:.2f}"})
        .apply(_hot, subset=["Cancel Z", "Load-out Z"]),
        width="stretch", hide_index=True)
    st.caption("|Z| > 2.0 vs each location's rolling 30/90-day history. The interpretation "
               "tag is heuristic (`scripts/analytics._interpret_anomaly`): isolated vs broad "
               "cancellations, active load-out, transpacific arb-delivery candidate, or "
               "re-warranting / paper hold.")
