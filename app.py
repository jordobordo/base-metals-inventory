"""
STEP 5 — Streamlit dashboard.

Reads ``data/copper_inventory.parquet`` (committed to the repo by the daily
GitHub Action) and shows the global copper warehouse-inventory picture across
LME, CME (COMEX) and SHFE, harmonised to metric tonnes.

Deploy: point Streamlit Community Cloud at this repo, main file ``app.py``,
Python 3.11. No secrets. It redeploys whenever the Action commits a new parquet.

Local:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from scripts.schema import (  # noqa: E402
    EXCHANGE_DATE_COL,
    build_asof_series,
    build_daily_series,
)

DATA_PATH = Path(__file__).parent / "data" / "copper_inventory.parquet"

BUCKETS = ["on_warrant", "cancelled", "off_warrant"]
BUCKET_LABELS = {"on_warrant": "On-warrant", "cancelled": "Cancelled", "off_warrant": "Off-warrant"}
EXCHANGES = ["cme", "lme", "shfe"]
EXCHANGE_LABELS = {"cme": "CME (COMEX)", "lme": "LME", "shfe": "SHFE"}

st.set_page_config(
    page_title="Global Copper Warehouse Inventory",
    page_icon="🟠",
    layout="wide",
)


def _parquet_token() -> float:
    try:
        return DATA_PATH.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(ttl=300)
def load_runs(token: float) -> pd.DataFrame:
    # `token` = parquet mtime (hashed into the cache key) — a new value busts the
    # cache the moment the file changes, without waiting for the TTL.
    _ = token
    if not DATA_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(DATA_PATH)
    df["run_date"] = pd.to_datetime(df["run_date"])
    return df.sort_values("run_date").reset_index(drop=True)


def fmt(value: float | None, unit_div: float, suffix: str) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value / unit_div:,.0f} {suffix}"


runs = load_runs(_parquet_token())

st.title("🟠 Global Copper Warehouse Inventory")

if runs.empty:
    st.info(
        "No data yet. Run `python scripts/aggregate.py` locally, or wait for the "
        "daily GitHub Action to commit the first `data/copper_inventory.parquet`."
    )
    st.stop()

# --------------------------------------------------------------------------- #
# Sidebar controls
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Display")
    unit = st.radio("Units", ["kilotonnes", "tonnes"], index=0)
    unit_div, unit_suffix = (1000.0, "kt") if unit == "kilotonnes" else (1.0, "t")

    timeline = st.radio(
        "Timeline",
        ["Report as-of date", "Pipeline run date"],
        index=0,
        help="As-of date: plot each exchange at the date its report is *for* "
        "(CME ~T+1, LME ~T+2, SHFE = the report Friday) — so today's run already "
        "spans ~a week. Run date: the day the pipeline fetched it. Both forward-fill "
        "missing days (LOCF).",
    )
    show_exchanges = st.multiselect(
        "Exchanges",
        options=EXCHANGES,
        default=EXCHANGES,
        format_func=lambda e: EXCHANGE_LABELS[e],
    )

if timeline == "Report as-of date":
    series = build_asof_series(runs).rename(columns={"date": "when"})
if timeline != "Report as-of date" or series.empty or "when" not in series:
    series = build_daily_series(runs).rename(columns={"date": "when"})
series = series.set_index("when")

date_min, date_max = series.index.min().date(), series.index.max().date()
with st.sidebar:
    if date_min < date_max:
        start, end = st.slider(
            "Date range",
            min_value=date_min, max_value=date_max,
            value=(date_min, date_max), format="YYYY-MM-DD",
        )
        series = series.loc[str(start):str(end)]

latest = runs.iloc[-1]
prev = runs.iloc[-2] if len(runs) > 1 else None

# Business-day, forward-filled series — used for day-over-day so "T-1" is always
# the previous business day, regardless of gaps between pipeline-run rows.
_dod_series = build_asof_series(runs).set_index("date")


def _asof(e: str):
    v = latest.get(EXCHANGE_DATE_COL[e])
    return pd.to_datetime(v).date() if pd.notna(v) else None


def dod(col: str) -> tuple[float | None, float | None]:
    """Absolute + % change of `col`: latest date vs T-1 (previous business day).

    Uses the forward-filled as-of series; falls back to the last two run rows for
    columns it doesn't carry (e.g. the price legs)."""
    if col in _dod_series.columns and len(_dod_series) >= 2:
        cur, prv = _dod_series[col].iloc[-1], _dod_series[col].iloc[-2]
    else:
        cur = latest.get(col)
        prv = prev.get(col) if prev is not None else None
    if prv is None or pd.isna(cur) or pd.isna(prv):
        return None, None
    d = float(cur) - float(prv)
    if d == 0:
        pct = 0.0                              # unchanged -> +0.0%, even from a zero base
    elif float(prv) != 0:
        pct = d / float(prv) * 100.0
    else:
        pct = None                            # moved off a zero base -> % undefined
    return d, pct


def delta_str(col: str) -> str | None:
    d, pct = dod(col)
    if d is None:
        return None
    return f"{d / unit_div:+,.0f} {unit_suffix}" + (f"  ({pct:+.1f}%)" if pct is not None else "")


as_of = " · ".join(f"{EXCHANGE_LABELS[e]}: {_asof(e) or 'n/a'}" for e in EXCHANGES)
st.caption(f"Last pipeline run **{latest['run_date'].date()}** — data as of: {as_of}")

# --------------------------------------------------------------------------- #
# KPI row
# --------------------------------------------------------------------------- #
k = st.columns(4)
k[0].metric("Global reported stock",
            fmt(latest.get("global_reported_stock_t"), unit_div, unit_suffix),
            delta_str("global_reported_stock_t"))
for col, b in zip(k[1:], BUCKETS):
    col.metric(f"Global {BUCKET_LABELS[b].lower()}",
               fmt(latest.get(f"global_{b}_t"), unit_div, unit_suffix),
               delta_str(f"global_{b}_t"))

st.divider()

# --------------------------------------------------------------------------- #
# Charts — one stacked bar per date the picture actually changed (a source
# published), not one per forward-filled day. String-dated x-axis = no time,
# equal-width bars, no weekend gaps (series is already business-day).
# --------------------------------------------------------------------------- #
def change_point_bars(cols_df: pd.DataFrame) -> pd.DataFrame:
    changed = cols_df.fillna(-1.0).ne(cols_df.fillna(-1.0).shift()).any(axis=1)
    out = cols_df.loc[changed].copy()
    out.index = out.index.strftime("%Y-%m-%d")
    return out


x_label = "report as-of date" if timeline == "Report as-of date" else "pipeline run date"
left, right = st.columns(2)

with left:
    st.subheader("Global composition by date")
    comp = series[[f"global_{b}_t" for b in BUCKETS]] / unit_div
    comp.columns = [BUCKET_LABELS[b] for b in BUCKETS]
    st.bar_chart(
        change_point_bars(comp), height=340, stack=True,
        y_label=unit_suffix, x_label=x_label,
    )

with right:
    st.subheader("Total by exchange")
    cols = [f"{e}_total_t" for e in show_exchanges] or [f"{e}_total_t" for e in EXCHANGES]
    byx = series[cols] / unit_div
    byx.columns = [EXCHANGE_LABELS[c.split("_")[0]] for c in cols]
    st.bar_chart(
        change_point_bars(byx), height=340, stack=True,
        y_label=unit_suffix, x_label=x_label,
    )

st.subheader("Breakdown & day-over-day change")

_BUCKET_COLS = [
    ("On-warrant", "on_warrant_t"),
    ("Cancelled", "cancelled_t"),
    ("Off-warrant", "off_warrant_t"),
    ("Reported stock", "total_t"),
]


def _dod_cell(col: str) -> str:
    """DoD change of `col` in tonnes with the % change in brackets (table cell)."""
    d, pct = dod(col)
    if d is None:
        return "—"
    return f"{d:+,.0f} t" + (f"  ({pct:+.1f}%)" if pct is not None else "")


def usd_delta(col: str) -> str | None:
    """st.metric-style delta string in USD/t (None when there's no prior run)."""
    d, pct = dod(col)
    if d is None:
        return None
    return f"{d:+,.0f} USD/t" + (f"  ({pct:+.1f}%)" if pct is not None else "")


# gentle tints per stock type: (cell background, header background)
_TINT = {
    "On-warrant": ("#eef4fb", "#d6e5f6"),
    "Cancelled": ("#fdf3e8", "#f6e2c8"),
    "Off-warrant": ("#eef6ee", "#d8ecd8"),
    "Reported stock": ("#f3f1f9", "#e1dbf1"),
    "_plain": ("#f1f3f6", "#dfe3ea"),
}
_CENTER = "center !important"
_TH_STYLE = [
    ("text-align", _CENTER), ("vertical-align", "middle"), ("font-weight", "700"),
    ("color", "#1f2430"), ("background-color", "#e7e9ef"),
    ("border-bottom", "2px solid #b3bac7"), ("padding", "8px 12px"), ("font-size", "0.9rem"),
]


def styled_table(df: pd.DataFrame, groups: list[tuple[str, list[str]]], num_fmt: dict) -> object:
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
        cell_bg, head_bg = _TINT[label]
        for c in cols:
            if c not in df.columns:
                continue
            j = df.columns.get_loc(c)
            sty = sty.set_properties(subset=[c], **{"background-color": cell_bg})
            tstyles.append({"selector": f"th.col_heading.col{j}", "props": [("background-color", head_bg)]})
    return sty.set_table_styles(tstyles)


rows = []
for e in EXCHANGES:
    row: dict[str, object] = {"Exchange": EXCHANGE_LABELS[e]}
    for mlabel, msuf in _BUCKET_COLS:
        col = f"{e}_{msuf}"
        cur = latest.get(col)
        row[f"{mlabel} ({unit_suffix})"] = float(cur) / unit_div if pd.notna(cur) else None
        row[f"{mlabel} Δ"] = _dod_cell(col)
    row["As of"] = str(_asof(e) or "—")
    rows.append(row)

bt = pd.DataFrame(rows).set_index("Exchange")
_bt_groups = [(m, [f"{m} ({unit_suffix})", f"{m} Δ"]) for m, _ in _BUCKET_COLS]
st.table(styled_table(bt, _bt_groups, {f"{m} ({unit_suffix})": "{:,.0f}" for m, _ in _BUCKET_COLS}))

st.divider()

# --------------------------------------------------------------------------- #
# CME (COMEX) - LME copper price spread  (market-on-close, previous trading day)
# --------------------------------------------------------------------------- #
st.subheader("CME (COMEX) − LME copper price spread")

if pd.notna(latest.get("cme_lme_spread_3m_usd_t")):
    contract = latest.get("comex_contract") or "front"
    p = st.columns(3)
    p[0].metric("CME − LME (3-month)", f"{latest['cme_lme_spread_3m_usd_t']:+,.0f} USD/t",
                usd_delta("cme_lme_spread_3m_usd_t"))
    p[1].metric(f"CME price ({contract})",
                f"{latest['comex_copper_usd_t']:,.0f} USD/t", usd_delta("comex_copper_usd_t"))
    p[2].metric("LME price (3-month)",
                f"{latest['lme_copper_3m_usd_t']:,.0f} USD/t", usd_delta("lme_copper_3m_usd_t"))

    cpx_d, lme_d = latest.get("comex_price_date"), latest.get("lme_price_date")
    st.caption(
        f"Market-on-close (previous trading day). CME official settlement for the "
        f"most-active COMEX copper month ({contract}) "
        f"{latest.get('comex_copper_usd_lb'):.4f} USD/lb × 2204.62 lb/t, minus LME "
        f"3-month. As of {pd.to_datetime(cpx_d).date() if pd.notna(cpx_d) else 'n/a'} "
        f"(COMEX) / {pd.to_datetime(lme_d).date() if pd.notna(lme_d) else 'n/a'} (LME). "
        "Positive = COMEX above LME. Sources: CME Group, Westmetall."
    )

    _sp = build_daily_series(runs).set_index("date")[["cme_lme_spread_3m_usd_t"]].dropna()
    if not _sp.empty:
        _sp = _sp.loc[_sp.ne(_sp.shift()).any(axis=1)]
        _sp.columns = ["CME − LME 3-month"]
        _sp.index = _sp.index.strftime("%Y-%m-%d")
        st.line_chart(_sp, height=280, y_label="USD/t", x_label="date")
else:
    st.info("No CME–LME price data yet — populates from the next pipeline run.")

# --------------------------------------------------------------------------- #
# Data health / raw log
# --------------------------------------------------------------------------- #
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
    st.caption(f"{len(runs)} pipeline runs on record · parquet: `{DATA_PATH.name}`")

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
- [LME Copper](https://www.lme.com/en/metals/non-ferrous/lme-copper) — day-delayed Closing 3-month + Official cash prices
- [Westmetall market data](https://www.westmetall.com/en/markdaten.php) — LME price fallback

**SHFE**
- [Weekly stock report / 库存周报](https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/?query_params=weeklystock)
- [Daily warehouse-warrant report / 仓单日报](https://www.shfe.com.cn/eng/reports/StatisticalData/DailyData/?query_params=dailystock)

**Prices (fallback)**
- [Yahoo Finance — COMEX copper HG=F](https://finance.yahoo.com/quote/HG=F)
        """
    )

st.caption(
    f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC}. "
    "Report lags: CME ~T+1, LME stock-breakdown ~T+2, LME OWSR ~T+3, SHFE weekly = last Friday."
)
