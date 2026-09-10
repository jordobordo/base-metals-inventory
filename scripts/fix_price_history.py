"""
One-off: re-align the historical CME-LME price spread in
``data/copper_inventory.parquet``.

Older rows have two defects (see the spread discussion / commit history):

  1. **Cross-session spread** — the stored ``cme_lme_spread_*`` compared a COMEX
     settlement from one session against an LME 3-month from a *different*
     session (the COMEX feed was frozen while the LME leg advanced), which
     collapsed the printed spread to a fraction of the real market-on-close gap.
  2. **Mixed LME source** — early rows took the LME 3-month from lme.com's
     *Closing* price; the pipeline now uses Westmetall's *Official* 3-month.

This script rebuilds the LME leg for every priced row **as of that row's own
``comex_price_date``**, from the Westmetall ``LME_Cu_cash`` history (fetched
once), and recomputes:

    lme_copper_cash_usd_t, lme_copper_3m_usd_t, lme_price_date,
    lme_cash_3m_spread_usd_t, cme_lme_spread_usd_t, cme_lme_spread_3m_usd_t

COMEX columns and every non-price column are left untouched. Rows without a
COMEX price (the first days) are skipped.

    python scripts/fix_price_history.py [--parquet PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts.aggregate import DEFAULT_PARQUET  # noqa: E402
from scripts.price_scraper import (  # noqa: E402
    WESTMETALL_LME_CU, _fetch_westmetall, _parse_westmetall,
)
from scripts.schema import SCHEMA  # noqa: E402

log = logging.getLogger("fix_price_history")

_FIX_COLS = [
    "lme_copper_cash_usd_t", "lme_copper_3m_usd_t", "lme_price_date",
    "lme_cash_3m_spread_usd_t", "cme_lme_spread_usd_t", "cme_lme_spread_3m_usd_t",
]


def _westmetall_history() -> list[tuple[dt.date, float, float | None]]:
    rows = _parse_westmetall(_fetch_westmetall(WESTMETALL_LME_CU))  # newest first
    if not rows:
        raise RuntimeError("Westmetall returned no LME rows")
    log.info("Westmetall LME history: %s .. %s (%d rows)",
             rows[-1][0], rows[0][0], len(rows))
    return rows


def _asof(history: list[tuple[dt.date, float, float | None]], day: dt.date):
    """Newest Westmetall row on/before ``day`` -> (date, cash, three_month)."""
    for d, cash, m3 in history:  # history is newest-first
        if d <= day:
            return d, cash, m3
    return None


def fix_frame(df: pd.DataFrame, history) -> tuple[pd.DataFrame, list[dict]]:
    out = df.copy()
    changes: list[dict] = []
    for i, row in out.iterrows():
        cx_date = row.get("comex_price_date")
        cx_t = row.get("comex_copper_usd_t")
        if pd.isna(cx_date) or pd.isna(cx_t):
            continue
        cx_date = pd.to_datetime(cx_date).date()
        hit = _asof(history, cx_date)
        if hit is None:
            log.warning("no Westmetall LME row on/before %s (run %s) — skipped",
                        cx_date, row.get("run_date"))
            continue
        lme_date, cash, m3 = hit
        cx_t = float(cx_t)
        new = {
            "lme_copper_cash_usd_t": round(cash, 2),
            "lme_copper_3m_usd_t": round(m3, 2) if m3 is not None else None,
            "lme_price_date": pd.Timestamp(lme_date),
            "lme_cash_3m_spread_usd_t": round(cash - m3, 2) if m3 is not None else None,
            "cme_lme_spread_usd_t": round(cx_t - cash, 2),
            "cme_lme_spread_3m_usd_t": round(cx_t - m3, 2) if m3 is not None else None,
        }
        before = {c: row.get(c) for c in _FIX_COLS}
        for c, v in new.items():
            out.at[i, c] = v
        changes.append({
            "run_date": pd.to_datetime(row["run_date"]).date(),
            "comex_date": cx_date, "lme_date_old": before["lme_price_date"],
            "lme_date_new": lme_date,
            "spr3_old": before["cme_lme_spread_3m_usd_t"],
            "spr3_new": new["cme_lme_spread_3m_usd_t"],
            "sprC_old": before["cme_lme_spread_usd_t"],
            "sprC_new": new["cme_lme_spread_usd_t"],
        })
    return out, changes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    df = pd.read_parquet(args.parquet)
    history = _westmetall_history()
    fixed, changes = fix_frame(df, history)

    if not changes:
        print("no priced rows to correct.")
        return 0
    rep = pd.DataFrame(changes)
    for c in ("lme_date_old",):
        rep[c] = pd.to_datetime(rep[c]).dt.date
    print(rep.to_string(index=False))
    print(f"\n{len(changes)} row(s) re-aligned.")

    if args.dry_run:
        print("(--dry-run: parquet not written)")
        return 0

    fixed = fixed[[c for c in SCHEMA if c in fixed.columns]
                  + [c for c in fixed.columns if c not in SCHEMA]]
    fixed.to_parquet(args.parquet, index=False)
    print(f"wrote {args.parquet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
