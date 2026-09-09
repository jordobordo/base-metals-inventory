"""Physical vs Paper Scarcity — institutional read on copper stock drawdowns.

A second Streamlit page (the overview stays in ``app.py``). Everything analytical
comes from ``scripts/analytics.py``; the charts are in ``views/scarcity.py``.

Run:  streamlit run app.py   → pick "Scarcity Analysis" in the sidebar.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analytics import ArbCostBand, scarcity_scorecard  # noqa: E402
from scripts.schema import EXCHANGE_DATE_COL, staleness  # noqa: E402
from views.common import GEO_PATH, DATA_PATH, load_geo, load_runs, mtime, unit_controls  # noqa: E402
from views.scarcity import (  # noqa: E402
    anomaly_table,
    chart_spatial_concentration,
    chart_term_structure_arb,
    chart_warrant_vs_loadout,
    kpi_row,
)

st.set_page_config(page_title="Physical vs Paper Scarcity", page_icon="🔬", layout="wide")

runs = load_runs(mtime(DATA_PATH))
geo = load_geo(mtime(GEO_PATH))

st.title("🔬 Physical vs Paper Scarcity")

if runs.empty:
    st.info("No data yet. Run `python scripts/aggregate.py` or wait for the daily "
            "GitHub Action to commit the first `data/copper_inventory.parquet`.")
    st.stop()

# --------------------------------------------------------------------------- #
# Sidebar — units + the adjustable physical cost hurdle
# --------------------------------------------------------------------------- #
unit_div, unit_suffix = unit_controls(key="scarcity_unit")
with st.sidebar:
    st.header("Physical cost hurdle")
    st.caption("Cost to move an LME-deliverable tonne onto COMEX. Drives the arb "
               "band and the *Arb Open/Closed* label.")
    freight = st.slider("Freight (USD/mt)", 0, 400, 120, 5)
    finance = st.slider("Finance + insurance (USD/mt)", 0, 150, 25, 5)
    tariff = st.slider("Import tariff (% of LME 3M)", 0, 60, 0, 1)

band = ArbCostBand(freight_usd_mt=float(freight),
                   finance_insurance_usd_mt=float(finance),
                   tariff_pct=float(tariff))

# --------------------------------------------------------------------------- #
# As-of caption + stale banner (same rules as the overview page)
# --------------------------------------------------------------------------- #
latest = runs.iloc[-1]
_asof = {e: (pd.to_datetime(latest.get(c)).date() if pd.notna(latest.get(c)) else "n/a")
         for e, c in EXCHANGE_DATE_COL.items()}
st.caption(f"Last pipeline run **{latest['run_date'].date()}** — data as of: "
           + " · ".join(f"{e.upper()}: {v}" for e, v in _asof.items()))

_stale = [f"**{f}** {i['bdays_stale']}bd stale (as of {i['as_of']})"
          for f, i in staleness(runs).items() if i["stale"]]
if _stale:
    st.warning("⚠️ carried forward — " + "  ·  ".join(_stale))

# --------------------------------------------------------------------------- #
# Verdict banner
# --------------------------------------------------------------------------- #
card = scarcity_scorecard(runs, geo, band=band)
_TONE = {"Physical scarcity": "🟢", "Warehouse reshuffling / financing": "🟠",
         "Mixed / inconclusive": "⚪"}
v = st.columns([2, 1])
v[0].metric("Verdict", f"{_TONE.get(card.verdict, '')} {card.verdict}")
v[1].metric("Score", f"{card.score:+.2f}", help="-1 = reshuffling / financing … +1 = physical scarcity")
with st.expander("Why", expanded=False):
    for r in card.rationale:
        st.markdown(f"- {r}")

st.divider()
st.subheader("Global inventories & market temperature")
kpi_row(runs, band, unit_div, unit_suffix)

st.divider()
st.subheader("LME spatial concentration & warrant status")
chart_spatial_concentration(geo, unit_div, unit_suffix)

st.divider()
st.subheader("Warrant dynamics vs physical load-out")
chart_warrant_vs_loadout(geo, unit_div, unit_suffix)

st.divider()
st.subheader("Term structure & arbitrage band")
chart_term_structure_arb(runs, band, unit_div, unit_suffix)

st.divider()
st.subheader("Anomaly & diagnostics")
anomaly_table(runs, geo, band)

st.caption(f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC}. "
           "Report lags: CME ~T+1, LME breakdown ~T+2, LME OWSR ~T+3, SHFE weekly = last Friday.")
