"""Shared schema + LOCF helper for the copper inventory pipeline.

Deliberately dependency-light (only ``datetime`` + ``pandas``) so the Streamlit
dashboard can import it without pulling in the scraper stack.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

# Column order for the stored parquet row. Keep stable — the dashboard depends on it.
#
# Inventory taxonomy (per exchange), all metric tonnes:
#   on_warrant   registered / pledged / deliverable against the contract
#   cancelled    warranted metal earmarked for withdrawal (a load-out queue) —
#                LME only; CME and SHFE have no such mechanism (0)
#   unregistered spec metal inside an exchange-monitored warehouse, not on warrant,
#                no withdrawal queue — CME "Eligible" and SHFE (库存 − 仓单)
#   off_warrant  shadow inventory off the exchange warrant system (LME OWSR only)
#   *_total_t    = "reported stock" = on_warrant + cancelled + unregistered
#                  (all metal in exchange-monitored warehouses — consistent across
#                   the three exchanges; excludes off_warrant / shadow)
SCHEMA: list[str] = [
    "run_date", "retrieved_at",
    # CME (converted to tonnes; raw short tons kept for audit)
    "cme_on_warrant_t", "cme_cancelled_t", "cme_unregistered_t", "cme_off_warrant_t", "cme_total_t",
    "cme_data_date", "cme_stale", "cme_registered_short_tons", "cme_eligible_short_tons",
    # LME
    "lme_on_warrant_t", "lme_cancelled_t", "lme_unregistered_t", "lme_off_warrant_t", "lme_total_t",
    "lme_warrant_data_date", "lme_offwarrant_data_date", "lme_stale", "lme_offwarrant_stale",
    # SHFE (weekly backbone; daily warrant on the side)
    "shfe_on_warrant_t", "shfe_cancelled_t", "shfe_unregistered_t", "shfe_off_warrant_t", "shfe_total_t",
    "shfe_data_date", "shfe_stale", "shfe_warrant_daily_t", "shfe_warrant_daily_date",
    # Global
    "global_on_warrant_t", "global_cancelled_t", "global_unregistered_t", "global_off_warrant_t",
    "global_reported_stock_t", "global_total_t",
    # Prices — CME (COMEX) vs LME copper, USD (previous completed session)
    "comex_copper_usd_lb", "comex_copper_usd_t", "comex_price_date", "comex_contract",
    "lme_copper_cash_usd_t", "lme_copper_3m_usd_t", "lme_price_date",
    "cme_lme_spread_usd_t", "cme_lme_spread_3m_usd_t", "price_stale",
    # Provenance
    "sources_ok", "sources_failed", "notes",
]

DATE_COLS: set[str] = {
    "run_date", "cme_data_date", "lme_warrant_data_date", "lme_offwarrant_data_date",
    "shfe_data_date", "shfe_warrant_daily_date", "comex_price_date", "lme_price_date",
}

# Which column holds each exchange's report as-of date, and its value columns.
ASOF_SPEC: dict[str, tuple[str, list[str]]] = {
    "cme": ("cme_data_date",
            ["cme_on_warrant_t", "cme_cancelled_t", "cme_unregistered_t",
             "cme_off_warrant_t", "cme_total_t"]),
    "lme": ("lme_warrant_data_date",
            ["lme_on_warrant_t", "lme_cancelled_t", "lme_unregistered_t",
             "lme_off_warrant_t", "lme_total_t"]),
    "shfe": ("shfe_data_date",
             ["shfe_on_warrant_t", "shfe_cancelled_t", "shfe_unregistered_t",
              "shfe_off_warrant_t", "shfe_total_t"]),
}
EXCHANGE_DATE_COL: dict[str, str] = {ex: dcol for ex, (dcol, _) in ASOF_SPEC.items()}


def build_daily_series(
    df: pd.DataFrame,
    *,
    end: dt.date | None = None,
    columns: list[str] | None = None,
    freq: str = "B",
) -> pd.DataFrame:
    """Reindex the run log to a business-day calendar and forward-fill.

    Satisfies the spec's "forward-fill (LOCF) any missing days due to regional
    holidays". ``freq`` defaults to ``"B"`` (Mon-Fri) so weekends never appear on
    charts; pass ``"D"`` for a true every-day index. Numeric value columns are
    filled; ``*_data_date`` and ``*_stale`` columns are carried forward too so
    staleness stays visible.
    """
    if df.empty:
        return df.copy()
    work = df.copy()
    work["run_date"] = pd.to_datetime(work["run_date"])
    work = work.sort_values("run_date").set_index("run_date")

    end_ts = pd.Timestamp(end or dt.date.today())
    calendar = pd.date_range(work.index.min(), max(work.index.max(), end_ts), freq=freq)

    cols = columns or [
        c for c in work.columns
        if c.endswith(("_t", "_lb")) or c in DATE_COLS or c.endswith("_stale")
    ]
    out = work[cols].reindex(calendar).ffill()
    out.index.name = "date"
    return out.reset_index()


def build_asof_series(
    df: pd.DataFrame, *, end: dt.date | None = None, freq: str = "B"
) -> pd.DataFrame:
    """Daily series indexed by each exchange's **report as-of date**, not run date.

    Every exchange is placed on the timeline at the date its report is *for*
    (LME ~T+2, CME ~T+1, SHFE = the report Friday), then forward-filled. When
    two runs cover the same as-of date, the later run wins. The ``global_*``
    columns are re-summed from the aligned per-exchange values, so a source that
    only has recent history simply starts its contribution later (min_count=1 —
    the total is NaN only where no exchange has data yet).

    Index runs from the earliest as-of date across all exchanges to ``end``
    (default today). Returns a frame with a ``date`` column.
    """
    if df.empty:
        return df.copy()

    end_ts = pd.Timestamp(end or dt.date.today())
    runs = df.sort_values("run_date")  # later run wins on same as-of date

    frames: dict[str, tuple[pd.DataFrame, list[str]]] = {}
    starts: list[pd.Timestamp] = []
    for ex, (dcol, vcols) in ASOF_SPEC.items():
        have = [c for c in vcols if c in runs.columns]
        if dcol not in runs.columns or not have:
            continue
        sub = runs[[dcol, *have]].copy()
        sub[dcol] = pd.to_datetime(sub[dcol], errors="coerce")
        sub = sub.dropna(subset=[dcol]).drop_duplicates(subset=[dcol], keep="last")
        if sub.empty:
            continue
        sub = sub.sort_values(dcol).set_index(dcol)
        frames[ex] = (sub, have)
        starts.append(sub.index.min())

    if not starts:
        return pd.DataFrame(columns=["date"])

    last = max([f[0].index.max() for f in frames.values()] + [end_ts])
    calendar = pd.date_range(min(starts), last, freq=freq)
    out = pd.DataFrame(index=calendar)
    out.index.name = "date"
    # Always emit every value column (NaN if that exchange has no data yet), so
    # downstream column selection never KeyErrors.
    for _dcol, vcols in ASOF_SPEC.values():
        for c in vcols:
            out[c] = float("nan")
    for _ex, (sub, have) in frames.items():
        # ffill forward from each exchange's first report; bfill its earliest
        # value back to the series start so a later-starting source (e.g. CME's
        # T+1 date) doesn't create a step-up "spike" when it first appears.
        filled = sub.reindex(calendar).ffill().bfill()
        for c in have:
            out[c] = filled[c]

    def _sum(cols: list[str]) -> pd.Series:
        present = [c for c in cols if c in out.columns]
        if not present:
            return pd.Series(index=out.index, dtype="float64")
        return out[present].sum(axis=1, min_count=1)

    out["global_on_warrant_t"] = _sum(["cme_on_warrant_t", "lme_on_warrant_t", "shfe_on_warrant_t"])
    out["global_cancelled_t"] = _sum(["cme_cancelled_t", "lme_cancelled_t", "shfe_cancelled_t"])
    out["global_unregistered_t"] = _sum(["cme_unregistered_t", "shfe_unregistered_t"])  # LME: none
    out["global_off_warrant_t"] = _sum(["lme_off_warrant_t"])  # shadow — LME OWSR only
    # "Reported stock" = exchange-monitored warehouse stock = on + cancelled +
    # unregistered = each exchange's *_total_t summed.
    out["global_reported_stock_t"] = _sum(["cme_total_t", "lme_total_t", "shfe_total_t"])
    # Grand total also counts LME off-warrant / shadow inventory.
    out["global_total_t"] = _sum(["cme_total_t", "lme_total_t", "shfe_total_t", "lme_off_warrant_t"])
    return out.reset_index()
