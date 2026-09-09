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
    "global_on_warrant_t", "global_cancelled_t", "global_off_warrant_t",
    "global_reported_stock_t", "global_total_t",
    # Prices — CME (COMEX) vs LME copper, USD (previous completed session)
    "comex_copper_usd_lb", "comex_copper_usd_t", "comex_price_date", "comex_contract",
    "lme_copper_cash_usd_t", "lme_copper_3m_usd_t", "lme_cash_3m_spread_usd_t", "lme_price_date",
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
            ["cme_on_warrant_t", "cme_cancelled_t", "cme_off_warrant_t", "cme_total_t"]),
    "lme": ("lme_warrant_data_date",
            ["lme_on_warrant_t", "lme_cancelled_t", "lme_off_warrant_t", "lme_total_t"]),
    "shfe": ("shfe_data_date",
             ["shfe_on_warrant_t", "shfe_cancelled_t", "shfe_off_warrant_t", "shfe_total_t"]),
}
EXCHANGE_DATE_COL: dict[str, str] = {ex: dcol for ex, (dcol, _) in ASOF_SPEC.items()}

# A feed is "stale" once its latest report is older than this many *business*
# days (report cadence + a day of slack). COMEX ~T+1, LME breakdown ~T+2, LME
# OWSR ~T+3, SHFE weekly = last Friday (so up to ~a week normally).
STALE_AFTER_BDAYS: dict[str, int] = {"cme": 3, "lme": 3, "lme_offwarrant": 5, "shfe": 8}
_STALE_DATE_COL: dict[str, str] = {
    "cme": "cme_data_date", "lme": "lme_warrant_data_date",
    "lme_offwarrant": "lme_offwarrant_data_date", "shfe": "shfe_data_date",
}
STALE_LABEL: dict[str, str] = {
    "cme": "COMEX stocks", "lme": "LME breakdown",
    "lme_offwarrant": "LME off-warrant", "shfe": "SHFE weekly",
}

# --- tidy geographic breakdown (data/lme_geo.parquet) --------------------------
LME_GEO_SCHEMA: list[str] = [
    "run_date", "report_date", "report_type",   # 'breakdown' (per location) | 'owsr' (per region)
    "region", "location",
    "on_warrant_t", "cancelled_t", "opening_t",
    "delivered_in_t", "delivered_out_t", "closing_t", "off_warrant_t",
    "retrieved_at",
]
_GEO_KEY = ["report_date", "report_type", "region", "location"]


def staleness(df: pd.DataFrame, *, ref: dt.date | None = None) -> dict[str, dict]:
    """Per-feed freshness: {feed: {as_of, bdays_stale, stale}}.

    ``bdays_stale`` counts business days between a feed's latest report date and
    ``ref`` (default = the newest ``run_date``). ``stale`` applies the
    :data:`STALE_AFTER_BDAYS` tolerance.
    """
    if df.empty:
        return {}
    ref_ts = pd.Timestamp(ref or pd.to_datetime(df["run_date"]).max())
    out: dict[str, dict] = {}
    for feed, dcol in _STALE_DATE_COL.items():
        if dcol not in df.columns:
            continue
        s = pd.to_datetime(df[dcol], errors="coerce").dropna()
        if s.empty:
            continue
        as_of = s.max()
        bdays = max(0, len(pd.bdate_range(as_of, ref_ts)) - 1)
        out[feed] = {
            "as_of": as_of.date(),
            "bdays_stale": bdays,
            "stale": bdays > STALE_AFTER_BDAYS.get(feed, 3),
        }
    return out


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

    # Chart starts at the first *pipeline run*, not at an old report date a run
    # happened to carry (e.g. last week's SHFE weekly on a Wednesday row).
    run_floor = pd.to_datetime(runs["run_date"]).min()
    cal_start = max(min(starts), run_floor)
    last = max([f[0].index.max() for f in frames.values()] + [end_ts])
    calendar = pd.date_range(cal_start, last, freq=freq)
    out = pd.DataFrame(index=calendar)
    out.index.name = "date"
    # Always emit every value column (NaN if that exchange has no data yet), so
    # downstream column selection never KeyErrors.
    for _dcol, vcols in ASOF_SPEC.values():
        for c in vcols:
            out[c] = float("nan")
    for ex, (sub, have) in frames.items():
        # ffill from each report date (including any before cal_start), then bfill
        # so a later-starting source doesn't create a step-up "spike" when it
        # first appears; finally clip to the visible calendar.
        full_idx = calendar.union(sub.index)
        filled = sub.reindex(full_idx).ffill().bfill().reindex(calendar)
        for c in have:
            out[c] = filled[c]
        # tail-stale flag: everything on the calendar after this feed's last
        # real report is a carry-forward.
        out[f"{ex}_stale"] = calendar > sub.index.max()

    _add_global_cols(out)
    return out.reset_index()


def build_native_asof_series(
    df: pd.DataFrame, *, end: dt.date | None = None, freq: str = "B"
) -> pd.DataFrame:
    """Exchange-native as-of view: each feed's step-function values exist **only**
    within [first report, last report] — no carry past the last report, no
    back-fill before the first. ``global_*`` requires every leg present on a date
    (``min_count`` = number of contributing feeds) so the summed line stops on
    the earliest stale date instead of dropping/spiking. ``*_stale`` columns are
    all ``False`` here (no carried tail) but kept for a uniform API.
    """
    if df.empty:
        return df.copy()
    end_ts = pd.Timestamp(end or dt.date.today())
    runs = df.sort_values("run_date")

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
        frames[ex] = (sub.sort_values(dcol).set_index(dcol), have)
        starts.append(frames[ex][0].index.min())

    if not starts:
        return pd.DataFrame(columns=["date"])

    run_floor = pd.to_datetime(runs["run_date"]).min()
    cal_start = max(min(starts), run_floor)
    last = max([f[0].index.max() for f in frames.values()] + [end_ts])
    calendar = pd.date_range(cal_start, last, freq=freq)
    out = pd.DataFrame(index=calendar)
    out.index.name = "date"
    for _dcol, vcols in ASOF_SPEC.values():
        for c in vcols:
            out[c] = float("nan")

    for ex, (sub, have) in frames.items():
        first, lastr = sub.index.min(), sub.index.max()
        window = calendar[(calendar >= first) & (calendar <= lastr)]
        filled = sub.reindex(calendar.union(sub.index)).ffill().reindex(window)
        for c in have:
            out.loc[window, c] = filled[c]
        out[f"{ex}_stale"] = False

    _add_global_cols(out, require_all=True)
    return out.reset_index()


def _add_global_cols(out: pd.DataFrame, *, require_all: bool = False) -> None:
    def _sum(cols: list[str]) -> pd.Series:
        present = [c for c in cols if c in out.columns]
        if not present:
            return pd.Series(index=out.index, dtype="float64")
        mc = len(present) if require_all else 1
        return out[present].sum(axis=1, min_count=mc)

    out["global_on_warrant_t"] = _sum(["cme_on_warrant_t", "lme_on_warrant_t", "shfe_on_warrant_t"])
    out["global_cancelled_t"] = _sum(["cme_cancelled_t", "lme_cancelled_t", "shfe_cancelled_t"])
    out["global_off_warrant_t"] = _sum(["cme_off_warrant_t", "lme_off_warrant_t"])  # SHFE: none
    out["global_reported_stock_t"] = _sum(["cme_total_t", "lme_total_t", "shfe_total_t"])
    out["global_total_t"] = _sum(["cme_total_t", "lme_total_t", "shfe_total_t", "lme_off_warrant_t"])


# --------------------------------------------------------------------------- #
# Geographic breakdown parquet
# --------------------------------------------------------------------------- #
def upsert_geo(rows: list[dict], path) -> pd.DataFrame:
    """Insert/replace tidy LME geo rows (keyed on report_date+type+region+location)
    and write ``path`` back. ``rows`` may be empty (no-op returns existing/empty)."""
    from pathlib import Path

    path = Path(path)
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=LME_GEO_SCHEMA)
    if not rows:
        return existing
    new = pd.DataFrame([{k: r.get(k) for k in LME_GEO_SCHEMA} for r in rows], columns=LME_GEO_SCHEMA)
    for c in ("run_date", "report_date"):
        new[c] = pd.to_datetime(new[c], errors="coerce")
        if c in existing.columns:
            existing[c] = pd.to_datetime(existing[c], errors="coerce")
    new["retrieved_at"] = pd.to_datetime(new["retrieved_at"], utc=True, errors="coerce")
    combined = pd.concat([existing, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=_GEO_KEY, keep="last").sort_values(
        ["report_date", "report_type", "region", "location"]
    ).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path, index=False)
    return combined
