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

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from scripts.schema import (  # noqa: E402
    EXCHANGE_DATE_COL,
    STALE_LABEL,
    build_asof_series,
    build_daily_series,
    build_native_asof_series,
    staleness,
)

DATA_PATH = Path(__file__).parent / "data" / "copper_inventory.parquet"
GEO_PATH = Path(__file__).parent / "data" / "lme_geo.parquet"
SPREAD_HIST_PATH = Path(__file__).parent / "data" / "comex_lme_history.parquet"

BUCKETS = ["on_warrant", "cancelled", "off_warrant"]
BUCKET_LABELS = {"on_warrant": "On-warrant", "cancelled": "Cancelled", "off_warrant": "Off-warrant"}
EXCHANGES = ["cme", "lme", "shfe"]
EXCHANGE_LABELS = {"cme": "CME (COMEX)", "lme": "LME", "shfe": "SHFE"}

st.set_page_config(
    page_title="Global Copper Warehouse Inventory",
    page_icon="🟠",
    layout="wide",
)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
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


@st.cache_data(ttl=300)
def load_geo(token: float) -> pd.DataFrame:
    _ = token
    if not GEO_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(GEO_PATH)
    for c in ("run_date", "report_date"):
        df[c] = pd.to_datetime(df[c])
    return df


@st.cache_data(ttl=300)
def load_spread_history(token: float) -> pd.DataFrame:
    """Multi-week CME-LME spread by market session (scripts/backfill_prices.py)."""
    _ = token
    if not SPREAD_HIST_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(SPREAD_HIST_PATH)
    for c in ("session_date", "lme_price_date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c])
    return df.sort_values("session_date").reset_index(drop=True)


def fmt(value: float | None, unit_div: float, suffix: str) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value / unit_div:,.0f} {suffix}"


runs = load_runs(_mtime(DATA_PATH))
geo = load_geo(_mtime(GEO_PATH))
spread_hist = load_spread_history(_mtime(SPREAD_HIST_PATH))
fresh = staleness(runs)

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
        "View",
        ["Same-Day Synced (LOCF)", "Exchange-Native (as-of)", "Pipeline run date"],
        index=0,
        help=(
            "**Same-Day Synced** — every feed carried forward (LOCF) to a common "
            "daily calendar; carried-forward tails are shaded/dashed.\n\n"
            "**Exchange-Native** — each feed shown only within its own reported "
            "range; the summed global line stops where any feed goes stale "
            "(no false spike).\n\n"
            "**Pipeline run date** — plotted by the day the pipeline fetched it."
        ),
    )
    show_exchanges = st.multiselect(
        "Exchanges",
        options=EXCHANGES,
        default=EXCHANGES,
        format_func=lambda e: EXCHANGE_LABELS[e],
    )

_VIEW_BUILDER = {
    "Same-Day Synced (LOCF)": build_asof_series,
    "Exchange-Native (as-of)": build_native_asof_series,
    "Pipeline run date": build_daily_series,
}
series = _VIEW_BUILDER[timeline](runs).rename(columns={"date": "when"})
if series.empty or "when" not in series:
    series = build_daily_series(runs).rename(columns={"date": "when"})
series = series.set_index("when")
native_view = timeline == "Exchange-Native (as-of)"

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

# Day-over-day is computed on the EXCHANGE-NATIVE series (real report points
# only), so a carried-forward / stale leg can never inject a spike. Prices are
# joined from the run-date daily series.
_dod_series = build_native_asof_series(runs).set_index("date")
_price_cols = [c for c in runs.columns if c.endswith(("_usd_t", "_usd_lb"))]
if _price_cols:
    _dod_series = _dod_series.join(
        build_daily_series(runs).set_index("date")[_price_cols], how="outer"
    )


def _asof(e: str):
    v = latest.get(EXCHANGE_DATE_COL[e])
    return pd.to_datetime(v).date() if pd.notna(v) else None


def dod(col: str) -> tuple[float | None, float | None]:
    """Change of `col` = latest value minus the previous *different* value on the
    exchange-native series (the last real report-to-report move; a
    carried-forward / stale leg contributes 0, never a spike)."""
    if col not in _dod_series.columns:
        return None, None
    vals = _dod_series[col].dropna()
    if vals.empty:
        return None, None
    cur = float(vals.iloc[-1])
    earlier = vals[vals != vals.iloc[-1]]
    prv = float(earlier.iloc[-1]) if not earlier.empty else cur  # flat window -> 0 change
    d = cur - prv
    if d == 0:
        pct = 0.0
    elif prv != 0:
        pct = d / prv * 100.0
    else:
        pct = None
    return d, pct


def delta_str(col: str) -> str | None:
    d, pct = dod(col)
    if d is None:
        return None
    return f"{d / unit_div:+,.0f} {unit_suffix}" + (f"  ({pct:+.1f}%)" if pct is not None else "")


as_of = " · ".join(f"{EXCHANGE_LABELS[e]}: {_asof(e) or 'n/a'}" for e in EXCHANGES)
st.caption(f"Last pipeline run **{latest['run_date'].date()}** — data as of: {as_of}")

# --------------------------------------------------------------------------- #
# Stale-feed banner
# --------------------------------------------------------------------------- #
_stale_since: dict[str, dt.date] = {}
_stale_msgs: list[str] = []
for feed, info in fresh.items():
    if info["stale"]:
        _stale_msgs.append(
            f"**{STALE_LABEL.get(feed, feed)}** carried forward — "
            f"{info['bdays_stale']} business days stale (as of {info['as_of']})"
        )
        _stale_since[feed] = info["as_of"]
if _stale_msgs:
    st.warning("⚠️ " + "  ·  ".join(_stale_msgs))
# earliest date any leg went stale — used to shade chart tails
_earliest_stale = min(
    (pd.Timestamp(d) for d in _stale_since.values()), default=None
)

# --------------------------------------------------------------------------- #
# KPI row
# --------------------------------------------------------------------------- #
k = st.columns(5)
k[0].metric("Global reported stock",
            fmt(latest.get("global_reported_stock_t"), unit_div, unit_suffix),
            delta_str("global_reported_stock_t"))
k[1].metric("Grand total (incl. off-warrant)",
            fmt(latest.get("global_total_t"), unit_div, unit_suffix),
            delta_str("global_total_t"))
for col, b in zip(k[2:], BUCKETS):
    col.metric(f"Global {BUCKET_LABELS[b].lower()}",
               fmt(latest.get(f"global_{b}_t"), unit_div, unit_suffix),
               delta_str(f"global_{b}_t"))

st.caption(
    "**Reported stock** = each exchange's headline published figure — CME "
    "*Registered + Eligible*, LME *on-warrant + cancelled* (closing warrants), "
    "SHFE *库存*. LME **off-warrant** (OWSR) is a separate T+3 report and is "
    "**not** in any exchange's headline, so it is added on top for the "
    "**grand total**. (CME's *Eligible* is COMEX off-warrant metal and is "
    "already inside `cme_total`, so `global_off_warrant` mixes CME Eligible + "
    "LME OWSR.)"
)

st.divider()

# --------------------------------------------------------------------------- #
# Charts — hand-built Altair so no invalid "bind:scales" param is emitted on a
# categorical axis (the cause of the previously-blank chart), and so stale tails
# can be shaded / dashed.
# --------------------------------------------------------------------------- #
_TINT_HEX = {"On-warrant": "#5b8def", "Cancelled": "#e0a458",
             "Off-warrant": "#5aa469", "Reported stock": "#8a7fc0"}
_EXCH_HEX = {"CME (COMEX)": "#d1495b", "LME": "#2e86ab", "SHFE": "#e5a823"}


def _change_points(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows where a value actually changed (a source published),
    keeping the datetime index."""
    changed = df.fillna(-1.0).ne(df.fillna(-1.0).shift()).any(axis=1)
    return df.loc[changed]


def _day_ticks(dates):
    """One ISO tick per distinct data date — feeds `alt.Axis(values=...)` so a
    sparse temporal axis labels every point instead of auto multi-day ticks.
    Above ~20 distinct dates, defer to Altair's own tick selection."""
    u = sorted(pd.to_datetime(pd.Series(list(dates))).dt.normalize().unique())
    if len(u) > 20:
        return alt.Undefined
    return [pd.Timestamp(d).isoformat() for d in u]


def composition_chart(s: pd.DataFrame) -> alt.Chart:
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
                        scale=alt.Scale(domain=list(_TINT_HEX), range=list(_TINT_HEX.values())),
                        legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("day:O", title="date"), "bucket:N",
                 alt.Tooltip("value:Q", format=",.0f")],
    ).properties(height=340)


def by_exchange_chart(s: pd.DataFrame):
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
        x=alt.X("date:T", title=None,
                axis=alt.Axis(values=_day_ticks(long["date"]), format="%b %d", labelAngle=-40)),
        y=alt.Y("value:Q", title=unit_suffix, scale=alt.Scale(zero=False)),
        color=alt.Color("exchange:N", title=None,
                        scale=alt.Scale(domain=list(_EXCH_HEX), range=list(_EXCH_HEX.values())),
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


left, right = st.columns(2)
with left:
    st.subheader("Global composition")
    st.altair_chart(composition_chart(_change_points(series)), width="stretch")
with right:
    st.subheader("Total by exchange")
    st.altair_chart(by_exchange_chart(_change_points(series)), width="stretch")
st.caption(
    "Left: global buckets stacked, one bar per date a source published. "
    "Right: each exchange on its **own** y-scale so day-to-day moves are visible; "
    "dashed segments are carried-forward (stale) values."
    + ("  Exchange-Native view — each line ends at that feed's last real report."
       if native_view else "")
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
st.subheader("Copper price spreads")

# One row per **market session** — a real COMEX settlement date joined with the
# LME cash / 3-month from that same session. Prefer the dedicated multi-week
# history (scripts/backfill_prices.py -> data/comex_lme_history.parquet); fall
# back to whatever distinct sessions the run log captured.
if not spread_hist.empty:
    _h = spread_hist.rename(columns={
        "session_date": "session", "comex_usd_t": "comex_copper_usd_t",
        "lme_3m_usd_t": "lme_copper_3m_usd_t"}).copy()
    _src_label = f"{_h['comex_source'].iloc[-1]} (COMEX) + Westmetall (LME)"
else:
    _keep = ["cme_lme_spread_3m_usd_t", "lme_cash_3m_spread_usd_t",
             "comex_copper_usd_t", "lme_copper_3m_usd_t", "comex_contract", "lme_price_date"]
    _h = (runs[["run_date", "comex_price_date", *[c for c in _keep if c in runs.columns]]]
          .dropna(subset=["comex_price_date"]).copy())
    _h["session"] = pd.to_datetime(_h["comex_price_date"])
    _h = _h.sort_values("run_date").drop_duplicates("session", keep="last")
    _src_label = "CME Group (COMEX) + Westmetall (LME)"

_h = _h.sort_values("session").reset_index(drop=True)
if _h.empty or pd.to_numeric(_h.get("cme_lme_spread_3m_usd_t"), errors="coerce").dropna().empty:
    st.info("No price data yet — populates from the next pipeline run.")
else:
    _last = _h.iloc[-1]
    contract = _last.get("comex_contract") or latest.get("comex_contract") or "front"

    def _sd(col: str) -> str | None:
        """Session-over-session change: latest minus the previous *distinct* value."""
        v = pd.to_numeric(_h.get(col), errors="coerce").dropna()
        if v.empty:
            return None
        cur = float(v.iloc[-1])
        earlier = v[v != cur]
        return f"{cur - (float(earlier.iloc[-1]) if not earlier.empty else cur):+,.0f} USD/t"

    _lme_asof_col = "lme_price_date" if "lme_price_date" in _h.columns else "session"

    def _asof(col: str) -> str:
        v = _last.get(col)
        return f"as of {pd.to_datetime(v).date()}" if pd.notna(v) else "as of —"

    p = st.columns(4)
    p[0].metric("CME − LME (3-month)", f"{_last['cme_lme_spread_3m_usd_t']:+,.0f} USD/t",
                _sd("cme_lme_spread_3m_usd_t"))
    p[0].caption(_asof("session"))
    _c3 = _last.get("lme_cash_3m_spread_usd_t")
    p[1].metric("LME cash − 3-month", f"{_c3:+,.0f} USD/t" if pd.notna(_c3) else "—",
                _sd("lme_cash_3m_spread_usd_t"))
    p[1].caption(_asof(_lme_asof_col))
    p[2].metric(f"CME price ({contract})",
                f"{_last['comex_copper_usd_t']:,.0f} USD/t", _sd("comex_copper_usd_t"))
    p[2].caption(_asof("session"))
    p[3].metric("LME price (3-month)",
                f"{_last['lme_copper_3m_usd_t']:,.0f} USD/t", _sd("lme_copper_3m_usd_t"))
    p[3].caption(_asof(_lme_asof_col))

    _sess = pd.to_datetime(_last["session"])
    _behind = len(pd.bdate_range(_sess, pd.Timestamp(dt.date.today()))) - 1
    if _behind >= 2:
        st.warning(f"⚠️ Latest CME−LME session is **{_sess.date()}** — {_behind} business "
                   "days back. The COMEX settlement feed has not served a fresher date; "
                   "each point is still a correct market-on-close spread for its own session.")
    st.caption(
        f"Per market session (COMEX settlement date = LME as-of date). **CME − LME 3M** = "
        f"most-active COMEX month ({contract}) settle − LME 3-month; **LME cash − 3M** = "
        f"LME term structure (positive = backwardation). {len(_h)} session(s). "
        f"Sources: {_src_label}."
    )

    _long = (_h.rename(columns={"cme_lme_spread_3m_usd_t": "CME − LME 3M",
                                "lme_cash_3m_spread_usd_t": "LME cash − 3M"})
             .melt("session", value_vars=["CME − LME 3M", "LME cash − 3M"],
                   var_name="spread", value_name="usd_t").dropna(subset=["usd_t"]))
    line = alt.Chart(_long).mark_line(point=True).encode(
        x=alt.X("session:T", title=None,
                axis=alt.Axis(values=_day_ticks(_long["session"]),
                              format="%b %d", labelAngle=-40, labelOverlap=False)),
        y=alt.Y("usd_t:Q", title="USD/t"),
        color=alt.Color("spread:N", title=None, legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("session:T", title="session"), "spread:N",
                 alt.Tooltip("usd_t:Q", format="+,.0f")],
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#9aa0a6").encode(y="y:Q")
    st.altair_chart((zero + line).properties(height=300), width="stretch")

# --------------------------------------------------------------------------- #
# LME by location (data/lme_geo.parquet)
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("LME by location")

if geo.empty:
    st.info("Geo breakdown builds from the next pipeline run "
            "(`data/lme_geo.parquet`).")
else:
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
            alt.Chart(top).mark_bar(color="#2e86ab").encode(
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
            alt.Chart(regs).mark_bar(color="#5aa469").encode(
                x=alt.X("region:N", sort="-y", title=None),
                y=alt.Y("off_warrant:Q", title=f"Off-warrant ({unit_suffix})"),
                tooltip=["region:N", alt.Tooltip("off_warrant:Q", format=",.0f")],
            ).properties(height=220),
            width="stretch",
        )

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
- [Westmetall market data](https://www.westmetall.com/en/markdaten.php) — LME Copper cash-settlement + 3-month price (the price leg)

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
