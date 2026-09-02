"""
STEP 4 — the Aggregator.

Runs the three exchange scrapers, harmonises their output into one wide row (all
tonnes, one consistent on-warrant / cancelled / off-warrant schema), computes the
global total, and upserts the row into ``data/copper_inventory.parquet``.

    python scripts/aggregate.py                 # run everything, upsert today's row
    python scripts/aggregate.py --dry-run       # compute + print, don't write
    python scripts/aggregate.py --strict        # any scraper failure aborts
    python scripts/aggregate.py --no-shfe-daily # skip the daily-warrant side fetch

Harmonisation
-------------
Per exchange, mapped to (on-warrant, cancelled, off-warrant); ``total`` is their
sum and equals that exchange's own reported total:

    CME   on = Registered x 0.907185     cancelled = 0 (COMEX has no such concept)
          off = Eligible x 0.907185      total = Registered + Eligible (tonnes)

    LME   on = Open Tonnage (live)       cancelled = Cancelled Tonnage
          off = OWSR GLOBAL TOTAL (CU)   total = on + cancelled + off

    SHFE  on = 仓单 (futures warrants, weekly)
          cancelled = 库存 - 仓单  (implied non-warranted; NOT a real cancelled figure)
          off = NaN  (SHFE does not report off-warrant)
          total = 库存 (physical inventory, weekly)   [= on + cancelled]

    global_total = cme_total + lme_total + shfe_total
                 = global_on + global_cancelled + global_off   (off excludes SHFE)

Each exchange also carries its own ``*_data_date`` (the report's as-of date),
kept distinct from ``run_date`` (when this pipeline ran). Sources are inherently
forward-filled — every scraper returns its latest *available* report even if a
few days stale. If a scraper fails and ``--strict`` is not set, the previous
parquet row's values for that exchange are carried forward and ``*_stale`` is set
so the staleness stays visible.

Daily calendar / LOCF for charting is :func:`build_daily_series`, used by the
Streamlit app in STEP 5 — the parquet itself stays an append log of runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts.cme_scraper import CMEScraperError, get_cme_copper_stocks  # noqa: E402
from scripts.lme_scraper import (  # noqa: E402
    LMEScraperError,
    get_lme_copper_offwarrant,
    get_lme_copper_warrants,
)
from scripts.price_scraper import PriceScraperError, get_cme_lme_copper_spread  # noqa: E402
from scripts.schema import DATE_COLS, SCHEMA, build_daily_series  # noqa: E402,F401
from scripts.shfe_scraper import SHFEScraperError, get_shfe_copper_stocks  # noqa: E402

log = logging.getLogger("aggregate")

# User-specified conversion (precise value is 0.90718474; spec pins this one).
SHORT_TON_TO_TONNE = 0.907185

DEFAULT_PARQUET = _HERE.parent / "data" / "copper_inventory.parquet"

_DATE_COLS = DATE_COLS  # local alias; SCHEMA + build_daily_series come from scripts.schema


class AggregationError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Scraper runners (each returns a dict of harmonised fields, or raises)
# --------------------------------------------------------------------------- #
def _run_cme() -> dict[str, Any]:
    rec = get_cme_copper_stocks()
    reg_t = rec["registered_short_tons"] * SHORT_TON_TO_TONNE
    elig_t = rec["eligible_short_tons"] * SHORT_TON_TO_TONNE
    return {
        "cme_on_warrant_t": round(reg_t, 3),
        "cme_cancelled_t": 0.0,
        "cme_off_warrant_t": round(elig_t, 3),
        "cme_total_t": round(reg_t + elig_t, 3),
        "cme_data_date": rec["report_date"],
        "cme_registered_short_tons": rec["registered_short_tons"],
        "cme_eligible_short_tons": rec["eligible_short_tons"],
    }


def _run_lme_warrants() -> dict[str, Any]:
    rec = get_lme_copper_warrants()
    return {
        "lme_on_warrant_t": rec["live_warrant_tonnes"],
        "lme_cancelled_t": rec["cancelled_warrant_tonnes"],
        "lme_warrant_data_date": rec["report_date"],
        "_lme_closing_t": rec["total_on_warrant_tonnes"],  # internal, for the total
    }


def _run_lme_offwarrant() -> dict[str, Any]:
    rec = get_lme_copper_offwarrant()
    return {
        "lme_off_warrant_t": rec["off_warrant_tonnes"],
        "lme_offwarrant_data_date": rec["report_date"],
    }


def _run_shfe(enrich_with_daily: bool) -> dict[str, Any]:
    rec = get_shfe_copper_stocks(enrich_with_daily=enrich_with_daily)
    return {
        "shfe_on_warrant_t": rec["futures_warrant_tonnes"],
        "shfe_cancelled_t": rec["implied_non_warranted_tonnes"],
        "shfe_off_warrant_t": float("nan"),
        "shfe_total_t": rec["physical_inventory_tonnes"],
        "shfe_data_date": rec["report_date"],
        "shfe_warrant_daily_t": rec.get("futures_warrant_tonnes_daily", float("nan")),
        "shfe_warrant_daily_date": rec.get("futures_warrant_daily_date"),
    }


_PRICE_COLS = [
    "comex_copper_usd_lb", "comex_copper_usd_t", "comex_price_date",
    "lme_copper_cash_usd_t", "lme_copper_3m_usd_t", "lme_price_date",
    "cme_lme_spread_usd_t", "cme_lme_spread_3m_usd_t",
]


def _run_prices() -> dict[str, Any]:
    # Raises PriceScraperError only if BOTH legs fail (-> carried forward).
    # A single failed leg just leaves that leg's columns None for this run.
    rec = get_cme_lme_copper_spread()
    if rec.get("price_legs_failed"):
        log.warning("price: partial (%s ok, %s failed)",
                    rec.get("price_legs_ok"), rec.get("price_legs_failed"))
    return {c: rec.get(c) for c in _PRICE_COLS}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def collect(
    *,
    run_shfe_daily: bool = True,
    strict: bool = False,
    prev_row: pd.Series | None = None,
) -> dict[str, Any]:
    """Run every scraper and return one harmonised row dict.

    On a scraper failure: re-raise if ``strict`` else carry the previous row's
    values for that exchange forward (marking ``*_stale``).
    """
    row: dict[str, Any] = {k: None for k in SCHEMA}
    row["run_date"] = dt.date.today()
    row["retrieved_at"] = dt.datetime.now(dt.timezone.utc)
    row["cme_stale"] = row["lme_stale"] = row["lme_offwarrant_stale"] = False
    row["shfe_stale"] = row["price_stale"] = False

    ok: list[str] = []
    failed: list[str] = []
    notes: list[str] = []

    tasks: list[tuple[str, Callable[[], dict[str, Any]], type[Exception], list[str], str]] = [
        ("CME", _run_cme, CMEScraperError,
         ["cme_on_warrant_t", "cme_cancelled_t", "cme_off_warrant_t", "cme_total_t",
          "cme_data_date", "cme_registered_short_tons", "cme_eligible_short_tons"], "cme_stale"),
        ("LME", _run_lme_warrants, LMEScraperError,
         ["lme_on_warrant_t", "lme_cancelled_t", "lme_warrant_data_date"], "lme_stale"),
        ("LME_OWSR", _run_lme_offwarrant, LMEScraperError,
         ["lme_off_warrant_t", "lme_offwarrant_data_date"], "lme_offwarrant_stale"),
        ("SHFE", lambda: _run_shfe(run_shfe_daily), SHFEScraperError,
         ["shfe_on_warrant_t", "shfe_cancelled_t", "shfe_off_warrant_t", "shfe_total_t",
          "shfe_data_date", "shfe_warrant_daily_t", "shfe_warrant_daily_date"], "shfe_stale"),
        ("PRICES", _run_prices, PriceScraperError, _PRICE_COLS, "price_stale"),
    ]

    lme_closing_t: float | None = None
    for name, fn, exc_type, carry_cols, stale_flag in tasks:
        try:
            log.info("running %s scraper ...", name)
            result = fn()
            lme_closing_t = result.pop("_lme_closing_t", lme_closing_t)
            row.update(result)
            ok.append(name)
        except exc_type as exc:
            if strict:
                raise AggregationError(f"{name} scraper failed: {exc}") from exc
            failed.append(name)
            note = f"{name} failed ({type(exc).__name__}: {str(exc).splitlines()[0][:120]})"
            notes.append(note)
            log.warning(note)
            _carry_forward(row, prev_row, carry_cols, stale_flag)

    _compute_totals(row, lme_closing_t)
    row["sources_ok"] = ",".join(ok)
    row["sources_failed"] = ",".join(failed)
    row["notes"] = " | ".join(notes)
    return row


def _carry_forward(row: dict[str, Any], prev: pd.Series | None, cols: list[str], stale_flag: str) -> None:
    if prev is None:
        log.warning("no previous row to carry forward for %s", cols[0].split("_")[0].upper())
        return
    carried = 0
    for c in cols:
        if c in prev.index and pd.notna(prev[c]):
            row[c] = _coerce_from_prev(c, prev[c])
            carried += 1
    if carried:
        row[stale_flag] = True
    else:
        log.warning("previous row had no usable values to carry forward for %s",
                    cols[0].split("_")[0].upper())


def _coerce_from_prev(col: str, value: Any) -> Any:
    if col in _DATE_COLS and isinstance(value, pd.Timestamp):
        return value.date()
    return value


def _compute_totals(row: dict[str, Any], lme_closing_t: float | None) -> None:
    lme_on = _num(row.get("lme_on_warrant_t"))
    lme_can = _num(row.get("lme_cancelled_t"))
    lme_off = _num(row.get("lme_off_warrant_t"))

    # Each exchange's *_total_t is the figure public trackers headline:
    #   CME  -> TOTAL COPPER (Registered + Eligible)  [matches Reuters COMEX stocks]
    #   LME  -> Closing Stock (live + cancelled)      [matches Westmetall / LME site]
    #   SHFE -> 库存 physical inventory                [matches SMM]
    # LME off-warrant (OWSR) is a SEPARATE figure that no "LME stock" quote includes.
    row["lme_total_t"] = _round(
        lme_closing_t if lme_closing_t is not None else _add(lme_on, lme_can)
    )

    cme_total = _num(row.get("cme_total_t"))
    shfe_total = _num(row.get("shfe_total_t"))

    row["global_on_warrant_t"] = _round(_add(_num(row.get("cme_on_warrant_t")), lme_on,
                                             _num(row.get("shfe_on_warrant_t"))))
    row["global_cancelled_t"] = _round(_add(_num(row.get("cme_cancelled_t")), lme_can,
                                            _num(row.get("shfe_cancelled_t"))))
    # Off-warrant excludes SHFE (not reported).
    row["global_off_warrant_t"] = _round(_add(_num(row.get("cme_off_warrant_t")), lme_off))

    # "Reported stock" = sum of each exchange's own headline figure.
    row["global_reported_stock_t"] = _round(_add(cme_total, row["lme_total_t"], shfe_total))
    # Grand total per spec = on-warrant + cancelled + off-warrant = reported + LME off-warrant.
    row["global_total_t"] = _round(_add(row["global_reported_stock_t"], lme_off))


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _add(*vals: float | None) -> float | None:
    present = [v for v in vals if v is not None]
    return sum(present) if present else None


def _round(v: float | None, ndigits: int = 3) -> float | None:
    return None if v is None else round(v, ndigits)


# --------------------------------------------------------------------------- #
# Parquet persistence
# --------------------------------------------------------------------------- #
def _row_to_frame(row: dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame([{k: row.get(k) for k in SCHEMA}], columns=SCHEMA)
    for c in _DATE_COLS:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    df["retrieved_at"] = pd.to_datetime(df["retrieved_at"], utc=True, errors="coerce")
    for c in df.columns:
        if c.endswith(("_t", "_short_tons", "_usd_lb")):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        if c.endswith("_stale"):
            df[c] = df[c].astype("boolean")
    return df


def upsert_parquet(row: dict[str, Any], parquet_path: Path) -> pd.DataFrame:
    """Insert/replace the row for its ``run_date`` and write the parquet back."""
    new = _row_to_frame(row)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        for col in SCHEMA:
            if col not in existing.columns:
                existing[col] = pd.NA
        existing = existing[SCHEMA]
        run_day = new.loc[0, "run_date"]
        existing = existing[existing["run_date"] != run_day]
        combined = pd.concat([existing, new], ignore_index=True)
    else:
        combined = new

    combined = combined.sort_values("run_date").reset_index(drop=True)
    combined.to_parquet(parquet_path, index=False)
    log.info("wrote %s (%d rows)", parquet_path, len(combined))
    return combined


def load_latest_row(parquet_path: Path) -> pd.Series | None:
    if not parquet_path.exists():
        return None
    df = pd.read_parquet(parquet_path)
    if df.empty:
        return None
    return df.sort_values("run_date").iloc[-1]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt_summary(row: dict[str, Any]) -> str:
    def g(k: str) -> str:
        v = row.get(k)
        return f"{'n/a':>12}" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{v:>12,.0f}"

    return "\n".join([
        f"  run_date            {row['run_date']}   sources_ok={row['sources_ok'] or '-'}"
        f"  failed={row['sources_failed'] or '-'}",
        f"  {'':16} {'on-warrant':>12} {'cancelled':>12} {'off-warrant':>12} {'reported*':>12}   as-of",
        f"  CME              {g('cme_on_warrant_t')} {g('cme_cancelled_t')} {g('cme_off_warrant_t')} {g('cme_total_t')}   {row.get('cme_data_date')}",
        f"  LME              {g('lme_on_warrant_t')} {g('lme_cancelled_t')} {g('lme_off_warrant_t')} {g('lme_total_t')}   {row.get('lme_warrant_data_date')} / {row.get('lme_offwarrant_data_date')}",
        f"  SHFE             {g('shfe_on_warrant_t')} {g('shfe_cancelled_t')} {g('shfe_off_warrant_t')} {g('shfe_total_t')}   {row.get('shfe_data_date')}",
        f"  {'GLOBAL':16} {g('global_on_warrant_t')} {g('global_cancelled_t')} {g('global_off_warrant_t')} {g('global_reported_stock_t')}",
        f"  * reported = each exchange's headline figure; grand total incl. LME "
        f"off-warrant = {g('global_total_t').strip()}",
        f"  price  COMEX {g('comex_copper_usd_t').strip()} USD/t  LME cash "
        f"{g('lme_copper_cash_usd_t').strip()} USD/t  ->  CME-LME spread "
        f"{g('cme_lme_spread_usd_t').strip()} USD/t"
        f"   ({row.get('comex_price_date')} / {row.get('lme_price_date')})",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Aggregate global copper warehouse inventory.")
    p.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET, help="output parquet path")
    p.add_argument("--dry-run", action="store_true", help="compute and print, do not write")
    p.add_argument("--strict", action="store_true", help="abort if any scraper fails")
    p.add_argument("--no-shfe-daily", action="store_true", help="skip the SHFE daily-warrant side fetch")
    p.add_argument("--json", action="store_true", help="print the row as JSON too")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    prev = load_latest_row(args.parquet)
    row = collect(run_shfe_daily=not args.no_shfe_daily, strict=args.strict, prev_row=prev)

    print(_fmt_summary(row))
    if args.json:
        print(json.dumps(row, indent=2, default=str))

    if not row["sources_ok"]:
        log.error("every scraper failed; not writing parquet")
        return 2
    if args.dry_run:
        print("\n(--dry-run: parquet not written)")
        return 0

    upsert_parquet(row, args.parquet)
    print(f"\nupserted run_date={row['run_date']} -> {args.parquet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
