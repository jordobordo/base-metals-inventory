"""Shared data-loading + formatting helpers for the dashboard pages.

Every page (the Overview router in ``app.py`` / ``views/overview_page.py``, and
``pages/1_Scarcity_Analysis.py``) loads its frames from here — one place to
change the cache behaviour or add a new parquet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = _ROOT / "data" / "copper_inventory.parquet"
GEO_PATH = _ROOT / "data" / "lme_geo.parquet"
SPREAD_HIST_PATH = _ROOT / "data" / "comex_lme_history.parquet"


def mtime(p: Path) -> float:
    """File mtime (0.0 if missing) — hashed into the cache key so a fresh parquet
    busts the cache immediately, without waiting for the TTL."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(ttl=300)
def load_runs(token: float) -> pd.DataFrame:
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
        if c in df.columns:
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


def unit_controls(*, key: str = "unit") -> tuple[float, str]:
    """Sidebar units radio → ``(unit_div, unit_suffix)``."""
    unit = st.sidebar.radio("Units", ["kilotonnes", "tonnes"], index=0, key=key)
    return (1000.0, "kt") if unit == "kilotonnes" else (1.0, "t")
