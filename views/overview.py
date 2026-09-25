"""Render components for the Overview page — the CME/LME/SHFE inventory
picture and the CME-LME price spread.

Mirrors the shape of ``views/scarcity.py``: pure(ish) functions over
already-loaded frames, drawing with ``st.*``. Colors and the shared
date-axis helper come from ``views/theme.py``. Sidebar controls, the view
(Same-Day Synced / Exchange-Native / Pipeline run date) series selection, and
section ordering live in ``views/overview_page.py`` — the actual page script.
"""

from __future__ import annotations

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from scripts.schema import EXCHANGE_DATE_COL, build_daily_series, build_native_asof_series
from views.common import fmt
from views.theme import BUCKET_COLORS, BUCKET_TINTS, EXCHANGE_COLORS, date_axis

BUCKETS = ["on_warrant", "cancelled", "off_warrant"]
BUCKET_LABELS = {"on_warrant": "On-warrant", "cancelled": "Cancelled", "off_warrant": "Off-warrant"}
EXCHANGES = ["cme", "lme", "shfe"]
EXCHANGE_LABELS = {"cme": "CME (COMEX)", "lme": "LME", "shfe": "SHFE"}

_BUCKET_COLS = [
    ("On-warrant", "on_warrant_t"), ("Cancelled", "cancelled_t"),
    ("Off-warrant", "off_warrant_t"), ("Reported stock", "total_t"),
]


def exchange_as_of(runs: pd.DataFrame, exchange: str) -> object | None:
    """The exchange's own report-as-of date on the latest pipeline run."""
    v = runs.iloc[-1].get(EXCHANGE_DATE_COL[exchange])
    return pd.to_datetime(v).date() if pd.notna(v) else None


def change_points(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows where a value actually changed (a source published),
    keeping the datetime index."""
    changed = df.fillna(-1.0).ne(df.fillna(-1.0).shift()).any(axis=1)
    return df.loc[changed]


def stale_banner(fresh: dict) -> None:
    """The '⚠️ carried forward' warning line from ``schema.staleness(runs)``."""
    from scripts.schema import STALE_LABEL

    msgs = [
        f"**{STALE_LABEL.get(feed, feed)}** carried forward — "
        f"{info['bdays_stale']} business days stale (as of {info['as_of']})"
        for feed, info in fresh.items() if info["stale"]
    ]
    if msgs:
        st.warning("⚠️ " + "  ·  ".join(msgs))


# --------------------------------------------------------------------------- #
# Day-over-day: latest minus the previous *distinct* value on the
# exchange-native series (real report points only), so a carried-forward /
# stale leg can never inject a spike. Price columns aren't in the native
# series, so fall back to the run-date daily series for those.
# --------------------------------------------------------------------------- #
def _native_series(runs: pd.DataFrame) -> pd.DataFrame:
    try:
        s = build_native_asof_series(runs)
    except Exception:  # pragma: no cover - defensive
        return pd.DataFrame()
    return s.set_index("date") if not s.empty and "date" in s.columns else pd.DataFrame()


def _dod(runs: pd.DataFrame, col: str):
    """(delta, pct, as_of) for `col`'s latest vs. previous *distinct* value.
    `as_of` is the report date the *current* value is from -- each KPI's delta
    can come from a different underlying report-date transition than its
    neighbours (they're each "this column's own most recent change", not "the
    same reference date for every column"), so it's shown per metric rather
    than assumed to line up across the row."""
    nat = _native_series(runs)
    if col in getattr(nat, "columns", []):
        vals = pd.to_numeric(nat[col], errors="coerce").dropna()
    else:
        d = build_daily_series(runs)
        if col not in d.columns or "date" not in d.columns:
            return None, None, None
        vals = pd.to_numeric(d.set_index("date")[col], errors="coerce").dropna()
    if vals.empty:
        return None, None, None
    cur = float(vals.iloc[-1])
    earlier = vals[vals != cur]
    prv = float(earlier.iloc[-1]) if not earlier.empty else cur  # flat window -> 0 change
    delta = cur - prv
    if delta == 0:
        pct = 0.0
    elif prv != 0:
        pct = delta / prv * 100.0
    else:
        pct = None
    return delta, pct, vals.index[-1].date()


def _delta_str(runs: pd.DataFrame, col: str, unit_div: float, unit_suffix: str) -> str | None:
    d, pct, _asof = _dod(runs, col)
    if d is None:
        return None
    return f"{d / unit_div:+,.0f} {unit_suffix}" + (f"  ({pct:+.1f}%)" if pct is not None else "")


def _dod_cell(runs: pd.DataFrame, col: str) -> str:
    """DoD change of `col` in tonnes with the % change in brackets (table cell)."""
    d, pct, _asof = _dod(runs, col)
    if d is None:
        return "—"
    return f"{d:+,.0f} t" + (f"  ({pct:+.1f}%)" if pct is not None else "")


# --------------------------------------------------------------------------- #
# 1. KPI row
# --------------------------------------------------------------------------- #
def kpi_row(runs: pd.DataFrame, unit_div: float, unit_suffix: str) -> None:
    latest = runs.iloc[-1]

    def _metric(col_container, label: str, col: str) -> None:
        _delta, _pct, asof = _dod(runs, col)
        col_container.metric(label, fmt(latest.get(col), unit_div, unit_suffix),
                             _delta_str(runs, col, unit_div, unit_suffix))
        col_container.caption(f"as of {asof}" if asof else "as of —")

    k = st.columns(5)
    _metric(k[0], "Global reported stock", "global_reported_stock_t")
    _metric(k[1], "Grand total (incl. off-warrant)", "global_total_t")
    for col, b in zip(k[2:], BUCKETS):
        _metric(col, f"Global {BUCKET_LABELS[b].lower()}", f"global_{b}_t")
    st.caption(
        "**Reported stock** = each exchange's headline published figure — CME "
        "*Registered + Eligible*, LME *on-warrant + cancelled* (closing warrants), "
        "SHFE *库存*. LME **off-warrant** (OWSR) is a separate T+3 report and is "
        "**not** in any exchange's headline, so it is added on top for the "
        "**grand total**. (CME's *Eligible* is COMEX off-warrant metal and is "
        "already inside `cme_total`, so `global_off_warrant` mixes CME Eligible + "
        "LME OWSR.) Each metric's **as of** date is its own most recent report-to-"
        "report change — they can differ across the row, so deltas don't "
        "necessarily add up across metrics the way the headline totals do."
    )


# --------------------------------------------------------------------------- #
# 2. Global composition & total-by-exchange charts
# --------------------------------------------------------------------------- #
def composition_chart(s: pd.DataFrame, unit_div: float, unit_suffix: str) -> alt.Chart:
    """Spaced stacked bars — one bar per date the picture changed, so individual
    report dates are legible (an area/step chart smeared them together)."""
    cols = [f"global_{b}_t" for b in BUCKETS]
    long = (
        (s[cols] / unit_div)
        .rename(columns={f"global_{b}_t": BUCKET_LABELS[b] for b in BUCKETS})
        .reset_index().rename(columns={"when": "date"})
        .melt("date", var_name="bucket", value_name="value")
        .dropna(subset=["value"])
    )
    long["day"] = pd.to_datetime(long["date"]).dt.strftime("%Y-%m-%d")
    order = sorted(long["day"].unique())
    return alt.Chart(long).mark_bar().encode(
        x=alt.X("day:O", sort=order, title=None,
                axis=alt.Axis(labelAngle=-40, labelOverlap=False),
                scale=alt.Scale(paddingInner=0.35)),
        y=alt.Y("value:Q", title=unit_suffix, stack="zero"),
        color=alt.Color("bucket:N", title=None,
                        scale=alt.Scale(domain=list(BUCKET_COLORS), range=list(BUCKET_COLORS.values())),
                        legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("day:O", title="date"), "bucket:N",
                 alt.Tooltip("value:Q", format=",.0f")],
    ).properties(height=340)


def by_exchange_chart(s: pd.DataFrame, show_exchanges: list[str],
                      unit_div: float, unit_suffix: str):
    """One mini panel per exchange with its **own** y-scale — CME (~700 kt) would
    otherwise flatten LME (~240 kt) and SHFE (~60 kt) into motionless lines."""
    exs = show_exchanges or EXCHANGES
    frames = []
    for e in exs:
        col = f"{e}_total_t"
        if col not in s.columns:
            continue
        f = (s[[col]] / unit_div).reset_index().rename(columns={"when": "date", col: "value"})
        f["exchange"] = EXCHANGE_LABELS[e]
        f["stale"] = s[f"{e}_stale"].values if f"{e}_stale" in s.columns else False
        frames.append(f)
    long = pd.concat(frames, ignore_index=True).dropna(subset=["value"]) if frames else pd.DataFrame()
    if long.empty:
        return alt.Chart(pd.DataFrame({"x": []})).mark_point()
    long["date"] = pd.to_datetime(long["date"])
    base = alt.Chart(long).mark_line(
        interpolate="step-after", point=alt.OverlayMarkDef(filled=True, size=28)
    ).encode(
        x=alt.X("date:T", title=None, axis=date_axis(long["date"])),
        y=alt.Y("value:Q", title=unit_suffix, scale=alt.Scale(zero=False)),
        color=alt.Color("exchange:N", title=None,
                        scale=alt.Scale(domain=list(EXCHANGE_COLORS), range=list(EXCHANGE_COLORS.values())),
                        legend=None),
        strokeDash=alt.StrokeDash("stale:N", legend=None,
                                  scale=alt.Scale(domain=[False, True], range=[[1, 0], [4, 3]])),
        tooltip=["date:T", "exchange:N", alt.Tooltip("value:Q", format=",.0f")],
    )
    return base.properties(height=104).facet(
        row=alt.Row("exchange:N", title=None,
                    sort=[EXCHANGE_LABELS[e] for e in EXCHANGES],
                    header=alt.Header(labelAngle=0, labelAlign="left", labelFontWeight="bold")),
    ).resolve_scale(y="independent")


# --------------------------------------------------------------------------- #
# 3. Breakdown & day-over-day table
# --------------------------------------------------------------------------- #
_CENTER = "center !important"
_TH_STYLE = [
    ("text-align", _CENTER), ("vertical-align", "middle"), ("font-weight", "700"),
    ("color", "#1f2430"), ("background-color", "#e7e9ef"),
    ("border-bottom", "2px solid #b3bac7"), ("padding", "8px 12px"), ("font-size", "0.9rem"),
]


def _styled_table(df: pd.DataFrame, groups: list[tuple[str, list[str]]], num_fmt: dict) -> object:
    sty = (
        df.style.format(num_fmt, na_rep="—")
        .set_properties(**{"text-align": _CENTER, "vertical-align": "middle",
                           "color": "#1f2430", "padding": "7px 12px"})
    )
    tstyles = [
        {"selector": "th, td", "props": [("text-align", _CENTER), ("vertical-align", "middle")]},
        {"selector": "th", "props": _TH_STYLE},
        {"selector": "th.row_heading, th.blank", "props": [("background-color", "#e7e9ef")]},
        {"selector": "caption", "props": [("text-align", _CENTER)]},
        {"selector": "table", "props": [("border-collapse", "collapse"), ("width", "100%"),
                                        ("margin", "0 auto")]},
    ]
    for label, cols in groups:
        cell_bg, head_bg = BUCKET_TINTS[label]
        for c in cols:
            if c not in df.columns:
                continue
            j = df.columns.get_loc(c)
            sty = sty.set_properties(subset=[c], **{"background-color": cell_bg})
            tstyles.append({"selector": f"th.col_heading.col{j}", "props": [("background-color", head_bg)]})
    return sty.set_table_styles(tstyles)


def breakdown_table(runs: pd.DataFrame, unit_div: float, unit_suffix: str) -> None:
    latest = runs.iloc[-1]
    rows = []
    for e in EXCHANGES:
        row: dict[str, object] = {"Exchange": EXCHANGE_LABELS[e]}
        for mlabel, msuf in _BUCKET_COLS:
            col = f"{e}_{msuf}"
            cur = latest.get(col)
            row[f"{mlabel} ({unit_suffix})"] = float(cur) / unit_div if pd.notna(cur) else None
            row[f"{mlabel} Δ"] = _dod_cell(runs, col)
        row["As of"] = str(exchange_as_of(runs, e) or "—")
        rows.append(row)

    bt = pd.DataFrame(rows).set_index("Exchange")
    groups = [(m, [f"{m} ({unit_suffix})", f"{m} Δ"]) for m, _ in _BUCKET_COLS]
    st.table(_styled_table(bt, groups, {f"{m} ({unit_suffix})": "{:,.0f}" for m, _ in _BUCKET_COLS}))


# --------------------------------------------------------------------------- #
# 4. Copper price spreads
# --------------------------------------------------------------------------- #
def price_spread_section(runs: pd.DataFrame, spread_hist: pd.DataFrame) -> None:
    latest = runs.iloc[-1]

    # One row per **market session** — a real COMEX settlement date joined with
    # the LME cash / 3-month from that same session. Prefer the dedicated
    # multi-week history (scripts/backfill_prices.py); fall back to whatever
    # distinct sessions the run log captured.
    if not spread_hist.empty:
        h = spread_hist.rename(columns={
            "session_date": "session", "comex_usd_t": "comex_copper_usd_t",
            "lme_3m_usd_t": "lme_copper_3m_usd_t"}).copy()
        src_label = f"{h['comex_source'].iloc[-1]} (COMEX) + Westmetall (LME)"
    else:
        keep = ["cme_lme_spread_3m_usd_t", "lme_cash_3m_spread_usd_t",
                "comex_copper_usd_t", "lme_copper_3m_usd_t", "comex_contract", "lme_price_date"]
        h = (runs[["run_date", "comex_price_date", *[c for c in keep if c in runs.columns]]]
             .dropna(subset=["comex_price_date"]).copy())
        h["session"] = pd.to_datetime(h["comex_price_date"])
        h = h.sort_values("run_date").drop_duplicates("session", keep="last")
        src_label = "CME Group (COMEX) + Westmetall (LME)"

    h = h.sort_values("session").reset_index(drop=True)
    if h.empty or pd.to_numeric(h.get("cme_lme_spread_3m_usd_t"), errors="coerce").dropna().empty:
        st.info("No price data yet — populates from the next pipeline run.")
        return

    last = h.iloc[-1]
    contract = last.get("comex_contract") or latest.get("comex_contract") or "front"

    def session_delta(col: str) -> str | None:
        """Session-over-session change: latest minus the previous *distinct* value."""
        v = pd.to_numeric(h.get(col), errors="coerce").dropna()
        if v.empty:
            return None
        cur = float(v.iloc[-1])
        earlier = v[v != cur]
        return f"{cur - (float(earlier.iloc[-1]) if not earlier.empty else cur):+,.0f} USD/t"

    lme_asof_col = "lme_price_date" if "lme_price_date" in h.columns else "session"

    def session_asof(col: str) -> str:
        v = last.get(col)
        return f"as of {pd.to_datetime(v).date()}" if pd.notna(v) else "as of —"

    p = st.columns(4)
    p[0].metric("CME − LME (3-month)", f"{last['cme_lme_spread_3m_usd_t']:+,.0f} USD/t",
                session_delta("cme_lme_spread_3m_usd_t"))
    p[0].caption(session_asof("session"))
    c3 = last.get("lme_cash_3m_spread_usd_t")
    p[1].metric("LME cash − 3-month", f"{c3:+,.0f} USD/t" if pd.notna(c3) else "—",
                session_delta("lme_cash_3m_spread_usd_t"))
    p[1].caption(session_asof(lme_asof_col))
    p[2].metric(f"CME price ({contract})",
                f"{last['comex_copper_usd_t']:,.0f} USD/t", session_delta("comex_copper_usd_t"))
    p[2].caption(session_asof("session"))
    p[3].metric("LME price (3-month)",
                f"{last['lme_copper_3m_usd_t']:,.0f} USD/t", session_delta("lme_copper_3m_usd_t"))
    p[3].caption(session_asof(lme_asof_col))

    sess = pd.to_datetime(last["session"])
    behind = len(pd.bdate_range(sess, pd.Timestamp(pd.Timestamp.today().date()))) - 1
    if behind >= 2:
        st.warning(f"⚠️ Latest CME−LME session is **{sess.date()}** — {behind} business "
                   "days back. The COMEX settlement feed has not served a fresher date; "
                   "each point is still a correct market-on-close spread for its own session.")
    st.caption(
        f"Per market session (COMEX settlement date = LME as-of date). **CME − LME 3M** = "
        f"most-active COMEX month ({contract}) settle − LME 3-month; **LME cash − 3M** = "
        f"LME term structure (positive = backwardation). {len(h)} session(s). "
        f"Sources: {src_label}."
    )

    long = (h.rename(columns={"cme_lme_spread_3m_usd_t": "CME − LME 3M",
                              "lme_cash_3m_spread_usd_t": "LME cash − 3M"})
            .melt("session", value_vars=["CME − LME 3M", "LME cash − 3M"],
                  var_name="spread", value_name="usd_t").dropna(subset=["usd_t"]))
    line = alt.Chart(long).mark_line(point=True).encode(
        x=alt.X("session:T", title=None, axis=date_axis(long["session"])),
        y=alt.Y("usd_t:Q", title="USD/t"),
        color=alt.Color("spread:N", title=None, legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("session:T", title="session"), "spread:N",
                 alt.Tooltip("usd_t:Q", format="+,.0f")],
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#9aa0a6").encode(y="y:Q")
    st.altair_chart((zero + line).properties(height=300), width="stretch")


# --------------------------------------------------------------------------- #
# 5. LME by location
# --------------------------------------------------------------------------- #
def geo_section(geo: pd.DataFrame, unit_div: float, unit_suffix: str) -> None:
    if geo.empty:
        st.info("Geo breakdown builds from the next pipeline run (`data/lme_geo.parquet`).")
        return

    bd = geo[geo["report_type"] == "breakdown"]
    if not bd.empty:
        rd = bd["report_date"].max()
        latest_bd = bd[bd["report_date"] == rd].copy()
        st.caption(f"LME stock-breakdown — report of **{rd.date()}** "
                   f"({len(latest_bd)} delivery points)")
        gcols = ["location", "region", "on_warrant_t", "cancelled_t",
                 "delivered_in_t", "delivered_out_t", "closing_t"]
        disp = latest_bd[gcols].rename(columns={
            "location": "Location", "region": "Country",
            "on_warrant_t": f"On-warrant ({unit_suffix})",
            "cancelled_t": f"Cancelled ({unit_suffix})",
            "delivered_in_t": f"Delivered-in ({unit_suffix})",
            "delivered_out_t": f"Delivered-out ({unit_suffix})",
            "closing_t": f"Closing ({unit_suffix})",
        })
        num = [c for c in disp.columns if c.endswith(f"({unit_suffix})")]
        disp[num] = disp[num] / unit_div
        disp = disp.sort_values(f"Closing ({unit_suffix})", ascending=False)
        gl, gr = st.columns([3, 2])
        gl.dataframe(disp.style.format({c: "{:,.0f}" for c in num}, na_rep="—"),
                     width="stretch", hide_index=True)
        top = (latest_bd[latest_bd["on_warrant_t"] > 0]
               .nlargest(15, "on_warrant_t")[["location", "region", "on_warrant_t"]].copy())
        top["on_warrant"] = top["on_warrant_t"] / unit_div
        gr.altair_chart(
            alt.Chart(top).mark_bar(color=BUCKET_COLORS["On-warrant"]).encode(
                x=alt.X("on_warrant:Q", title=f"On-warrant ({unit_suffix})"),
                y=alt.Y("location:N", sort="-x", title=None,
                        axis=alt.Axis(labelOverlap=False, labelLimit=150)),
                tooltip=["location:N", "region:N", alt.Tooltip("on_warrant:Q", format=",.0f")],
            ).properties(height=max(220, 26 * len(top))),
            width="stretch",
        )

    ow = geo[geo["report_type"] == "owsr"]
    if not ow.empty:
        ord_ = ow["report_date"].max()
        latest_ow = ow[ow["report_date"] == ord_]
        regs = latest_ow[latest_ow["region"] != "GLOBAL"]
        glob = latest_ow[latest_ow["region"] == "GLOBAL"]["off_warrant_t"]
        st.caption(f"LME off-warrant (OWSR) by region — **{ord_.date()}** · "
                   f"global {fmt(float(glob.iloc[0]) if not glob.empty else None, unit_div, unit_suffix)}")
        regs = regs[["region", "off_warrant_t"]].copy()
        regs["off_warrant"] = regs["off_warrant_t"] / unit_div
        st.altair_chart(
            alt.Chart(regs).mark_bar(color=BUCKET_COLORS["Off-warrant"]).encode(
                x=alt.X("region:N", sort="-y", title=None),
                y=alt.Y("off_warrant:Q", title=f"Off-warrant ({unit_suffix})"),
                tooltip=["region:N", alt.Tooltip("off_warrant:Q", format=",.0f")],
            ).properties(height=220),
            width="stretch",
        )


# --------------------------------------------------------------------------- #
# 6. Data health & sources
# --------------------------------------------------------------------------- #
def data_health_section(runs: pd.DataFrame, data_path: Path) -> None:
    with st.expander("Data health & raw run log"):
        health_cols = [
            "run_date", "sources_ok", "sources_failed",
            "cme_stale", "lme_stale", "lme_offwarrant_stale", "shfe_stale", "price_stale",
            "global_total_t", "cme_lme_spread_usd_t", "notes",
        ]
        health_cols = [c for c in health_cols if c in runs.columns]
        log = runs[health_cols].sort_values("run_date", ascending=False).head(30).copy()
        log["run_date"] = log["run_date"].dt.date
        st.dataframe(log, width="stretch", hide_index=True)
        st.caption(f"{len(runs)} pipeline runs on record · parquet: `{data_path.name}`")


def sources_section() -> None:
    with st.expander("Sources"):
        st.markdown(
            """
**CME (COMEX)**
- [Warehouse & Depository Stocks (Registrar Reports)](https://www.cmegroup.com/clearing/operations-and-deliveries/registrar-reports.html) — hosts the copper stocks report (Registered / Eligible)
- [Copper futures settlements](https://www.cmegroup.com/markets/metals/base/copper.settlements.html) — most-active month settle (price leg)
- [NYMEX & COMEX Delivery Notices & Stocks](https://www.cmegroup.com/solutions/clearing/operations-and-deliveries/nymex-delivery-notices.html)

**LME**
- [Stock breakdown report](https://www.lme.com/market-data/reports-and-data/warehouse-and-stocks-reports/stock-breakdown-report?page=1&DateFacet=Last+7+days) — live + cancelled warrants
- [Off-warrant stock reporting](https://www.lme.com/market-data/reports-and-data/warehouse-and-stocks-reports/off-warrant-stock-reporting?page=1&DateFacet=Last+7+days)
- [Westmetall market data](https://www.westmetall.com/en/markdaten.php) — LME Copper cash-settlement + 3-month price + closing stock (the price leg, and the preferred source for `lme_total_t`)

**SHFE**
- [Weekly stock report / 库存周报](https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/?query_params=weeklystock)
- [Daily warehouse-warrant report / 仓单日报](https://www.shfe.com.cn/eng/reports/StatisticalData/DailyData/?query_params=dailystock)

**Prices (fallback)**
- [Barchart](https://www.barchart.com/futures/quotes/HG*0/futures-prices) — preferred COMEX source when reachable
- [Yahoo Finance — COMEX copper HG=F](https://finance.yahoo.com/quote/HG=F) — last-resort fallback
            """
        )
