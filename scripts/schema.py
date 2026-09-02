"""Shared schema + LOCF helper for the copper inventory pipeline.

Deliberately dependency-light (only ``datetime`` + ``pandas``) so the Streamlit
dashboard can import it without pulling in the scraper stack.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

# Column order for the stored parquet row. Keep stable — the dashboard depends on it.
SCHEMA: list[str] = [
    "run_date", "retrieved_at",
    # CME (converted to tonnes; raw short tons kept for audit)
    "cme_on_warrant_t", "cme_cancelled_t", "cme_off_warrant_t", "cme_total_t",
    "cme_data_date", "cme_stale", "cme_registered_short_tons", "cme_eligible_short_tons",
    # LME
    "lme_on_warrant_t", "lme_cancelled_t", "lme_off_warrant_t", "lme_total_t",
    "lme_warrant_data_date", "lme_offwarrant_data_date", "lme_stale", "lme_offwarrant_stale",
    # SHFE (weekly backbone; daily warrant on the side)
    "shfe_on_warrant_t", "shfe_cancelled_t", "shfe_off_warrant_t", "shfe_total_t",
    "shfe_data_date", "shfe_stale", "shfe_warrant_daily_t", "shfe_warrant_daily_date",
    # Global
    "global_on_warrant_t", "global_cancelled_t", "global_off_warrant_t", "global_total_t",
    # Provenance
    "sources_ok", "sources_failed", "notes",
]

DATE_COLS: set[str] = {
    "run_date", "cme_data_date", "lme_warrant_data_date", "lme_offwarrant_data_date",
    "shfe_data_date", "shfe_warrant_daily_date",
}


def build_daily_series(
    df: pd.DataFrame,
    *,
    end: dt.date | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Reindex the run log to a continuous daily calendar and forward-fill.

    Satisfies the spec's "forward-fill (LOCF) any missing days due to regional
    holidays". Index is a daily ``DatetimeIndex`` from the first run to ``end``
    (default: today). Numeric value columns are filled; ``*_data_date`` and
    ``*_stale`` columns are carried forward too so staleness stays visible.
    """
    if df.empty:
        return df.copy()
    work = df.copy()
    work["run_date"] = pd.to_datetime(work["run_date"])
    work = work.sort_values("run_date").set_index("run_date")

    end_ts = pd.Timestamp(end or dt.date.today())
    calendar = pd.date_range(work.index.min(), max(work.index.max(), end_ts), freq="D")

    cols = columns or [
        c for c in work.columns
        if c.endswith("_t") or c in DATE_COLS or c.endswith("_stale")
    ]
    out = work[cols].reindex(calendar).ffill()
    out.index.name = "date"
    return out.reset_index()
