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


@st.cache_data(ttl=1800)
def load_runs() -> pd.DataFrame:
    if not DATA_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(DATA_PATH)
    df["run_date"] = pd.to_datetime(df["run_date"])
    return df.sort_values("run_date").reset_index(drop=True)


def fmt(value: float | None, unit_div: float, suffix: str) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value / unit_div:,.0f} {suffix}"


runs = load_runs()

st.title("🟠 Global Copper Warehouse Inventory")
st.caption("LME + CME (COMEX) + SHFE · harmonised to metric tonnes · on-warrant / cancelled / off-warrant")

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


def _asof(e: str):
    v = latest.get(EXCHANGE_DATE_COL[e])
    return pd.to_datetime(v).date() if pd.notna(v) else None


def dod(col: str) -> tuple[float | None, float | None]:
    """Day-over-day absolute + % change of `col` (latest run vs previous run)."""
    cur = latest.get(col)
    prv = prev.get(col) if prev is not None else None
    if pd.isna(cur) or prv is None or pd.isna(prv):
        return None, None
    d = float(cur) - float(prv)
    pct = (d / float(prv) * 100.0) if float(prv) != 0 else None
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
st.caption(
    f"Reported stock = each exchange's own headline figure (COMEX total, LME "
    f"on-warrant, SHFE 库存). Grand total **incl. LME off-warrant** "
    f"({fmt(latest.get('lme_off_warrant_t'), unit_div, unit_suffix)}): "
    f"**{fmt(latest.get('global_total_t'), unit_div, unit_suffix)}**."
)

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
    """DoD change of `col` in tonnes with the % change in brackets."""
    d, pct = dod(col)
    if d is None:
        return "—"
    return f"{d:+,.0f} t" + (f"  ({pct:+.1f}%)" if pct is not None else "")


rows = []
for e in EXCHANGES:
    row: dict[str, object] = {"Exchange": EXCHANGE_LABELS[e]}
    for mlabel, msuf in _BUCKET_COLS:
        col = f"{e}_{msuf}"
        cur = latest.get(col)
        row[f"{mlabel} ({unit_suffix})"] = float(cur) / unit_div if pd.notna(cur) else None
        row[f"{mlabel} · DoD Δ"] = _dod_cell(col)
    row["As of"] = _asof(e)
    rows.append(row)

bt = pd.DataFrame(rows).set_index("Exchange")
st.dataframe(
    bt.style.format({f"{m} ({unit_suffix})": "{:,.0f}" for m, _ in _BUCKET_COLS}, na_rep="—"),
    width="stretch",
)
st.caption(
    "**Reported stock** = each exchange's headline figure: CME = Registered + "
    "Eligible; LME = on-warrant (live + cancelled); SHFE = 库存. Off-warrant is a "
    "separate figure only LME publishes — *not* in LME's reported stock, but *is* "
    "in the global grand total above. SHFE *Cancelled* is the implied non-warranted "
    "portion (库存 − 仓单). **DoD Δ is in tonnes** (latest run vs previous run), "
    f"% change in brackets. SHFE daily warrant (side feed): "
    f"{fmt(latest.get('shfe_warrant_daily_t'), unit_div, unit_suffix)} @ "
    f"{pd.to_datetime(latest.get('shfe_warrant_daily_date')).date() if pd.notna(latest.get('shfe_warrant_daily_date')) else 'n/a'}."
)

# --------------------------------------------------------------------------- #
# Data health / raw log
# --------------------------------------------------------------------------- #
with st.expander("Data health & raw run log"):
    health_cols = [
        "run_date", "sources_ok", "sources_failed",
        "cme_stale", "lme_stale", "lme_offwarrant_stale", "shfe_stale",
        "global_total_t", "notes",
    ]
    log = runs[health_cols].sort_values("run_date", ascending=False).head(30).copy()
    log["run_date"] = log["run_date"].dt.date
    st.dataframe(log, width="stretch", hide_index=True)
    st.caption(f"{len(runs)} pipeline runs on record · parquet: `{DATA_PATH.name}`")

st.caption(
    f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC} · "
    "source reports: LME stock-breakdown (T+2) + OWSR (T+3), CME Copper_Stocks (T+1), "
    "SHFE weekly stock report."
)
