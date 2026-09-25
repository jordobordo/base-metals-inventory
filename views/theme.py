"""Shared design tokens for the dashboard — the single place both pages get
their colors, chart-axis helper, and "institutional" CSS from.

Before this module the same four bucket colors and three exchange colors were
declared independently in ``app.py`` (``_TINT_HEX`` / ``_EXCH_HEX`` / ``_TINT``)
and in ``views/scarcity.py`` (``_ON`` / ``_CANC`` / ``_TERM`` / ...) — same
values today by luck, nothing stopping them drifting apart tomorrow. Import
from here instead of re-declaring a hex code.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------- #
# Palette — bucket accents, exchange accents, and structural neutrals
# --------------------------------------------------------------------------- #
COLOR_ON_WARRANT = "#5b8def"
COLOR_CANCELLED = "#e0a458"
COLOR_OFF_WARRANT = "#5aa469"
COLOR_REPORTED = "#8a7fc0"   # "reported stock" bucket + LME term-structure line

COLOR_CME = "#d1495b"
COLOR_LME = "#2e86ab"
COLOR_SHFE = "#e5a823"

COLOR_DRAW = "#5aa469"       # inventory draw-rate area (scarcity page)
COLOR_NET = "#2e86ab"        # "net of hurdle" / secondary line
COLOR_GREY = "#9aa0a6"       # zero-rules, stale bands, grid neutrals

BUCKET_COLORS = {
    "On-warrant": COLOR_ON_WARRANT, "Cancelled": COLOR_CANCELLED,
    "Off-warrant": COLOR_OFF_WARRANT, "Reported stock": COLOR_REPORTED,
}
EXCHANGE_COLORS = {"CME (COMEX)": COLOR_CME, "LME": COLOR_LME, "SHFE": COLOR_SHFE}

# Pale tints of the bucket colors, for table-cell backgrounds: (cell, header).
BUCKET_TINTS = {
    "On-warrant": ("#eef4fb", "#d6e5f6"),
    "Cancelled": ("#fdf3e8", "#f6e2c8"),
    "Off-warrant": ("#eef6ee", "#d8ecd8"),
    "Reported stock": ("#f3f1f9", "#e1dbf1"),
    "_plain": ("#f1f3f6", "#dfe3ea"),
}
BORDER_GREY = "#e7e9ef"


def inject_css() -> None:
    """One-time page CSS: tabular numerals on every figure, a touch tighter
    top padding, and a thin rule under each section header. Additive only —
    no layout restructuring, safe to call on every page."""
    st.markdown(
        f"""
        <style>
        [data-testid="stMetricValue"], [data-testid="stMetricDelta"],
        table td, table th, code {{
            font-variant-numeric: tabular-nums;
        }}
        [data-testid="stMetricLabel"] {{
            font-size: 0.8rem; color: #5b6472;
            text-transform: uppercase; letter-spacing: 0.02em;
        }}
        .block-container {{ padding-top: 2rem; padding-bottom: 2rem; }}
        h3 {{ border-bottom: 1px solid {BORDER_GREY}; padding-bottom: 0.35rem; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def date_axis(dates, **kw) -> alt.Axis:
    """A temporal axis with one labelled tick per real data date — no auto
    ticks at half-day / multi-day intervals, no hidden labels. Above ~20
    distinct dates, defers to Altair's own tick selection (a fixed tick per
    point would overlap once there's real history)."""
    vals = sorted({pd.Timestamp(d).normalize() for d in pd.to_datetime(list(dates))})
    if len(vals) > 20:
        return alt.Axis(format="%b %d", labelAngle=-40, labelOverlap=False, **kw)
    return alt.Axis(values=[v.isoformat() for v in vals], format="%b %d",
                    labelAngle=-40, labelOverlap=False, **kw)
