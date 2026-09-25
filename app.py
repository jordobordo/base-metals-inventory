"""
Dashboard entrypoint — Streamlit Cloud points here.

This file is a **router only**: it owns the one shared ``st.set_page_config``
call and hands off to the page scripts via ``st.navigation``. The actual pages
are ``views/overview_page.py`` (default) and ``pages/1_Scarcity_Analysis.py``.

Both read ``data/copper_inventory.parquet`` (+ ``data/lme_geo.parquet`` and
``data/comex_lme_history.parquet``), committed to the repo by the daily
GitHub Action, and show the global copper warehouse-inventory picture across
LME, CME (COMEX) and SHFE, harmonised to metric tonnes.

Deploy: point Streamlit Community Cloud at this repo, main file ``app.py``,
Python 3.11. No secrets. It redeploys whenever the Action commits a new parquet.

Local:  streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Global Copper Warehouse Inventory",
    page_icon="🟠",
    layout="wide",
)

pg = st.navigation([
    st.Page("views/overview_page.py", title="Overview", icon="🟠", default=True),
    st.Page("pages/1_Scarcity_Analysis.py", title="Scarcity Analysis", icon="🔬"),
])
pg.run()
