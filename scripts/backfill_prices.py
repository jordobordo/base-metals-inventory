"""
Build / refresh ``data/comex_lme_history.parquet`` — one row per **market
session** (a real COMEX settlement date) with the LME cash / 3-month from that
same session and the CME-LME spreads.

The daily run log only carries the handful of COMEX sessions the pipeline
happened to capture (the CmeWS feed keeps ~1 week and is flaky for the latest
day). This pulls a proper multi-week COMEX settlement series and joins it with
Westmetall's LME history so the dashboard's spread chart has real depth.

COMEX source order:
  1. Barchart ``historical/get`` — ~6 months of the most-active contract's daily
     settlements in one call. Needs the JS challenge to be down, or a browser
     token in ``BARCHART_XSRF_TOKEN`` / ``BARCHART_COOKIE``.
  2. CmeWS settlements — only its rolling ~1-week window, as a fallback.

    python scripts/backfill_prices.py [--days 180] [--dry-run] [-v]
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

from scripts.price_scraper import (  # noqa: E402
    WESTMETALL_LME_CU, PriceScraperError, _fetch_westmetall, _parse_westmetall,
    get_comex_cme_history, get_comex_copper_history,
)
from scripts.schema import upsert_comex_lme_history  # noqa: E402

log = logging.getLogger("backfill_prices")

DEFAULT_HISTORY_PARQUET = _HERE.parent / "data" / "comex_lme_history.parquet"


def _comex_history(days: int) -> tuple[pd.DataFrame, str]:
    try:
        return get_comex_copper_history(days=days), "Barchart"
    except PriceScraperError as exc:
        log.warning("Barchart COMEX history unavailable (%s); trying the CmeWS window", exc)
    return get_comex_cme_history(days=12), "CmeWS"


def _lme_asof(history: list[tuple[dt.date, float, float | None]], day: dt.date):
    for d, cash, m3 in history:  # newest first
        if d <= day:
            return d, cash, m3
    return None


def build_rows(*, days: int = 180) -> list[dict]:
    comex, source = _comex_history(days)
    lme_hist = _parse_westmetall(_fetch_westmetall(WESTMETALL_LME_CU))
    if not lme_hist:
        raise PriceScraperError("Westmetall returned no LME rows")
    lme_floor = lme_hist[-1][0]
    now = dt.datetime.now(dt.timezone.utc)

    rows: list[dict] = []
    for _, c in comex.iterrows():
        sess = pd.to_datetime(c["date"]).date()
        if sess < lme_floor:
            continue
        hit = _lme_asof(lme_hist, sess)
        if hit is None:
            continue
        lme_date, cash, m3 = hit
        cx_lb = float(c["settle"])
        cx_t = round(cx_lb * 2204.6226218488, 2)
        rows.append({
            "session_date": pd.Timestamp(sess),
            "comex_contract": str(c["contract"]),
            "comex_usd_lb": round(cx_lb, 4),
            "comex_usd_t": cx_t,
            "lme_cash_usd_t": round(cash, 2),
            "lme_3m_usd_t": round(m3, 2) if m3 is not None else None,
            "lme_price_date": pd.Timestamp(lme_date),
            "cme_lme_spread_usd_t": round(cx_t - cash, 2),
            "cme_lme_spread_3m_usd_t": round(cx_t - m3, 2) if m3 is not None else None,
            "lme_cash_3m_spread_usd_t": round(cash - m3, 2) if m3 is not None else None,
            "comex_source": source,
            "retrieved_at": now,
        })
    log.info("built %d session rows from %s COMEX history", len(rows), source)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parquet", type=Path, default=DEFAULT_HISTORY_PARQUET)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    rows = build_rows(days=args.days)
    if not rows:
        print("no session rows built.")
        return 0
    rep = pd.DataFrame(rows)[["session_date", "comex_contract", "comex_usd_t",
                              "lme_3m_usd_t", "cme_lme_spread_3m_usd_t", "comex_source"]]
    rep["session_date"] = rep["session_date"].dt.date
    print(rep.to_string(index=False))

    if args.dry_run:
        print(f"\n(--dry-run: {len(rows)} rows not written)")
        return 0
    out = upsert_comex_lme_history(rows, args.parquet)
    print(f"\nupserted {len(rows)} rows -> {args.parquet} ({len(out)} sessions total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
