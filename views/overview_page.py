"""Overview — the CME/LME/SHFE global copper warehouse-inventory picture.

The page ``st.navigation`` runs for the "Overview" entry (see ``app.py``, the
router). Sidebar controls + the as-of/stale header live here since they're
Overview-specific state; the sections themselves are ``views/overview.py``.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.schema import (  # noqa: E402
    build_asof_series,
    build_daily_series,
    build_native_asof_series,
    staleness,
)
from views import overview  # noqa: E402
from views.common import (  # noqa: E402
    DATA_PATH, GEO_PATH, SPREAD_HIST_PATH,
    load_geo, load_runs, load_spread_history, mtime, unit_controls,
)
from views.theme import inject_css  # noqa: E402

inject_css()

runs = load_runs(mtime(DATA_PATH))
geo = load_geo(mtime(GEO_PATH))
spread_hist = load_spread_history(mtime(SPREAD_HIST_PATH))
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
unit_div, unit_suffix = unit_controls()
with st.sidebar:
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
        options=overview.EXCHANGES,
        default=overview.EXCHANGES,
        format_func=lambda e: overview.EXCHANGE_LABELS[e],
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

# --------------------------------------------------------------------------- #
# As-of header + stale banner
# --------------------------------------------------------------------------- #
latest = runs.iloc[-1]
as_of = " · ".join(
    f"{overview.EXCHANGE_LABELS[e]}: {overview.exchange_as_of(runs, e) or 'n/a'}"
    for e in overview.EXCHANGES
)
st.caption(f"Last pipeline run **{latest['run_date'].date()}** — data as of: {as_of}")
overview.stale_banner(fresh)

st.divider()
st.subheader("Global inventories")
overview.kpi_row(runs, unit_div, unit_suffix)

st.divider()
left, right = st.columns(2)
with left:
    st.subheader("Global composition")
    st.altair_chart(overview.composition_chart(overview.change_points(series), unit_div, unit_suffix),
                    width="stretch")
with right:
    st.subheader("Total by exchange")
    st.altair_chart(
        overview.by_exchange_chart(overview.change_points(series), show_exchanges, unit_div, unit_suffix),
        width="stretch")
st.caption(
    "Left: global buckets stacked, one bar per date a source published. "
    "Right: each exchange on its **own** y-scale so day-to-day moves are visible; "
    "dashed segments are carried-forward (stale) values."
    + ("  Exchange-Native view — each line ends at that feed's last real report."
       if native_view else "")
)

st.subheader("Breakdown & day-over-day change")
overview.breakdown_table(runs, unit_div, unit_suffix)

st.divider()
st.subheader("Copper price spreads")
overview.price_spread_section(runs, spread_hist)

st.divider()
st.subheader("LME by location")
overview.geo_section(geo, unit_div, unit_suffix)

overview.data_health_section(runs, DATA_PATH)
overview.sources_section()

st.caption(
    f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC}. "
    "Report lags: CME ~T+1, LME stock-breakdown ~T+2, LME OWSR ~T+3, SHFE weekly = last Friday."
)
