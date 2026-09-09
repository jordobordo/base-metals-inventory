"""Scarcity vs. reshuffling analytics for the copper inventory pipeline.

Answers one question: when headline warehouse stock falls, is it **physical
scarcity** (metal genuinely leaving the system) or **warehouse reshuffling /
financing** (warrants cancelled and re-warranted, metal parked off-warrant, a
paper arb that never clears)?

Three layers, matching the request:

1. Warrant lifecycle & "phantom tightness"  -> :func:`location_warrant_flows`,
   :func:`cancelled_share`.  Per LME location: fresh cancellations vs. actual
   load-out (Delivered-Out), a re-warranting detector, and the cancelled share.
2. Statistical anomaly detection  -> :func:`rolling_zscore`, :func:`zscore_frame`,
   :func:`anomaly_scan`.  Rolling 30- / 90-day Z-scores of net cancellations,
   physical load-outs and the cash-to-3M spread, with an ``abs(z) > 2`` alert.
3. Arbitrage hurdle modelling  -> :class:`ArbCostBand`, :func:`net_arb_margin`,
   :func:`classify_arb_regime`, :func:`arb_hurdle_frame`.  A configurable
   freight + finance + duty band that splits "Paper Dislocation" from an
   "Open Physical Arb".

Dashboard helpers for the Scarcity Analysis page: :func:`hub_of` /
:func:`hub_warrant_status` / :func:`cancellation_concentration` (spatial
concentration), :func:`loadout_response` (cancellation spikes that produced no
load-out), :func:`net_draw_rate`, and :func:`diagnose_anomalies` (per-location
alert table with an automated interpretation tag).

:func:`scarcity_scorecard` folds the first three layers into a single signed verdict.

Dependency-light on purpose (pandas + numpy only) so the dashboard can import it.
Reads ``data/copper_inventory.parquet`` (wide run log) and
``data/lme_geo.parquet`` (tidy per-location breakdown); see ``scripts/schema.py``.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
DEFAULT_PARQUET = _HERE.parent / "data" / "copper_inventory.parquet"
DEFAULT_GEO_PARQUET = _HERE.parent / "data" / "lme_geo.parquet"

if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
from scripts.schema import build_native_asof_series  # noqa: E402

# 1 metric tonne = 2204.6226 lb  (kept here too so callers can normalise $/lb feeds)
LB_PER_TONNE = 2204.6226218488

ALERT_Z = 2.0
DEFAULT_WINDOWS: tuple[int, int] = (30, 90)

# A physical move below this many tonnes is treated as "nothing loaded out" when
# deciding whether a cancelled-warrant swing was a re-warranting rather than a
# withdrawal.  ~one LME warrant lot rounding.
LOADOUT_TOL_T = 250.0
REWARRANT_MIN_T = 250.0


def usd_lb_to_mt(usd_per_lb: float | pd.Series) -> float | pd.Series:
    """Convenience: convert a $/lb price to $/mt (COMEX quotes copper in $/lb)."""
    return usd_per_lb * LB_PER_TONNE


# --------------------------------------------------------------------------- #
# 1. Warrant lifecycle & "phantom tightness"
# --------------------------------------------------------------------------- #
def cancelled_share(
    on_warrant: float | pd.Series, cancelled: float | pd.Series
) -> float | pd.Series:
    """``cancelled / (on_warrant + cancelled) * 100``.

    The share of *total* warranted metal that is earmarked for withdrawal.  A
    rising share with no matching load-out is the classic "phantom tightness"
    tell.  Returns ``NaN`` where the denominator is zero.
    """
    on_w = np.asarray(on_warrant, dtype="float64")
    can = np.asarray(cancelled, dtype="float64")
    denom = on_w + can
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(denom > 0, can / denom * 100.0, np.nan)
    if isinstance(on_warrant, pd.Series):
        return pd.Series(share, index=on_warrant.index, name="cancelled_share")
    if isinstance(cancelled, pd.Series):
        return pd.Series(share, index=cancelled.index, name="cancelled_share")
    return float(share)


_GEO_LEVELS: dict[str, list[str]] = {
    "location": ["region", "location"],
    "region": ["region"],
    "global": [],
}
_FLOW_NUM = [
    "on_warrant_t", "cancelled_t", "opening_t",
    "delivered_in_t", "delivered_out_t", "closing_t",
]


def location_warrant_flows(
    geo: pd.DataFrame,
    *,
    level: str = "location",
    loadout_tol_t: float = LOADOUT_TOL_T,
    rewarrant_min_t: float = REWARRANT_MIN_T,
) -> pd.DataFrame:
    """Per-location warrant lifecycle flows from the tidy geo breakdown.

    One row per ``(<level key>, report_date)``, sorted, carrying the reported
    levels plus period-over-period flow decomposition:

    ================================  ==========================================
    ``d_on_warrant_t`` / ``d_cancelled_t`` / ``d_closing_t``
                                      change since the previous report
    ``new_cancellations_t``           gross tonnage moved *into* cancelled state
                                      (``max(d_cancelled, 0)``) — intent to pull
    ``uncancelled_t``                 cancelled tonnage that left cancelled state
                                      (``max(-d_cancelled, 0)``)
    ``withdrawals_t``                 actual physical load-out (Delivered-Out)
    ``net_physical_flow_t``           Delivered-In − Delivered-Out (>0 = inflow)
    ``implied_rewarrant_t``           ``max(0, uncancelled_t − withdrawals_t)`` —
                                      cancelled metal that re-appeared on warrant
                                      instead of physically leaving
    ``cancellation_drawdown_ratio``   ``withdrawals_t / new_cancellations_t`` —
                                      how much freshly-cancelled metal actually
                                      leaves (low ⇒ phantom; NaN if none cancelled)
    ``cancelled_share`` / ``d_cancelled_share``
                                      level (%) and its change
    ``is_rewarranting``               bool: cancelled fell & on-warrant rose with
                                      no matching load-out
    ``phantom_tightness``             bool: cancelled rose & on-warrant fell with
                                      no matching load-out (headline drawdown that
                                      is warrant re-labelling, not scarcity)
    ================================  ==========================================

    ``level`` ∈ {``"location"``, ``"region"``, ``"global"``}.  For the LME
    breakdown ``region`` is the reporting country.
    """
    if level not in _GEO_LEVELS:
        raise ValueError(f"level must be one of {sorted(_GEO_LEVELS)}, got {level!r}")
    if geo is None or geo.empty:
        return pd.DataFrame()

    df = geo.loc[geo["report_type"] == "breakdown"].copy()
    if df.empty:
        return pd.DataFrame()
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df = df.dropna(subset=["report_date"])
    for c in _FLOW_NUM:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")

    key = _GEO_LEVELS[level]
    if key:
        df = (
            df.groupby([*key, "report_date"], as_index=False, dropna=False)[_FLOW_NUM]
            .sum(min_count=1)
        )
    else:  # global: collapse all locations per report_date
        df = df.groupby("report_date", as_index=False)[_FLOW_NUM].sum(min_count=1)
    df = df.sort_values([*key, "report_date"]).reset_index(drop=True)

    grp = df.groupby(key, dropna=False) if key else None

    def _diff(col: str) -> pd.Series:
        return grp[col].diff() if grp is not None else df[col].diff()

    df["d_on_warrant_t"] = _diff("on_warrant_t")
    df["d_cancelled_t"] = _diff("cancelled_t")
    df["d_closing_t"] = _diff("closing_t")

    df["withdrawals_t"] = df["delivered_out_t"]
    df["net_physical_flow_t"] = df["delivered_in_t"].fillna(0) - df["delivered_out_t"].fillna(0)
    df["new_cancellations_t"] = df["d_cancelled_t"].clip(lower=0)
    df["uncancelled_t"] = (-df["d_cancelled_t"]).clip(lower=0)
    df["implied_rewarrant_t"] = (df["uncancelled_t"] - df["withdrawals_t"].fillna(0)).clip(lower=0)

    ratio = df["withdrawals_t"] / df["new_cancellations_t"].where(df["new_cancellations_t"] > 0)
    df["cancellation_drawdown_ratio"] = ratio.astype("float64")

    df["cancelled_share"] = cancelled_share(df["on_warrant_t"], df["cancelled_t"])
    df["d_cancelled_share"] = (
        df.groupby(key, dropna=False)["cancelled_share"].diff() if key
        else df["cancelled_share"].diff()
    )

    quiet = df["withdrawals_t"].fillna(0) <= loadout_tol_t
    df["is_rewarranting"] = (
        (df["implied_rewarrant_t"] > rewarrant_min_t)
        & (df["d_on_warrant_t"] > 0)
        & (df["d_cancelled_t"] < 0)
        & quiet
    )
    df["phantom_tightness"] = (
        (df["d_cancelled_t"] > rewarrant_min_t)
        & (df["d_on_warrant_t"] < 0)
        & quiet
    )

    front = [*key, "report_date", "on_warrant_t", "cancelled_t", "closing_t",
             "delivered_in_t", "delivered_out_t"]
    rest = [c for c in df.columns if c not in front]
    return df[front + rest].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 2. Statistical anomaly detection
# --------------------------------------------------------------------------- #
def _wlabel(window: int | str) -> str:
    s = str(window).strip().lower()
    return s if s.endswith("d") else f"{s}d"


def rolling_zscore(
    s: pd.Series, window: int | str, *, min_periods: int | None = None
) -> pd.Series:
    """Rolling Z-score ``(x - mean) / std`` (population std, ``ddof=0``).

    ``window`` is a span in **calendar days** (``30`` or ``"30D"``).  If ``s`` has
    a ``DatetimeIndex`` the window is time-based, so irregular report cadence
    (holidays, missed scrapes) is handled correctly; otherwise it falls back to a
    fixed observation count.  ``std == 0`` windows yield ``NaN`` (no spurious
    infinite alerts).
    """
    s = pd.Series(s, dtype="float64").dropna()
    if s.empty:
        return pd.Series(dtype="float64")
    if isinstance(s.index, pd.DatetimeIndex):
        s = s.sort_index()
        s = s[~s.index.duplicated(keep="last")]
        roll = s.rolling(f"{int(str(window).rstrip('Dd'))}D", min_periods=min_periods or 2)
    else:
        w = int(str(window).rstrip("Dd"))
        roll = s.rolling(w, min_periods=min_periods or max(2, w // 3))
    mu = roll.mean()
    sd = roll.std(ddof=0)
    return ((s - mu) / sd.where(sd > 0)).astype("float64")


def zscore_frame(
    s: pd.Series,
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    threshold: float = ALERT_Z,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """``value`` + a ``z<w>d`` column and ``alert_<w>d`` bool per window, plus a
    combined ``alert`` (``abs(z) > threshold`` on any window)."""
    s = pd.Series(s, dtype="float64")
    if isinstance(s.index, pd.DatetimeIndex):
        s = s.sort_index()
        s = s[~s.index.duplicated(keep="last")]
    out = pd.DataFrame({"value": s})
    alert_cols: list[str] = []
    for w in windows:
        lab = _wlabel(w)
        z = rolling_zscore(s, w, min_periods=min_periods).reindex(s.index)
        out[f"z{lab}"] = z
        out[f"alert_{lab}"] = z.abs() > threshold
        alert_cols.append(f"alert_{lab}")
    out["alert"] = out[alert_cols].any(axis=1)
    return out


def _spread_series(runs: pd.DataFrame) -> pd.Series:
    """LME cash − 3-month spread indexed by pricing date, filled from the raw
    cash/3M legs where the stored spread column is null."""
    df = runs.copy()
    spread = pd.to_numeric(df.get("lme_cash_3m_spread_usd_t"), errors="coerce")
    cash = pd.to_numeric(df.get("lme_copper_cash_usd_t"), errors="coerce")
    m3 = pd.to_numeric(df.get("lme_copper_3m_usd_t"), errors="coerce")
    if cash is not None and m3 is not None:
        spread = spread.fillna(cash - m3)
    idx = pd.to_datetime(
        df.get("lme_price_date").fillna(df["run_date"])
        if "lme_price_date" in df else df["run_date"],
        errors="coerce",
    )
    return (
        pd.Series(spread.values, index=idx, name="cash_3m_spread_usd_mt")
        .dropna().sort_index().pipe(lambda x: x[~x.index.duplicated(keep="last")])
    )


def anomaly_scan(
    runs: pd.DataFrame,
    geo: pd.DataFrame | None = None,
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    threshold: float = ALERT_Z,
) -> dict[str, pd.DataFrame]:
    """Bundle the three anomaly domains.  Each value is a long frame indexed by
    date with the :func:`zscore_frame` columns; the geo domains also carry a
    ``location`` column (plus a ``GLOBAL`` roll-up).

    Keys: ``net_cancellations`` (daily ``d_cancelled_t`` by location),
    ``load_outs`` (``delivered_out_t`` by location), ``cash_3m_spread``.
    """
    result: dict[str, pd.DataFrame] = {}

    if geo is not None and not geo.empty:
        loc = location_warrant_flows(geo, level="location")
        glob = location_warrant_flows(geo, level="global")
        for key, src_col in (("net_cancellations", "d_cancelled_t"),
                             ("load_outs", "delivered_out_t")):
            parts: list[pd.DataFrame] = []
            if not loc.empty:
                for name, g in loc.groupby("location", dropna=False):
                    ser = pd.Series(
                        g[src_col].values, index=pd.to_datetime(g["report_date"])
                    )
                    zf = zscore_frame(ser, windows=windows, threshold=threshold)
                    zf.insert(0, "location", name)
                    parts.append(zf)
            if not glob.empty:
                ser = pd.Series(
                    glob[src_col].values, index=pd.to_datetime(glob["report_date"])
                )
                zf = zscore_frame(ser, windows=windows, threshold=threshold)
                zf.insert(0, "location", "GLOBAL")
                parts.append(zf)
            result[key] = (
                pd.concat(parts).rename_axis("date").reset_index()
                if parts else pd.DataFrame()
            )
    else:
        result["net_cancellations"] = pd.DataFrame()
        result["load_outs"] = pd.DataFrame()

    spread = _spread_series(runs)
    result["cash_3m_spread"] = (
        zscore_frame(spread, windows=windows, threshold=threshold)
        .rename_axis("date").reset_index()
        if not spread.empty else pd.DataFrame()
    )
    return result


# --------------------------------------------------------------------------- #
# 3. Arbitrage hurdle modelling
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ArbCostBand:
    """Configurable cost of moving an LME-deliverable tonne onto COMEX.

    ``freight_usd_mt``           ocean + inland freight, $/mt.
    ``finance_insurance_usd_mt`` financing of the in-transit cargo + insurance, $/mt.
    ``tariff_pct``               import duty / customs, percent of the metal value.
    ``tariff_basis``             which price the duty is levied on:
                                 ``"lme_3m"`` (default), ``"cme"`` or ``"none"``.
    ``extra_usd_mt``             any other flat handling/brokerage, $/mt.
    """

    freight_usd_mt: float = 120.0
    finance_insurance_usd_mt: float = 25.0
    tariff_pct: float = 0.0
    tariff_basis: str = "lme_3m"
    extra_usd_mt: float = 0.0

    def duty_usd_mt(
        self, *, lme_3m_price_mt=None, cme_price_mt=None
    ):
        if not self.tariff_pct or self.tariff_basis == "none":
            base = lme_3m_price_mt if lme_3m_price_mt is not None else cme_price_mt
            return base * 0.0 if isinstance(base, pd.Series) else 0.0
        basis = {"lme_3m": lme_3m_price_mt, "cme": cme_price_mt}.get(self.tariff_basis)
        if basis is None:
            raise ValueError(f"tariff_basis={self.tariff_basis!r} needs that price supplied")
        return basis * (self.tariff_pct / 100.0)

    def transfer_cost(self, *, lme_3m_price_mt=None, cme_price_mt=None):
        """Total $/mt hurdle: freight + finance/insurance + duty + extras."""
        flat = self.freight_usd_mt + self.finance_insurance_usd_mt + self.extra_usd_mt
        return flat + self.duty_usd_mt(lme_3m_price_mt=lme_3m_price_mt, cme_price_mt=cme_price_mt)


def net_arb_margin(
    cme_price_mt: float | pd.Series,
    lme_3m_price_mt: float | pd.Series,
    band: ArbCostBand | None = None,
) -> float | pd.Series:
    """``(cme_price_mt - lme_3m_price_mt) - band.transfer_cost(...)``.

    Positive ⇒ the physical arbitrage clears its costs.
    """
    band = band or ArbCostBand()
    gross = cme_price_mt - lme_3m_price_mt
    cost = band.transfer_cost(lme_3m_price_mt=lme_3m_price_mt, cme_price_mt=cme_price_mt)
    return gross - cost


def classify_arb_regime(
    gross_spread: float | pd.Series, net_margin: float | pd.Series
) -> str | pd.Series:
    """"Open Physical Arb" (net > 0), "Paper Dislocation" (gross > 0 but net ≤ 0),
    else "No Dislocation"."""
    if isinstance(gross_spread, pd.Series) or isinstance(net_margin, pd.Series):
        gs = pd.Series(gross_spread)
        nm = pd.Series(net_margin)
        return pd.Series(
            np.select(
                [nm > 0, gs > 0],
                ["Open Physical Arb", "Paper Dislocation"],
                default="No Dislocation",
            ),
            index=gs.index if gs.index.size else nm.index,
            name="regime",
        )
    if net_margin > 0:
        return "Open Physical Arb"
    if gross_spread > 0:
        return "Paper Dislocation"
    return "No Dislocation"


def arb_hurdle_frame(
    runs: pd.DataFrame,
    *,
    band: ArbCostBand | None = None,
    cme_col: str = "comex_copper_usd_t",
    lme_col: str = "lme_copper_3m_usd_t",
) -> pd.DataFrame:
    """Per-run arb decomposition indexed by ``run_date``:
    ``cme_price_mt, lme_3m_price_mt, gross_spread_usd_mt, transfer_cost_usd_mt,
    net_arb_margin_usd_mt, regime``.
    """
    band = band or ArbCostBand()
    if runs is None or runs.empty or cme_col not in runs or lme_col not in runs:
        return pd.DataFrame()
    df = runs[["run_date", cme_col, lme_col]].copy()
    df["run_date"] = pd.to_datetime(df["run_date"], errors="coerce")
    df[cme_col] = pd.to_numeric(df[cme_col], errors="coerce")
    df[lme_col] = pd.to_numeric(df[lme_col], errors="coerce")
    df = df.dropna(subset=["run_date", cme_col, lme_col]).rename(
        columns={cme_col: "cme_price_mt", lme_col: "lme_3m_price_mt"}
    )
    if df.empty:
        return pd.DataFrame()
    df["gross_spread_usd_mt"] = df["cme_price_mt"] - df["lme_3m_price_mt"]
    df["transfer_cost_usd_mt"] = band.transfer_cost(
        lme_3m_price_mt=df["lme_3m_price_mt"], cme_price_mt=df["cme_price_mt"]
    )
    df["net_arb_margin_usd_mt"] = df["gross_spread_usd_mt"] - df["transfer_cost_usd_mt"]
    df["regime"] = classify_arb_regime(df["gross_spread_usd_mt"], df["net_arb_margin_usd_mt"])
    return df.set_index("run_date").sort_index()


# --------------------------------------------------------------------------- #
# Spatial concentration + physical-response + diagnostics (dashboard helpers)
# --------------------------------------------------------------------------- #
HUBS = ["Singapore", "Rotterdam", "Busan", "Port Klang", "US", "Other"]
_HUB_BY_LOCATION = {"Singapore": "Singapore", "Rotterdam": "Rotterdam",
                    "Busan": "Busan", "Port Klang": "Port Klang"}
_TRANSPACIFIC_HUBS = {"US", "Busan", "Singapore", "Port Klang"}


def hub_of(region, location) -> str:
    """Bucket an LME delivery point into one of :data:`HUBS`.

    Named hub by exact ``location`` (Singapore / Rotterdam / Busan / Port Klang),
    ``"US"`` for anything in the USA, else ``"Other"``.
    """
    loc = "" if location is None or (isinstance(location, float) and np.isnan(location)) else str(location).strip()
    reg = "" if region is None or (isinstance(region, float) and np.isnan(region)) else str(region).strip()
    if loc in _HUB_BY_LOCATION:
        return _HUB_BY_LOCATION[loc]
    if reg.upper() in {"USA", "US", "UNITED STATES"}:
        return "US"
    return "Other"


def _latest_breakdown(geo: pd.DataFrame, report_date=None) -> pd.DataFrame:
    if geo is None or geo.empty:
        return pd.DataFrame()
    bd = geo.loc[geo["report_type"] == "breakdown"].copy()
    if bd.empty:
        return bd
    bd["report_date"] = pd.to_datetime(bd["report_date"], errors="coerce")
    rd = pd.to_datetime(report_date) if report_date is not None else bd["report_date"].max()
    out = bd.loc[bd["report_date"] == rd].copy()
    for c in ("on_warrant_t", "cancelled_t", "closing_t"):
        out[c] = pd.to_numeric(out.get(c), errors="coerce")
    out["hub"] = [hub_of(r, l) for r, l in zip(out.get("region"), out.get("location"))]
    return out


def hub_warrant_status(geo: pd.DataFrame, *, report_date=None) -> pd.DataFrame:
    """On-warrant vs cancelled stocks per hub for the latest (or given) LME
    breakdown report.  Columns: ``hub, on_warrant_t, cancelled_t, total_t,
    cancelled_share`` (%), ordered by ``total_t`` descending.  Drives Chart 1.
    """
    out = _latest_breakdown(geo, report_date)
    if out.empty:
        return pd.DataFrame(columns=["hub", "on_warrant_t", "cancelled_t",
                                     "total_t", "cancelled_share", "report_date"])
    rd = out["report_date"].iloc[0]
    g = (out.groupby("hub", as_index=False)[["on_warrant_t", "cancelled_t"]]
         .sum(min_count=1))
    g["total_t"] = g["on_warrant_t"].fillna(0) + g["cancelled_t"].fillna(0)
    g["cancelled_share"] = cancelled_share(g["on_warrant_t"], g["cancelled_t"])
    g["report_date"] = rd
    g["hub"] = pd.Categorical(g["hub"], categories=HUBS, ordered=True)
    return g.sort_values("total_t", ascending=False).reset_index(drop=True)


def cancellation_concentration(geo: pd.DataFrame, *, report_date=None) -> dict:
    """Where the cancelled tonnage sits in the latest LME breakdown:
    ``{top_location, top_cancelled_t, global_cancelled_t, top_share_pct,
    top_hub, top_hub_cancelled_t, top_hub_share_pct}``.  Empty dict if no data.
    """
    out = _latest_breakdown(geo, report_date)
    if out.empty or out["cancelled_t"].fillna(0).sum() <= 0:
        return {}
    total = float(out["cancelled_t"].sum(min_count=1))
    by_loc = out.groupby("location")["cancelled_t"].sum(min_count=1).sort_values(ascending=False)
    by_hub = out.groupby("hub")["cancelled_t"].sum(min_count=1).sort_values(ascending=False)
    top_loc, top_loc_t = by_loc.index[0], float(by_loc.iloc[0])
    top_hub, top_hub_t = str(by_hub.index[0]), float(by_hub.iloc[0])
    return {
        "report_date": out["report_date"].iloc[0].date(),
        "top_location": str(top_loc),
        "top_cancelled_t": top_loc_t,
        "global_cancelled_t": total,
        "top_share_pct": round(top_loc_t / total * 100.0, 1),
        "top_hub": top_hub,
        "top_hub_cancelled_t": top_hub_t,
        "top_hub_share_pct": round(top_hub_t / total * 100.0, 1),
    }


def loadout_response(
    geo: pd.DataFrame,
    *,
    level: str = "global",
    lag_bdays: int = 10,
    spike_z: float = 2.0,
    spike_min_t: float = 1000.0,
    met_fraction: float = 0.5,
) -> pd.DataFrame:
    """For each **cancellation spike**, did physical metal actually leave within
    ``lag_bdays`` trading days?

    A row of :func:`location_warrant_flows` is a spike when ``new_cancellations_t``
    is a rolling-Z outlier (``> spike_z``) *or* ≥ ``spike_min_t``.  For each spike
    we sum ``withdrawals_t`` (Delivered-Out) over ``(report_date,
    report_date + lag_bdays business days]`` and compare with the spike size.

    Columns: ``report_date, new_cancellations_t, loadout_within_lag_t, unmet_t,
    responded`` (``loadout ≥ met_fraction × spike``).  ``responded == False`` rows
    are the "paper hold / re-warranting" markers on Chart 2.  Empty with < 2
    reports.
    """
    flows = location_warrant_flows(geo, level=level)
    if flows.empty or "report_date" not in flows:
        return pd.DataFrame(columns=["report_date", "new_cancellations_t",
                                     "loadout_within_lag_t", "unmet_t", "responded"])
    key = _GEO_LEVELS.get(level, [])
    frames: list[pd.DataFrame] = []
    groups = flows.groupby(key, dropna=False) if key else [((), flows)]
    for _, g in groups:
        g = g.sort_values("report_date")
        nc = pd.Series(g["new_cancellations_t"].values,
                       index=pd.to_datetime(g["report_date"]))
        wd = pd.Series(g["withdrawals_t"].fillna(0).values,
                       index=pd.to_datetime(g["report_date"]))
        z = rolling_zscore(nc, 90, min_periods=3).reindex(nc.index)
        spike = ((z > spike_z) | (nc >= spike_min_t)) & (nc > 0)
        for ts, is_spike in spike.items():
            if not is_spike:
                continue
            end = ts + pd.tseries.offsets.BDay(lag_bdays)
            got = float(wd[(wd.index > ts) & (wd.index <= end)].sum())
            size = float(nc.loc[ts])
            rec = {"report_date": ts, "new_cancellations_t": size,
                   "loadout_within_lag_t": got, "unmet_t": max(0.0, size - got),
                   "responded": got >= met_fraction * size}
            if key:
                for k, v in zip(key, g[key].iloc[0]):
                    rec[k] = v
            frames.append(pd.DataFrame([rec]))
    if not frames:
        return pd.DataFrame(columns=["report_date", "new_cancellations_t",
                                     "loadout_within_lag_t", "unmet_t", "responded"])
    return pd.concat(frames, ignore_index=True).sort_values("report_date").reset_index(drop=True)


def net_draw_rate(
    runs: pd.DataFrame, *, col: str = "global_reported_stock_t"
) -> pd.Series:
    """Business-day rate of change of ``col`` on the exchange-native as-of series
    (real report points only, so a stale/carried leg cannot inject a spike).
    Negative = inventory drawing down.  Indexed by date; name ``net_draw_rate``.
    """
    if runs is None or runs.empty:
        return pd.Series(dtype="float64", name="net_draw_rate")
    native = build_native_asof_series(runs)
    if native.empty or col not in native:
        return pd.Series(dtype="float64", name="net_draw_rate")
    s = pd.Series(pd.to_numeric(native[col], errors="coerce").values,
                  index=pd.to_datetime(native["date"])).dropna().sort_index()
    if len(s) < 2:
        return pd.Series(dtype="float64", name="net_draw_rate")
    dv = s.diff()
    dd = s.index.to_series().diff().dt.days.replace(0, np.nan)
    bdays = (dd * 5.0 / 7.0).clip(lower=1.0)  # calendar days -> ~business days
    return (dv / bdays).dropna().rename("net_draw_rate")


def _interpret_anomaly(row: pd.Series, *, region_hits: Counter, hub_hits: Counter,
                       arb_regime: str | None) -> str:
    cz = float(row.get("cancel_z") or 0.0)
    lz = float(row.get("loadout_z") or 0.0)
    rw = int(row.get("rewarrant_events") or 0)
    region = row.get("region")
    hub = row.get("hub")
    if rw > 0 and lz <= ALERT_Z:
        # a re-warranting event already implies near-zero load-out at that report;
        # only a genuine load-out *alert* overrides the "paper hold" reading.
        return "Re-warranting / Paper Hold"
    if cz > 2.0 and lz <= 1.0 and region_hits.get(region, 0) <= 1:
        return "Isolated Cancellation - Low Physical Drain"
    if cz > 2.0 and (region_hits.get(region, 0) >= 2 or hub_hits.get(hub, 0) >= 2):
        return "Broad Regional Tightening"
    if lz > 2.0 and hub in _TRANSPACIFIC_HUBS and arb_regime == "Open Physical Arb":
        return "Transpacific Arb Delivery Candidate"
    if lz > 2.0:
        return "Active Physical Load-Out"
    return "Watch"


def diagnose_anomalies(
    runs: pd.DataFrame,
    geo: pd.DataFrame | None = None,
    *,
    band: ArbCostBand | None = None,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    threshold: float = ALERT_Z,
    lookback_reports: int = 6,
) -> pd.DataFrame:
    """Per-location diagnostics table (Part 5 of the scarcity page).

    One row per location that currently trips an ``|Z| > threshold`` alert on
    net cancellations or load-outs, or has a re-warranting event in the last
    ``lookback_reports`` reports.  Columns: ``location, hub, region, cancel_z,
    loadout_z, rewarrant_events, interpretation`` (see :func:`_interpret_anomaly`
    for the tag rules).  Empty frame when nothing is alerting.
    """
    cols = ["location", "hub", "region", "cancel_z", "loadout_z",
            "rewarrant_events", "interpretation"]
    if geo is None or geo.empty:
        return pd.DataFrame(columns=cols)

    scan = anomaly_scan(runs, geo, windows=windows, threshold=threshold)

    def _latest_abs_z(frame: pd.DataFrame) -> dict[str, float]:
        if frame is None or frame.empty or "location" not in frame:
            return {}
        zc = [c for c in frame.columns if c.startswith("z") and c[1:2].isdigit()]
        res: dict[str, float] = {}
        for name, g in frame.groupby("location", dropna=False):
            if name == "GLOBAL":
                continue
            last = g.sort_values("date").iloc[-1]
            arr = np.abs(last[zc].to_numpy(dtype="float64")) if zc else np.array([])
            res[name] = 0.0 if arr.size == 0 or np.all(np.isnan(arr)) else float(np.nanmax(arr))
        return res

    cancel_z = _latest_abs_z(scan.get("net_cancellations"))
    loadout_z = _latest_abs_z(scan.get("load_outs"))

    per_loc = location_warrant_flows(geo, level="location")
    rewarr: dict[str, int] = {}
    region_of: dict[str, str] = {}
    if not per_loc.empty:
        cutoff = per_loc["report_date"].sort_values().unique()
        cutoff = cutoff[-lookback_reports] if len(cutoff) > lookback_reports else cutoff[0]
        recent = per_loc[per_loc["report_date"] >= cutoff]
        rewarr = recent.groupby("location")["is_rewarranting"].sum().astype(int).to_dict()
        region_of = (per_loc.sort_values("report_date")
                     .groupby("location")["region"].last().to_dict())

    names = set(cancel_z) | set(loadout_z) | {k for k, v in rewarr.items() if v}
    recs = []
    for n in names:
        cz, lz, rw = cancel_z.get(n, 0.0), loadout_z.get(n, 0.0), int(rewarr.get(n, 0))
        if not (cz > threshold or lz > threshold or rw > 0):
            continue
        reg = region_of.get(n)
        recs.append({"location": n, "hub": hub_of(reg, n), "region": reg,
                     "cancel_z": round(cz, 2), "loadout_z": round(lz, 2),
                     "rewarrant_events": rw})
    if not recs:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(recs)
    region_hits = Counter(df.loc[df["cancel_z"] > threshold, "region"])
    hub_hits = Counter(df.loc[df["cancel_z"] > threshold, "hub"])
    arb = arb_hurdle_frame(runs, band=band or ArbCostBand())
    arb_regime = str(arb.iloc[-1]["regime"]) if not arb.empty else None
    df["interpretation"] = df.apply(
        _interpret_anomaly, axis=1,
        region_hits=region_hits, hub_hits=hub_hits, arb_regime=arb_regime,
    )
    df["_sev"] = df[["cancel_z", "loadout_z"]].max(axis=1) + df["rewarrant_events"]
    return df.sort_values("_sev", ascending=False).drop(columns="_sev")[cols].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Combined verdict
# --------------------------------------------------------------------------- #
@dataclass
class ScarcityScorecard:
    asof: dt.date | None
    score: float                     # -1 = reshuffling/financing ... +1 = physical scarcity
    verdict: str
    signals: dict[str, float]
    rationale: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "asof": self.asof.isoformat() if self.asof else None,
            "score": round(self.score, 3),
            "verdict": self.verdict,
            "signals": {k: (round(v, 3) if isinstance(v, float) else v)
                        for k, v in self.signals.items()},
            "rationale": self.rationale,
        }


def scarcity_scorecard(
    runs: pd.DataFrame,
    geo: pd.DataFrame | None = None,
    *,
    band: ArbCostBand | None = None,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    lookback_reports: int = 6,
) -> ScarcityScorecard:
    """Fold the three layers into one signed read.

    Each signal nudges the score by ±0.25; the sum is clipped to [-1, 1].
    ``score > 0.25`` ⇒ "Physical scarcity", ``< -0.25`` ⇒ "Warehouse reshuffling
    / financing", otherwise "Mixed / inconclusive".  With little history most
    inputs are ``NaN`` and the verdict is honestly inconclusive.
    """
    band = band or ArbCostBand()
    signals: dict[str, float] = {}
    rationale: list[str] = []
    score = 0.0
    asof: dt.date | None = None

    glob = location_warrant_flows(geo, level="global") if geo is not None else pd.DataFrame()
    per_loc = location_warrant_flows(geo, level="location") if geo is not None else pd.DataFrame()
    if not glob.empty:
        asof = pd.to_datetime(glob["report_date"].max()).date()
        recent = glob.tail(lookback_reports)
        cs = float(recent["cancelled_share"].iloc[-1])
        cs_trend = float(recent["cancelled_share"].iloc[-1] - recent["cancelled_share"].iloc[0])
        conv = float(
            recent["withdrawals_t"].sum(min_count=1)
            / recent["new_cancellations_t"].replace(0, np.nan).sum(min_count=1)
        ) if recent["new_cancellations_t"].sum(min_count=1) else float("nan")
        # re-warranting / phantom tightness only show up *per location* — an
        # aggregate hides a shed re-warranting behind another shed's load-out.
        cutoff = recent["report_date"].min()
        pl_recent = per_loc[per_loc["report_date"] >= cutoff] if not per_loc.empty else per_loc
        rewarr = int(pl_recent["is_rewarranting"].sum()) if not pl_recent.empty else 0
        phantom = int(pl_recent["phantom_tightness"].sum()) if not pl_recent.empty else 0
        signals.update(
            cancelled_share_pct=cs, cancelled_share_trend_pp=cs_trend,
            cancellation_to_loadout_ratio=conv,
            rewarranting_events=rewarr, phantom_tightness_events=phantom,
        )
        if not np.isnan(conv):
            if conv >= 0.8:
                score += 0.25
                rationale.append(f"cancelled metal is actually loading out (ratio {conv:.2f}) → real")
            elif conv <= 0.3:
                score -= 0.25
                rationale.append(f"cancelled metal is not leaving (ratio {conv:.2f}) → paper")
        if cs_trend > 3 and phantom:
            score -= 0.25
            rationale.append(
                f"cancelled share +{cs_trend:.1f}pp with load-out near zero → warrant re-labelling"
            )
        if rewarr:
            score -= 0.25
            rationale.append(f"{rewarr} re-warranting event(s) in last {lookback_reports} reports → reshuffling")

    scan = anomaly_scan(runs, geo, windows=windows)
    lo = scan.get("load_outs")
    if lo is not None and not lo.empty:
        g = lo[lo["location"] == "GLOBAL"]
        if not g.empty:
            zc = [c for c in g.columns if c.startswith("z")]
            z_last = float(g.iloc[-1][zc].abs().max()) if zc else float("nan")
            signals["loadout_z_abs_max"] = z_last
            if not np.isnan(z_last) and z_last > ALERT_Z and g.iloc[-1]["value"] > 0:
                score += 0.25
                rationale.append(f"physical load-out spike (|z|={z_last:.1f}) → scarcity")

    arb = arb_hurdle_frame(runs, band=band)
    if not arb.empty:
        last = arb.iloc[-1]
        signals["arb_regime"] = last["regime"]
        signals["net_arb_margin_usd_mt"] = float(last["net_arb_margin_usd_mt"])
        asof = asof or arb.index.max().date()
        if last["regime"] == "Open Physical Arb":
            score += 0.25
            rationale.append(
                f"CME–LME arb clears costs (net +{last['net_arb_margin_usd_mt']:.0f} $/mt) → pull on metal"
            )
        elif last["regime"] == "Paper Dislocation":
            score -= 0.25
            rationale.append(
                f"CME–LME spread inside the transfer-cost band "
                f"(net {last['net_arb_margin_usd_mt']:.0f} $/mt) → paper dislocation"
            )

    score = float(np.clip(score, -1.0, 1.0))
    verdict = (
        "Physical scarcity" if score > 0.25
        else "Warehouse reshuffling / financing" if score < -0.25
        else "Mixed / inconclusive"
    )
    if not rationale:
        rationale.append("insufficient history for a confident read")
    return ScarcityScorecard(asof=asof, score=score, verdict=verdict,
                             signals=signals, rationale=rationale)


# --------------------------------------------------------------------------- #
# IO helpers + CLI
# --------------------------------------------------------------------------- #
def load_frames(
    parquet=DEFAULT_PARQUET, geo_parquet=DEFAULT_GEO_PARQUET
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the run log and the geo breakdown (empty frame if the geo file is absent)."""
    runs = pd.read_parquet(parquet)
    geo = pd.read_parquet(geo_parquet) if Path(geo_parquet).exists() else pd.DataFrame()
    return runs, geo


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Copper scarcity vs. reshuffling analytics")
    p.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    p.add_argument("--geo-parquet", type=Path, default=DEFAULT_GEO_PARQUET)
    p.add_argument("--freight", type=float, default=120.0, help="freight $/mt")
    p.add_argument("--finance", type=float, default=25.0, help="finance + insurance $/mt")
    p.add_argument("--tariff-pct", type=float, default=0.0, help="import duty %% of LME 3M price")
    args = p.parse_args(argv)

    runs, geo = load_frames(args.parquet, args.geo_parquet)
    band = ArbCostBand(freight_usd_mt=args.freight,
                       finance_insurance_usd_mt=args.finance,
                       tariff_pct=args.tariff_pct)

    print(f"runs: {len(runs)} rows   geo: {len(geo)} rows\n")

    glob = location_warrant_flows(geo, level="global")
    if not glob.empty:
        cols = ["report_date", "on_warrant_t", "cancelled_t", "cancelled_share",
                "new_cancellations_t", "withdrawals_t", "implied_rewarrant_t",
                "is_rewarranting", "phantom_tightness"]
        print("-- global warrant lifecycle --")
        print(glob[cols].to_string(index=False), "\n")

    hubs = hub_warrant_status(geo)
    if not hubs.empty:
        print("-- LME warrant status by hub --")
        print(hubs[["hub", "on_warrant_t", "cancelled_t", "total_t",
                    "cancelled_share"]].to_string(index=False), "\n")
        conc = cancellation_concentration(geo)
        if conc:
            print(f"   cancellation concentration: {conc['top_location']} holds "
                  f"{conc['top_share_pct']}% of global cancellations "
                  f"({conc['top_hub']} hub {conc['top_hub_share_pct']}%)\n")

    arb = arb_hurdle_frame(runs, band=band)
    if not arb.empty:
        print("-- CME-LME arb hurdle --")
        print(arb[["gross_spread_usd_mt", "transfer_cost_usd_mt",
                   "net_arb_margin_usd_mt", "regime"]].tail(10).to_string(), "\n")

    diag = diagnose_anomalies(runs, geo, band=band)
    print(f"-- diagnose_anomalies: {len(diag)} location(s) alerting --")
    if not diag.empty:
        print(diag.to_string(index=False), "\n")

    scan = anomaly_scan(runs, geo)
    for name, frame in scan.items():
        n_alert = int(frame["alert"].sum()) if not frame.empty and "alert" in frame else 0
        print(f"anomaly[{name}]: {0 if frame.empty else len(frame)} rows, {n_alert} alert(s)")

    print("\n-- scarcity scorecard --")
    print(json.dumps(scarcity_scorecard(runs, geo, band=band).as_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
