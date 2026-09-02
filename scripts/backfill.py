"""
One-off history backfill for ``data/copper_inventory.parquet``.

The daily pipeline only records "now". This pulls whatever recent history each
source still exposes and writes a synthetic run row per observation date, so the
dashboard's as-of chart has real points going back ~1-2 weeks instead of every
exchange starting on the same day.

What can be recovered:
  * CME  — the Copper_Stocks.xls carries a "PREV TOTAL" column, i.e. the prior
           business day's Registered / Eligible. Two points: activity date + prev.
  * LME  — the stock-breakdown and OWSR listings expose ~the last 7 days /
           current month; every listed report is parsed.
  * SHFE — the last few weekly reports (Fridays) are fetched individually.

    python scripts/backfill.py [--parquet PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts.aggregate import (  # noqa: E402
    DEFAULT_PARQUET, SHORT_TON_TO_TONNE, _compute_totals, upsert_parquet,
)
from scripts.cme_scraper import download_cme_copper_stocks  # noqa: E402
from scripts.lme_scraper import (  # noqa: E402
    OFF_WARRANT_CONFIG_ID, OFF_WARRANT_PAGE, STOCK_BREAKDOWN_CONFIG_ID,
    download_lme_report, list_lme_reports, parse_lme_offwarrant, parse_lme_stock_breakdown,
)
from scripts.schema import SCHEMA  # noqa: E402
from scripts.shfe_scraper import fetch_shfe_weekly_stock_html, parse_shfe_weekly_stock  # noqa: E402

log = logging.getLogger("backfill")

_RE_NUM_COLS = (2, 7)  # PREV TOTAL, TOTAL TODAY in the CME grand-total rows


def _prev_business_day(d: dt.date) -> dt.date:
    p = d - dt.timedelta(days=1)
    while p.weekday() >= 5:
        p -= dt.timedelta(days=1)
    return p


# --------------------------------------------------------------------------- #
# Per-source history
# --------------------------------------------------------------------------- #
def cme_points() -> list[dict[str, Any]]:
    """[{data_date, registered_st, eligible_st}] for the activity date + prev day."""
    import re

    raw = download_cme_copper_stocks()
    df = pd.read_excel(io.BytesIO(raw), sheet_name=0, header=None, engine="xlrd")
    blob = "\n".join(
        " ".join("" if pd.isna(v) else str(v) for v in row) for row in df.values.tolist()
    )
    m = re.search(r"Activity Date:\s*(\d{1,2}/\d{1,2}/\d{4})", blob)
    if not m:
        raise RuntimeError("CME: could not find Activity Date in Copper_Stocks.xls")
    act = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date()

    def _num(v: Any) -> float:
        return float(str(v).replace(",", "").strip())

    def row_vals(label_re: str) -> tuple[float, float]:
        mask = df[0].astype(str).str.contains(label_re, case=False, na=False, regex=True)
        r = df[mask].iloc[0]
        return _num(r[2]), _num(r[7])  # PREV TOTAL, TOTAL TODAY

    reg_prev, reg_now = row_vals(r"^\s*Total Registered")
    elig_prev, elig_now = row_vals(r"^\s*Total Eligible")
    return [
        {"data_date": act, "registered_st": reg_now, "eligible_st": elig_now},
        {"data_date": _prev_business_day(act), "registered_st": reg_prev, "eligible_st": elig_prev},
    ]


def lme_breakdown_points() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reports = list_lme_reports(config_id=STOCK_BREAKDOWN_CONFIG_ID,
                              date_facets=("Last 7 days", "Current month"))
    for item in reports:
        try:
            content, filename = download_lme_report(item["item_id"])
            rec = parse_lme_stock_breakdown(
                content, report_date=item["report_date"], source_report_name=item["name"] or filename
            )
            out.append({"data_date": rec.report_date, "live": rec.live_warrant_tonnes,
                        "cancelled": rec.cancelled_warrant_tonnes,
                        "closing": rec.total_on_warrant_tonnes})
        except Exception as exc:  # noqa: BLE001
            log.warning("LME breakdown %s: %s", item["name"], exc)
    return out


def lme_owsr_points() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reports = list_lme_reports(config_id=OFF_WARRANT_CONFIG_ID, listing_page=OFF_WARRANT_PAGE,
                              date_facets=("Last 7 days", "Current month"))
    for item in reports:
        try:
            content, filename = download_lme_report(item["item_id"])
            rec = parse_lme_offwarrant(content, report_date=item["report_date"],
                                       source_report_name=item["name"] or filename)
            out.append({"data_date": rec.report_date, "off_warrant": rec.off_warrant_tonnes})
        except Exception as exc:  # noqa: BLE001
            log.warning("LME OWSR %s: %s", item["name"], exc)
    return out


def shfe_points(weeks: int = 4) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    anchor = dt.date.today()
    for k in range(weeks):
        friday = anchor - dt.timedelta(days=(anchor.weekday() - 4) % 7 + 7 * k)
        try:
            html, url_date = fetch_shfe_weekly_stock_html(start_date=friday, fridays_to_try=1)
            rec = parse_shfe_weekly_stock(html, url_date=url_date)
            out.append({"data_date": rec.report_date, "warrant": rec.futures_warrant_tonnes,
                        "unregistered": rec.implied_non_warranted_tonnes,
                        "inventory": rec.physical_inventory_tonnes})
        except Exception as exc:  # noqa: BLE001
            log.warning("SHFE weekly ~%s: %s", friday, exc)
    return out


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #
def _nearest(points: list[dict[str, Any]], on: dt.date) -> dict[str, Any] | None:
    prior = [p for p in points if p["data_date"] <= on]
    return max(prior, key=lambda p: p["data_date"]) if prior else None


def build_rows() -> list[dict[str, Any]]:
    cme = sorted(cme_points(), key=lambda p: p["data_date"])
    lme = sorted(lme_breakdown_points(), key=lambda p: p["data_date"])
    owsr = sorted(lme_owsr_points(), key=lambda p: p["data_date"])
    shfe = sorted(shfe_points(), key=lambda p: p["data_date"])
    log.info("history: CME %d, LME %d, OWSR %d, SHFE %d points",
             len(cme), len(lme), len(owsr), len(shfe))

    dates = sorted({p["data_date"] for src in (cme, lme, owsr, shfe) for p in src})
    rows: list[dict[str, Any]] = []
    for d in dates:
        row: dict[str, Any] = {k: None for k in SCHEMA}
        row["run_date"] = d
        row["retrieved_at"] = dt.datetime.now(dt.timezone.utc)
        for f in ("cme_stale", "lme_stale", "lme_offwarrant_stale", "shfe_stale", "price_stale"):
            row[f] = False

        c = _nearest(cme, d)
        if c:
            reg_t = c["registered_st"] * SHORT_TON_TO_TONNE
            elig_t = c["eligible_st"] * SHORT_TON_TO_TONNE
            row.update(cme_on_warrant_t=round(reg_t, 3), cme_cancelled_t=0.0,
                       cme_unregistered_t=round(elig_t, 3), cme_off_warrant_t=float("nan"),
                       cme_total_t=round(reg_t + elig_t, 3), cme_data_date=c["data_date"],
                       cme_registered_short_tons=c["registered_st"],
                       cme_eligible_short_tons=c["eligible_st"])

        lclose = None
        lp = _nearest(lme, d)
        if lp:
            lclose = lp["closing"]
            row.update(lme_on_warrant_t=lp["live"], lme_cancelled_t=lp["cancelled"],
                       lme_unregistered_t=0.0, lme_warrant_data_date=lp["data_date"])
        op = _nearest(owsr, d)
        if op:
            row.update(lme_off_warrant_t=op["off_warrant"], lme_offwarrant_data_date=op["data_date"])

        sp = _nearest(shfe, d)
        if sp:
            row.update(shfe_on_warrant_t=sp["warrant"], shfe_cancelled_t=0.0,
                       shfe_unregistered_t=sp["unregistered"], shfe_off_warrant_t=float("nan"),
                       shfe_total_t=sp["inventory"], shfe_data_date=sp["data_date"])

        _compute_totals(row, lclose)
        row["sources_ok"] = ",".join(
            s for s, present in [("CME", c), ("LME", lp), ("LME_OWSR", op), ("SHFE", sp)] if present
        )
        row["sources_failed"] = ""
        row["notes"] = "backfill"
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    rows = build_rows()
    for r in rows:
        print(f"  {r['run_date']}  ok={r['sources_ok'] or '-':22}  "
              f"reported={r.get('global_reported_stock_t')}  total={r.get('global_total_t')}")
    if args.dry_run:
        print(f"\n(--dry-run: {len(rows)} rows not written)")
        return 0
    combined = None
    for r in rows:
        combined = upsert_parquet(r, args.parquet)
    print(f"\nupserted {len(rows)} backfill rows -> {args.parquet} "
          f"({len(combined) if combined is not None else 0} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
