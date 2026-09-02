"""
STEP 3 — SHFE (Shanghai Futures Exchange) copper inventory scraper.

Sources
-------
SHFE publishes two relevant warehouse reports as HTML tables under
``/data/tradedata/future/stockdata/`` (the same files its site renders in an
iframe). Both are already in **metric tonnes**.

1. Weekly stock report  —  库存周报  (published each Friday)
   URL: /data/tradedata/future/stockdata/weeklystock_<YYYYMMDD>/EN/all.html
   Per metal, per warehouse:  Previous Week / This Week / Change, each split into
   "Delivery-able" (库存 = physical stock in the exchange warehouse) and
   "On Warrant" (仓单 = registered futures warrants), plus storage capacity.
   The COPPER "Total" row is what we keep.

2. Daily warehouse warrant report  —  仓单日报  (published each trading day)
   URL: /data/tradedata/future/stockdata/dailystock_<YYYYMMDD>/EN/all.html
   Per metal, per warehouse:  On Warrant + Change.  **Warrants only — no
   physical-inventory column.**  Useful as a fresher warrant number between the
   weekly reports.

Harmonised fields
-----------------
    physical_inventory_tonnes    <- weekly "This Week / Delivery-able"  (库存)
    futures_warrant_tonnes       <- weekly "This Week / On Warrant"     (仓单)
    implied_non_warranted_tonnes <- inventory - warrant

SHFE has **no published "cancelled warrant" figure**. The user's spec lists a
"Cancelled Warrants" bucket for SHFE; the closest real quantity is
``physical_inventory - futures_warrant`` (stock in an exchange warehouse that is
not currently under warrant). It is exposed as ``implied_non_warranted_tonnes``
and must NOT be presented as a true cancelled-warrant number. STEP 4 decides how
to fold it into the global on-warrant / cancelled / off-warrant model.

The full inventory picture is therefore **weekly**. The daily report only
refreshes the warrant leg.

HTTP note
---------
Use a plain ``requests`` call + a browser User-Agent. ``curl_cffi`` gets
throttled here — do not use it.

SHFE sits behind a JS-challenge WAF ("WEB 应用防火墙") that blocks bursty /
datacentre traffic: after ~30 quick requests it starts returning a ~10.6 KB
challenge page (HTTP 200) instead of the report. This scraper therefore:

    * probes as few URLs as possible — weekly candidates are the last few
      **Fridays** only, not a day-by-day walk-back;
    * sleeps between probes;
    * detects the challenge page and raises :class:`SHFEBlockedError` loudly
      (with a longer retry back-off first), rather than returning junk.

If GitHub Actions runners get persistently challenged, the fallback options are a
residential proxy, a self-hosted runner, or a third-party mirror of the same
figures.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import time
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

__all__ = [
    "get_shfe_copper_stocks",
    "get_shfe_copper_warrants_daily",
    "fetch_shfe_weekly_stock_html",
    "fetch_shfe_daily_warrant_html",
    "parse_shfe_weekly_stock",
    "parse_shfe_daily_warrant",
    "SHFEWeeklyStock",
    "SHFEDailyWarrant",
    "SHFEScraperError",
    "SHFEBlockedError",
    "SHFEUnavailableError",
    "SHFEParseError",
]

log = logging.getLogger(__name__)

SHFE_BASE = "https://www.shfe.com.cn"
TRADING_DAY_URL = f"{SHFE_BASE}/data/config/currentTradingday.dat"
WEEKLY_URL_TMPL = f"{SHFE_BASE}/data/tradedata/future/stockdata/weeklystock_{{date}}/EN/all.html"
DAILY_URL_TMPL = f"{SHFE_BASE}/data/tradedata/future/stockdata/dailystock_{{date}}/EN/all.html"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SHFE_BASE}/reports/tradedata/",
}

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 3.0
BLOCKED_BACKOFF = 20.0     # WAF challenge: wait much longer before retrying
PROBE_DELAY = 1.5          # politeness pause between report-date probes
WEEKLY_FRIDAYS_TO_TRY = 3  # most recent Friday, then -7, -14
DAILY_LOOKBACK_DAYS = 7

_METAL_DEFAULT = "COPPER"
_RE_DATE_HEADER = re.compile(r"DATE\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
_RE_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_RE_TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_404 = re.compile(r"<title>\s*404\s*</title>|页面不存在", re.IGNORECASE)
# SHFE WAF challenge page ("WEB 应用防火墙"): ~10.6 KB, HTTP 200, JS slider captcha.
_RE_WAF = re.compile(r"应用防火墙|js-challenge|id=\"slider\"|id=\"captcha\"")
_DATE_MIN = dt.date(2000, 1, 1)


class SHFEScraperError(RuntimeError):
    """Base class for SHFE scraper failures."""


class SHFEBlockedError(SHFEScraperError):
    """SHFE's WAF served a JS-challenge page instead of the report."""


class SHFEUnavailableError(SHFEScraperError):
    """No report is published for the requested/searched dates (HTTP 404)."""


class SHFEParseError(SHFEScraperError):
    """A report downloaded but its structure could not be understood."""


@dataclasses.dataclass(slots=True)
class SHFEWeeklyStock:
    report_date: dt.date
    metal: str
    physical_inventory_tonnes: float
    futures_warrant_tonnes: float
    inventory_change_tonnes: float
    warrant_change_tonnes: float
    retrieved_at: dt.datetime

    @property
    def implied_non_warranted_tonnes(self) -> float:
        return round(self.physical_inventory_tonnes - self.futures_warrant_tonnes, 3)

    def as_record(self) -> dict[str, Any]:
        return {
            "source": "SHFE",
            "report_type": "weekly",
            "report_date": self.report_date,
            "metal": "Copper",
            "physical_inventory_tonnes": self.physical_inventory_tonnes,
            "futures_warrant_tonnes": self.futures_warrant_tonnes,
            "implied_non_warranted_tonnes": self.implied_non_warranted_tonnes,
            "inventory_change_tonnes": self.inventory_change_tonnes,
            "warrant_change_tonnes": self.warrant_change_tonnes,
            "unit": "metric_tonne",
            "retrieved_at": self.retrieved_at,
        }


@dataclasses.dataclass(slots=True)
class SHFEDailyWarrant:
    report_date: dt.date
    metal: str
    futures_warrant_tonnes: float
    warrant_change_tonnes: float
    retrieved_at: dt.datetime

    def as_record(self) -> dict[str, Any]:
        return {
            "source": "SHFE",
            "report_type": "daily_warrant",
            "report_date": self.report_date,
            "metal": "Copper",
            "futures_warrant_tonnes": self.futures_warrant_tonnes,
            "warrant_change_tonnes": self.warrant_change_tonnes,
            "unit": "metric_tonne",
            "retrieved_at": self.retrieved_at,
        }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _session(session=None):
    if requests is None:  # pragma: no cover
        raise SHFEScraperError("the 'requests' package is required for the SHFE scraper")
    if session is not None:
        return session, False
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s, True


def _get(sess, url: str, *, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES,
         backoff=DEFAULT_BACKOFF, allow_404=False):
    """GET with retries. Raises :class:`SHFEBlockedError` on the WAF challenge,
    :class:`SHFEUnavailableError` on 404 (unless ``allow_404`` -> returns None)."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last = exc
            log.warning("SHFE: network error attempt %d for %s: %s", attempt, url, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue

        text = resp.text or ""

        if _RE_WAF.search(text[:4000]):
            last = SHFEBlockedError(f"SHFE WAF challenge for {url}")
            log.warning("SHFE: WAF challenge on attempt %d for %s", attempt, url)
            if attempt < retries:
                time.sleep(BLOCKED_BACKOFF * attempt)
                continue
            raise last

        is_404 = resp.status_code == 404 or (
            resp.status_code == 200 and len(resp.content) < 6000 and _RE_404.search(text)
        )
        if is_404:
            if allow_404:
                return None
            raise SHFEUnavailableError(f"SHFE 404 for {url}")

        if resp.status_code != 200:
            last = SHFEScraperError(f"HTTP {resp.status_code} for {url}")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise SHFEScraperError(f"HTTP {resp.status_code} for {url}")
        return resp

    raise SHFEScraperError(f"SHFE GET failed after {retries} attempts: {url}") from last


def _looks_like_report(text: str) -> bool:
    return "el-table_table" in text and "<tr" in text.lower()


def _recent_fridays(anchor: dt.date, count: int) -> list[dt.date]:
    """The ``count`` most recent Fridays on or before ``anchor`` (plus the
    Thursday before each, in case a Friday holiday shifted publication)."""
    friday = anchor - dt.timedelta(days=(anchor.weekday() - 4) % 7)
    out: list[dt.date] = []
    for k in range(count):
        f = friday - dt.timedelta(days=7 * k)
        out.append(f)
        out.append(f - dt.timedelta(days=1))  # Thursday fallback
    return out


def _current_trading_day(sess) -> dt.date | None:
    try:
        resp = _get(sess, f"{TRADING_DAY_URL}?params={int(time.time() * 1000)}", retries=2)
    except SHFEScraperError as exc:
        log.warning("SHFE: could not read currentTradingday (%s); falling back to today", exc)
        return None
    m = re.search(r'"currentTradingday"\s*:\s*"(\d{8})"', resp.text)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Fetch (walk back to the latest published report)
# --------------------------------------------------------------------------- #
def fetch_shfe_weekly_stock_html(
    *, start_date: dt.date | None = None, fridays_to_try: int = WEEKLY_FRIDAYS_TO_TRY, session=None
) -> tuple[str, dt.date]:
    """Return (html, url_date) for the most recent weekly stock report.

    Only Friday (and the preceding Thursday) URLs are probed — the weekly report
    is a Friday publication — so this costs at most ``2 * fridays_to_try`` GETs.
    """
    sess, own = _session(session)
    try:
        anchor = start_date or _current_trading_day(sess) or dt.date.today()
        for d in _recent_fridays(anchor, fridays_to_try):
            if d > anchor:
                continue
            url = WEEKLY_URL_TMPL.format(date=d.strftime("%Y%m%d")) + f"?params={int(time.time()*1000)}"
            resp = _get(sess, url, allow_404=True)
            if resp is not None and _looks_like_report(resp.text):
                log.info("SHFE: weekly stock report found for %s", d)
                return resp.text, d
            time.sleep(PROBE_DELAY)
        raise SHFEUnavailableError(
            f"no SHFE weekly stock report among the last {fridays_to_try} Fridays before {anchor}"
        )
    finally:
        if own:
            sess.close()


def fetch_shfe_daily_warrant_html(
    *, start_date: dt.date | None = None, lookback_days: int = DAILY_LOOKBACK_DAYS, session=None
) -> tuple[str, dt.date]:
    """Return (html, url_date) for the most recent daily warehouse-warrant report."""
    sess, own = _session(session)
    try:
        anchor = start_date or _current_trading_day(sess) or dt.date.today()
        for delta in range(lookback_days + 1):
            d = anchor - dt.timedelta(days=delta)
            if d.weekday() >= 5:  # no Sat/Sun report
                continue
            url = DAILY_URL_TMPL.format(date=d.strftime("%Y%m%d")) + f"?params={int(time.time()*1000)}"
            resp = _get(sess, url, allow_404=True)
            if resp is not None and _looks_like_report(resp.text):
                log.info("SHFE: daily warrant report found for %s", d)
                return resp.text, d
            time.sleep(PROBE_DELAY)
        raise SHFEUnavailableError(
            f"no SHFE daily warrant report in the {lookback_days} days before {anchor}"
        )
    finally:
        if own:
            sess.close()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _rows_for_metal_section(html: str, metal: str) -> list[list[str]]:
    """All <tr> cell-lists between the metal's banner row and the next banner row.

    The metal banner is a row whose first cell is exactly the metal name
    (``COPPER``) — this correctly excludes the separate ``COPPER(BC)`` section.
    """
    metal_u = metal.strip().upper()
    all_rows: list[list[str]] = []
    for tr in _RE_TR.findall(html):
        cells = [_clean(c) for c in _RE_TD.findall(tr)]
        if cells:
            all_rows.append(cells)

    start = end = None
    for i, cells in enumerate(all_rows):
        first = cells[0].strip().upper()
        if start is None and first == metal_u:
            start = i + 1
            continue
        if start is not None:
            # next banner: a short all-caps label in col 0, no digits
            if first and first.replace("(", "").replace(")", "").isalpha() and first.isupper() \
                    and len(cells) <= 2:
                end = i
                break
    if start is None:
        raise SHFEParseError(f"metal section {metal!r} not found in SHFE report")
    return all_rows[start:end] if end else all_rows[start:]


def _clean(cell_html: str) -> str:
    return _RE_TAG.sub("", cell_html).replace("\xa0", " ").strip()


def _report_date(html: str, url_date: dt.date | None) -> dt.date:
    m = _RE_DATE_HEADER.search(html)
    if m:
        try:
            d = dt.date.fromisoformat(m.group(1))
            if _DATE_MIN <= d <= dt.date.today() + dt.timedelta(days=2):
                return d
        except ValueError:
            pass
    if url_date is not None:
        return url_date
    raise SHFEParseError("no report date in header and no url_date supplied")


def _to_number(value: str) -> float | None:
    s = (value or "").strip().replace(",", "")
    if s in {"", "-", "--", "—", "/"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    f = float(s)
    return -f if neg else f


def _total_row_numbers(rows: list[list[str]]) -> list[float]:
    """Numeric cells of the grand 'Total' row (first cell exactly 'Total')."""
    for cells in rows:
        if cells and cells[0].strip().lower() == "total":
            nums = [n for n in (_to_number(c) for c in cells[1:]) if n is not None]
            if nums:
                return nums
    raise SHFEParseError("no grand 'Total' row in the metal section")


def parse_shfe_weekly_stock(
    html: str,
    *,
    url_date: dt.date | None = None,
    metal: str = _METAL_DEFAULT,
    retrieved_at: dt.datetime | None = None,
) -> SHFEWeeklyStock:
    """Parse the weekly 库存周报 COPPER Total row.

    Total-row numeric layout (verified 2026-08):
        [prev_inv, prev_wrt, this_inv, this_wrt, chg_inv, chg_wrt, cap_last, cap_this, cap_chg]
    We confirm it by the identity  this - prev == change  on both legs, scanning a
    small offset in case leading columns shift.
    """
    if retrieved_at is None:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
    if not html or "<tr" not in html.lower():
        raise SHFEParseError("empty / non-HTML payload")

    rows = _rows_for_metal_section(html, metal)
    nums = _total_row_numbers(rows)
    if len(nums) < 6:
        raise SHFEParseError(f"weekly Total row has too few numbers: {nums}")

    off = _find_weekly_offset(nums)
    prev_inv, prev_wrt = nums[off], nums[off + 1]
    this_inv, this_wrt = nums[off + 2], nums[off + 3]
    chg_inv, chg_wrt = nums[off + 4], nums[off + 5]

    if this_inv < 0 or this_wrt < 0:
        raise SHFEParseError(f"negative weekly stock: inv={this_inv} wrt={this_wrt}")
    if this_wrt > this_inv + 1:
        raise SHFEParseError(
            f"warrant ({this_wrt}) exceeds inventory ({this_inv}) — column mapping wrong"
        )

    rec = SHFEWeeklyStock(
        report_date=_report_date(html, url_date),
        metal="Copper",
        physical_inventory_tonnes=this_inv,
        futures_warrant_tonnes=this_wrt,
        inventory_change_tonnes=chg_inv,
        warrant_change_tonnes=chg_wrt,
        retrieved_at=retrieved_at,
    )
    log.info(
        "SHFE weekly %s: inventory=%.0f warrant=%.0f (implied non-warranted=%.0f)",
        rec.report_date, this_inv, this_wrt, rec.implied_non_warranted_tonnes,
    )
    _ = (prev_inv, prev_wrt)  # kept for clarity of the verified layout
    return rec


def _find_weekly_offset(nums: list[float]) -> int:
    for off in range(0, min(3, len(nums) - 6) + 1):
        try:
            inv_ok = abs((nums[off + 2] - nums[off]) - nums[off + 4]) <= 1.0
            wrt_ok = abs((nums[off + 3] - nums[off + 1]) - nums[off + 5]) <= 1.0
        except IndexError:
            break
        if inv_ok and wrt_ok:
            return off
    # Fall back to the canonical layout; parse-time validation still guards it.
    log.warning("SHFE weekly: change-identity check failed, assuming offset 0 (nums=%s)", nums)
    return 0


def parse_shfe_daily_warrant(
    html: str,
    *,
    url_date: dt.date | None = None,
    metal: str = _METAL_DEFAULT,
    retrieved_at: dt.datetime | None = None,
) -> SHFEDailyWarrant:
    """Parse the daily 仓单日报 COPPER Total row: [on_warrant, change]."""
    if retrieved_at is None:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
    if not html or "<tr" not in html.lower():
        raise SHFEParseError("empty / non-HTML payload")

    rows = _rows_for_metal_section(html, metal)
    nums = _total_row_numbers(rows)
    if not nums:
        raise SHFEParseError("daily Total row has no numbers")
    warrant = nums[0]
    change = nums[1] if len(nums) > 1 else 0.0
    if warrant < 0:
        raise SHFEParseError(f"negative daily warrant total: {warrant}")

    rec = SHFEDailyWarrant(
        report_date=_report_date(html, url_date),
        metal="Copper",
        futures_warrant_tonnes=warrant,
        warrant_change_tonnes=change,
        retrieved_at=retrieved_at,
    )
    log.info("SHFE daily %s: warrant=%.0f (change %.0f)", rec.report_date, warrant, change)
    return rec


# --------------------------------------------------------------------------- #
# Public entrypoints
# --------------------------------------------------------------------------- #
def get_shfe_copper_warrants_daily(*, session=None) -> dict[str, Any]:
    """Latest daily warehouse-warrant total for copper (warrants only)."""
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    html, url_date = fetch_shfe_daily_warrant_html(session=session)
    return parse_shfe_daily_warrant(html, url_date=url_date, retrieved_at=retrieved_at).as_record()


def get_shfe_copper_stocks(*, enrich_with_daily: bool = True) -> dict[str, Any]:
    """Latest SHFE copper inventory picture.

    Returns the weekly record (physical inventory + futures warrants + implied
    non-warranted). If ``enrich_with_daily`` and the daily warrant report is
    newer than the weekly one, adds ``futures_warrant_tonnes_daily`` and
    ``futures_warrant_daily_date`` alongside (the weekly pair is left intact so
    it stays internally consistent — STEP 4 chooses which warrant leg to use).
    """
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    sess, _ = _session(None)
    try:
        html, url_date = fetch_shfe_weekly_stock_html(session=sess)
        rec = parse_shfe_weekly_stock(html, url_date=url_date, retrieved_at=retrieved_at).as_record()

        if enrich_with_daily:
            try:
                d_html, d_date = fetch_shfe_daily_warrant_html(session=sess)
                daily = parse_shfe_daily_warrant(d_html, url_date=d_date, retrieved_at=retrieved_at)
                if daily.report_date > rec["report_date"]:
                    rec["futures_warrant_tonnes_daily"] = daily.futures_warrant_tonnes
                    rec["futures_warrant_daily_date"] = daily.report_date
            except SHFEScraperError as exc:
                log.warning("SHFE: daily enrichment skipped: %s", exc)
        return rec
    finally:
        sess.close()


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if len(sys.argv) > 1:  # python scripts/shfe_scraper.py <weekly.html|daily.html> [YYYY-MM-DD]
        from pathlib import Path

        p = Path(sys.argv[1])
        raw = p.read_text(encoding="utf-8", errors="replace")
        ud = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
        if "weekly" in p.name.lower():
            out = parse_shfe_weekly_stock(raw, url_date=ud).as_record()
        else:
            out = parse_shfe_daily_warrant(raw, url_date=ud).as_record()
    else:
        out = get_shfe_copper_stocks()

    print(json.dumps(out, indent=2, default=str))
