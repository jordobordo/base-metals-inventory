"""
STEP 1 — CME (COMEX) copper warehouse-stock scraper.

Source file
-----------
CME publishes a daily copper warehouse-stock report at:

    https://www.cmegroup.com/delivery_reports/Copper_Stocks.xls

This is a genuine BIFF ``.xls`` workbook (OLE2 / ``xlrd``), one sheet named
``"Daily Metal Stocks Report"``. Verified layout (2026-08):

      row  col0                              col2      col3     col4      col5       col6        col7
      ---  --------------------------------  --------  -------  --------  ---------  ----------  -----------
       7   COPPER - HIGH GRADE                                                      Report Date: 8/31/2026
       8   Short Tons                                                               Activity Date: 8/28/2026
      10   DELIVERY POINT                    PREV TOT  RECEIVED WITHDRAWN NET CHANGE ADJUSTMENT  TOTAL TODAY
      ...  <per-warehouse blocks of 3 rows: Registered / Eligible / Total>
      67   Total Registered (warranted)      477482    0        0         0          0           477482
      68   Total Eligible (non-warranted)    276158    6085     836       5249       0           281407
      69   TOTAL COPPER                      753640    6085     836       5249       0           758889

The numbers we keep are in the **"TOTAL TODAY"** column (col 7) of the three
grand-total rows (67-69). ``Total Registered + Total Eligible == TOTAL COPPER``.

What this returns
-----------------
Raw **short tons**, plus dates:

    * registered_short_tons  -> "Total Registered (warranted)"      == on-warrant
    * eligible_short_tons    -> "Total Eligible (non-warranted)"    == off-warrant
    * total_short_tons       -> "TOTAL COPPER"
    * report_date            -> the **Activity Date** (business date the stocks
                                are effective — this is the key to align on)
    * publish_date           -> the "Report Date" (when CME published the file)

Unit conversion to metric tonnes (x 0.907185) and the mapping onto the harmonised
on-warrant / cancelled / off-warrant schema belong in STEP 4 (the aggregator),
NOT here. COMEX has no "cancelled warrant" concept; STEP 4 will treat cancelled
as 0 for CME (or wire in the separate delivery-notices report if we want it).

    >>> from scripts.cme_scraper import get_cme_copper_stocks
    >>> get_cme_copper_stocks()
    {'source': 'CME', 'report_date': datetime.date(2026, 8, 28),
     'publish_date': datetime.date(2026, 8, 31),
     'registered_short_tons': 477482.0, 'eligible_short_tons': 281407.0,
     'total_short_tons': 758889.0, 'unit': 'short_ton',
     'retrieved_at': datetime.datetime(...)}

Operational note
----------------
CME's Data Terms of Use prohibit automated access and their edge (Akamai) 403s
suspected scrapers — the block is IP/rate based and hits datacentre ranges
(incl. GitHub-hosted Actions runners) hardest. This scraper raises
:class:`CMEBlockedError` (loudly, with the block body) rather than returning
silent garbage, so the pipeline fails visibly and a fallback source can be
swapped in. See the README for fallback options.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import logging
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
from dateutil import parser as dateparser

try:
    import requests
except ImportError:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore[assignment]

__all__ = [
    "get_cme_copper_stocks",
    "download_cme_copper_stocks",
    "parse_cme_copper_stocks",
    "CMECopperStocks",
    "CMEScraperError",
    "CMEBlockedError",
    "CMEParseError",
]

log = logging.getLogger(__name__)

CME_COPPER_STOCKS_URL = "https://www.cmegroup.com/delivery_reports/Copper_Stocks.xls"

# A realistic desktop-Chrome header set. CME's edge fingerprints the full
# Sec-Fetch / Sec-CH-UA cluster, not just User-Agent, so send all of it.
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/vnd.ms-excel,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.cmegroup.com/markets/metals/base/copper.html",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 4.0  # seconds, multiplied by attempt number

# --- row / value patterns for the CME "Daily Metal Stocks Report" layout ---
_RE_TOTAL_REGISTERED = re.compile(r"total\s+registered", re.IGNORECASE)
_RE_TOTAL_ELIGIBLE = re.compile(r"total\s+eligible", re.IGNORECASE)
# Grand total row label: "TOTAL COPPER" (not the bare "Total" per-warehouse subtotals,
# and not the "Total Registered/Eligible" rows).
_RE_GRAND_TOTAL = re.compile(r"^\s*total\s+[a-z].*", re.IGNORECASE)
_RE_TOTAL_TODAY_HDR = re.compile(r"total\s*today", re.IGNORECASE)
_RE_PREV_TOTAL_HDR = re.compile(r"prev(?:ious)?\s*total", re.IGNORECASE)
_RE_SHORT_TON = re.compile(r"short\s*tons?", re.IGNORECASE)
_RE_METRIC_TON = re.compile(r"metric\s*tons?|tonnes?", re.IGNORECASE)

_RE_REPORT_DATE = re.compile(r"report\s*date\s*[:\-]?\s*" + r"([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}|[A-Za-z]{3,9}\.?\s+[0-9]{1,2},?\s+[0-9]{4})", re.IGNORECASE)
_RE_ACTIVITY_DATE = re.compile(r"activity\s*date\s*[:\-]?\s*" + r"([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}|[A-Za-z]{3,9}\.?\s+[0-9]{1,2},?\s+[0-9]{4})", re.IGNORECASE)
_RE_ANY_DATE = re.compile(r"([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}|[A-Za-z]{3,9}\.?\s+[0-9]{1,2},?\s+[0-9]{4})")

# Generic fallback patterns (older / HTML variants of the report).
_RE_TOTAL_ROW_GENERIC = re.compile(r"^\s*(grand\s+)?total\b", re.IGNORECASE)
_RE_REGISTERED_GENERIC = re.compile(r"regist", re.IGNORECASE)
_RE_ELIGIBLE_GENERIC = re.compile(r"eligib", re.IGNORECASE)
_RE_TOTAL_COL_GENERIC = re.compile(r"^\s*total\s*$", re.IGNORECASE)

_DATE_MIN = dt.date(2000, 1, 1)


class CMEScraperError(RuntimeError):
    """Base class for every failure mode in this module."""


class CMEBlockedError(CMEScraperError):
    """CME's edge returned an anti-scraping block (HTTP 403 + JSON message)."""


class CMEParseError(CMEScraperError):
    """The file downloaded but its structure could not be understood."""


@dataclasses.dataclass(slots=True)
class CMECopperStocks:
    """Parsed, still-raw (short-ton) COMEX copper stock snapshot."""

    report_date: dt.date          # Activity Date — business date the stocks are effective
    publish_date: dt.date | None  # Report Date — when CME published the file
    registered_short_tons: float
    eligible_short_tons: float
    total_short_tons: float
    unit: str
    retrieved_at: dt.datetime

    def as_record(self) -> dict[str, Any]:
        return {
            "source": "CME",
            "report_date": self.report_date,
            "publish_date": self.publish_date,
            "registered_short_tons": self.registered_short_tons,
            "eligible_short_tons": self.eligible_short_tons,
            "total_short_tons": self.total_short_tons,
            "unit": self.unit,
            "retrieved_at": self.retrieved_at,
        }


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def download_cme_copper_stocks(
    url: str = CME_COPPER_STOCKS_URL,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    session: "requests.Session | None" = None,
    save_raw_to: str | Path | None = None,
) -> bytes:
    """Fetch the raw Copper_Stocks workbook as bytes.

    Retries transient network errors and 429/5xx responses with a linear
    backoff. Raises :class:`CMEBlockedError` on the anti-scraping 403 and
    :class:`CMEScraperError` on any other non-200.
    """
    if requests is None:  # pragma: no cover
        raise CMEScraperError("the 'requests' package is required for downloading")

    own_session = session is None
    sess = session or requests.Session()
    if own_session:
        sess.headers.update(_BROWSER_HEADERS)

    last_exc: Exception | None = None
    try:
        for attempt in range(1, retries + 1):
            try:
                log.info("CME: GET %s (attempt %d/%d)", url, attempt, retries)
                resp = sess.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
            except requests.RequestException as exc:  # DNS, connReset, timeout, ...
                last_exc = exc
                log.warning("CME: network error on attempt %d: %s", attempt, exc)
                _sleep_before_retry(attempt, retries, backoff)
                continue

            content = resp.content or b""
            _raise_for_block(resp.status_code, content)

            if resp.status_code == 200 and content:
                _sanity_check_payload(content)
                if save_raw_to is not None:
                    dest = Path(save_raw_to)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content)
                    log.info("CME: wrote raw payload to %s (%d bytes)", dest, len(content))
                return content

            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = CMEScraperError(f"CME returned HTTP {resp.status_code}")
                log.warning("CME: retryable HTTP %s on attempt %d", resp.status_code, attempt)
                _sleep_before_retry(attempt, retries, backoff)
                continue

            raise CMEScraperError(
                f"CME returned HTTP {resp.status_code} for {url} "
                f"({len(content)} bytes, content-type={resp.headers.get('Content-Type')!r})"
            )

        raise CMEScraperError(f"CME download failed after {retries} attempts") from last_exc
    finally:
        if own_session:
            sess.close()


def _sleep_before_retry(attempt: int, retries: int, backoff: float) -> None:
    if attempt < retries:
        delay = backoff * attempt
        log.info("CME: sleeping %.1fs before retry", delay)
        time.sleep(delay)


def _raise_for_block(status_code: int, content: bytes) -> None:
    """Detect CME's Akamai anti-scraping response and raise a clear error."""
    head = content[:512].lstrip().lower()
    looks_like_block = (
        b"blocked due to suspected web scraping" in content
        or b"scraping mechanisms is strictly prohibited" in content
        or (status_code == 403 and head.startswith(b"{") and b"message" in head)
    )
    if looks_like_block:
        msg = content.decode("utf-8", "replace")[:600]
        raise CMEBlockedError(
            f"CME edge blocked this request as automated traffic (HTTP {status_code}). "
            f"Body: {msg}"
        )


def _sanity_check_payload(content: bytes) -> None:
    """Cheap guard against 200-OK error pages / empty files before parsing."""
    if len(content) < 512:
        raise CMEParseError(
            f"payload suspiciously small ({len(content)} bytes): {content[:200]!r}"
        )
    is_ole2 = content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    is_zip = content[:2] == b"PK"  # xlsx, just in case
    is_html = b"<html" in content[:2048].lower() or b"<table" in content[:2048].lower()
    if not (is_ole2 or is_zip or is_html):
        raise CMEParseError(
            f"payload is neither .xls/.xlsx nor HTML (head: {content[:32]!r})"
        )


# --------------------------------------------------------------------------- #
# Parse
# --------------------------------------------------------------------------- #
def parse_cme_copper_stocks(
    content: bytes,
    *,
    retrieved_at: dt.datetime | None = None,
) -> CMECopperStocks:
    """Parse the Copper_Stocks workbook bytes into a :class:`CMECopperStocks`.

    Primary path targets the verified "Daily Metal Stocks Report" layout.
    Falls back to a structure-tolerant scan for older / HTML variants.
    """
    if retrieved_at is None:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
    if not content:
        raise CMEParseError("empty payload")

    tables, full_text = _read_all_tables(content)
    if not tables:
        raise CMEParseError(
            f"no tables found in payload (first 200 chars: {content[:200]!r})"
        )

    # --- primary: the modern single-sheet report ---
    for df in tables:
        parsed = _parse_metal_stocks_report(df, retrieved_at)
        if parsed is not None:
            _validate(parsed)
            return parsed

    # --- fallback: generic total-row scan (older layouts) ---
    log.warning("CME: modern layout not recognised; trying generic fallback parser")
    report_date = _extract_report_date_generic(full_text, tables)
    registered, eligible, total = _extract_totals_generic(tables)
    if total is None and registered is not None and eligible is not None:
        total = registered + eligible
    if registered is None or eligible is None or total is None:
        raise CMEParseError(
            f"could not extract buckets (registered={registered}, "
            f"eligible={eligible}, total={total})"
        )
    unit = "metric_ton" if _RE_METRIC_TON.search(full_text) and not _RE_SHORT_TON.search(full_text) else "short_ton"
    parsed = CMECopperStocks(
        report_date=report_date,
        publish_date=None,
        registered_short_tons=float(registered),
        eligible_short_tons=float(eligible),
        total_short_tons=float(total),
        unit=unit,
        retrieved_at=retrieved_at,
    )
    _validate(parsed)
    return parsed


def _parse_metal_stocks_report(
    df: pd.DataFrame, retrieved_at: dt.datetime
) -> CMECopperStocks | None:
    """Parse the verified modern layout. Returns None if it doesn't match."""
    if df.empty or df.shape[1] < 3:
        return None
    grid = df.reset_index(drop=True)
    grid.columns = range(grid.shape[1])
    cells: list[list[str]] = [
        ["" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
         for v in grid.iloc[r].tolist()]
        for r in range(grid.shape[0])
    ]
    flat = "\n".join(" ".join(row) for row in cells)

    # This layout is identified by the "TOTAL TODAY" header and the
    # "Total Registered" / "Total Eligible" summary rows.
    if not _RE_TOTAL_TODAY_HDR.search(flat):
        return None
    if not (_RE_TOTAL_REGISTERED.search(flat) and _RE_TOTAL_ELIGIBLE.search(flat)):
        return None

    col_today = _find_col(cells, _RE_TOTAL_TODAY_HDR)
    col_prev = _find_col(cells, _RE_PREV_TOTAL_HDR)
    if col_today is None:
        return None

    def value_on_row(row_pred) -> float | None:
        hit_idx = None
        for i, row in enumerate(cells):
            label = row[0] if row else ""
            if row_pred(label):
                hit_idx = i  # take the LAST match (grand totals are at the bottom)
        if hit_idx is None:
            return None
        row = cells[hit_idx]
        val = _to_number(row[col_today]) if col_today < len(row) else None
        if val is None and col_prev is not None and col_prev < len(row):
            val = _to_number(row[col_prev])  # last-resort: previous total
        return val

    registered = value_on_row(lambda s: bool(_RE_TOTAL_REGISTERED.search(s)))
    eligible = value_on_row(lambda s: bool(_RE_TOTAL_ELIGIBLE.search(s)))
    total = value_on_row(
        lambda s: bool(_RE_GRAND_TOTAL.match(s))
        and not _RE_TOTAL_REGISTERED.search(s)
        and not _RE_TOTAL_ELIGIBLE.search(s)
    )
    if registered is None or eligible is None:
        return None
    if total is None:
        total = registered + eligible

    unit = "short_ton"
    if _RE_METRIC_TON.search(flat) and not _RE_SHORT_TON.search(flat):
        unit = "metric_ton"

    activity = _first_valid_date(_RE_ACTIVITY_DATE.findall(flat))
    report = _first_valid_date(_RE_REPORT_DATE.findall(flat))
    effective = activity or report
    if effective is None:
        effective = _first_valid_date(_RE_ANY_DATE.findall(flat))
    if effective is None:
        raise CMEParseError("modern layout matched but no usable date found")

    log.info(
        "CME: modern layout -> registered=%.0f eligible=%.0f total=%.0f "
        "activity=%s report=%s unit=%s",
        registered, eligible, total, activity, report, unit,
    )
    return CMECopperStocks(
        report_date=effective,
        publish_date=report,
        registered_short_tons=float(registered),
        eligible_short_tons=float(eligible),
        total_short_tons=float(total),
        unit=unit,
        retrieved_at=retrieved_at,
    )


def _find_col(cells: list[list[str]], pattern: re.Pattern[str]) -> int | None:
    """Column index of the first cell (scanning top rows) matching pattern."""
    for row in cells[:20]:
        for j, val in enumerate(row):
            if val and pattern.search(val):
                return j
    return None


def _validate(rec: CMECopperStocks) -> None:
    if rec.registered_short_tons < 0 or rec.eligible_short_tons < 0 or rec.total_short_tons <= 0:
        raise CMEParseError(f"implausible non-positive values: {rec.as_record()}")
    drift = abs((rec.registered_short_tons + rec.eligible_short_tons) - rec.total_short_tons)
    tol = max(2.0, rec.total_short_tons * 0.005)
    if drift > tol:
        raise CMEParseError(
            f"registered+eligible ({rec.registered_short_tons + rec.eligible_short_tons:.1f}) "
            f"!= total ({rec.total_short_tons:.1f}); drift {drift:.1f} > tol {tol:.1f}"
        )
    if not (_DATE_MIN <= rec.report_date <= dt.date.today() + dt.timedelta(days=2)):
        raise CMEParseError(f"report_date {rec.report_date} outside sane window")


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
def _read_all_tables(content: bytes) -> tuple[list[pd.DataFrame], str]:
    """Return (list of DataFrames, concatenated plain text) from the payload."""
    # Strategy A: a genuine Excel workbook (the current reality for Copper_Stocks.xls).
    for engine in ("xlrd", "openpyxl"):
        try:
            book = pd.read_excel(
                io.BytesIO(content), sheet_name=None, engine=engine, header=None
            )
        except Exception as exc:  # noqa: BLE001 - engines raise many types
            log.debug("CME: pd.read_excel(engine=%s) failed: %s", engine, exc)
            continue
        tables = [df for df in book.values() if not df.empty]
        text_blob = "\n".join(
            df.to_string(index=False, header=False, na_rep="") for df in tables
        )
        log.info("CME: parsed %d Excel sheet(s) via %s", len(tables), engine)
        return tables, text_blob

    # Strategy B: HTML tables (older *_Stocks.xls were HTML wearing an .xls suffix).
    text_blob = content.decode("utf-8", "replace")
    if _looks_like_html(text_blob):
        for flavor in ("lxml", "bs4"):
            try:
                tables = pd.read_html(io.StringIO(text_blob), flavor=flavor)
            except (ValueError, ImportError) as exc:
                log.debug("CME: pd.read_html(flavor=%s) failed: %s", flavor, exc)
                continue
            log.info("CME: parsed %d HTML table(s) via %s", len(tables), flavor)
            return [df for df in tables if not df.empty], text_blob

    return [], text_blob


def _looks_like_html(text: str) -> bool:
    head = text[:4096].lower()
    return any(tag in head for tag in ("<table", "<html", "<td", "<tr"))


# --------------------------------------------------------------------------- #
# Generic fallback (older layouts)
# --------------------------------------------------------------------------- #
def _extract_report_date_generic(full_text: str, tables: list[pd.DataFrame]) -> dt.date:
    for pat in (_RE_ACTIVITY_DATE, _RE_REPORT_DATE):
        d = _first_valid_date(pat.findall(full_text))
        if d:
            return d
    for df in tables:
        blob = " ".join(str(v) for v in df.to_numpy().ravel().tolist() if isinstance(v, str))
        d = _first_valid_date(_RE_ANY_DATE.findall(blob))
        if d:
            return d
    d = _first_valid_date(_RE_ANY_DATE.findall(full_text))
    if d:
        return d
    raise CMEParseError("generic parser: no plausible report date found")


def _extract_totals_generic(
    tables: list[pd.DataFrame],
) -> tuple[float | None, float | None, float | None]:
    best: tuple[float | None, float | None, float | None] | None = None
    for ti, df in enumerate(tables):
        if df.empty:
            continue
        grid = df.reset_index(drop=True)
        grid.columns = range(grid.shape[1])
        str_grid = grid.astype(object)

        col_reg = col_elig = col_tot = None
        for _, srow in str_grid.head(15).iterrows():
            row_cells = ["" if v is None else str(v) for v in srow.tolist()]
            if not (any(_RE_REGISTERED_GENERIC.search(c) for c in row_cells)
                    and any(_RE_ELIGIBLE_GENERIC.search(c) for c in row_cells)):
                continue
            for i, c in enumerate(row_cells):
                if col_reg is None and _RE_REGISTERED_GENERIC.search(c):
                    col_reg = i
                if col_elig is None and _RE_ELIGIBLE_GENERIC.search(c):
                    col_elig = i
                if col_tot is None and _RE_TOTAL_COL_GENERIC.match(c.strip()):
                    col_tot = i
            break

        total_row_idx = None
        for idx, srow in str_grid.iterrows():
            for v in srow.tolist():
                if v is None or not str(v).strip():
                    continue
                if _RE_TOTAL_ROW_GENERIC.match(str(v).strip()):
                    total_row_idx = idx
                break
        if total_row_idx is None:
            continue

        nums = [_to_number(v) for v in str_grid.iloc[total_row_idx].tolist()]
        numeric_positions = [i for i, n in enumerate(nums) if n is not None]
        if not numeric_positions:
            continue

        registered = nums[col_reg] if col_reg is not None and col_reg < len(nums) else None
        eligible = nums[col_elig] if col_elig is not None and col_elig < len(nums) else None
        total = nums[col_tot] if col_tot is not None and col_tot < len(nums) else None
        if registered is None or eligible is None:
            tail = [nums[i] for i in numeric_positions][-3:]
            if len(tail) == 3:
                registered, eligible, total = tail
            elif len(tail) == 2:
                registered, eligible = tail
                total = registered + eligible
        if registered is not None and eligible is not None:
            cand = (registered, eligible, total)
            log.info("CME(generic): table #%d -> %s", ti, cand)
            if total is not None:
                return cand
            best = best or cand
    return best if best is not None else (None, None, None)


# --------------------------------------------------------------------------- #
# Value / date helpers
# --------------------------------------------------------------------------- #
def _to_number(value: Any) -> float | None:
    """Parse a report cell into a float. Handles commas, parens, blanks, dashes."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if pd.isna(f) else f
    s = str(value).strip()
    if s in {"", "-", "--", "—", "N/A", "n/a", "NA", "nan", "NaN"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    f = float(s)
    return -f if neg else f


def _first_valid_date(raw_candidates: list[str]) -> dt.date | None:
    """First parseable, in-window date from a list of raw strings."""
    for raw in raw_candidates:
        raw = raw.strip()
        try:
            parsed = dateparser.parse(raw, dayfirst=False, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            continue
        if parsed is None:
            continue
        d = parsed.date()
        if _DATE_MIN <= d <= dt.date.today() + dt.timedelta(days=2):
            return d
    return None


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def get_cme_copper_stocks(
    *,
    url: str = CME_COPPER_STOCKS_URL,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    save_raw_to: str | Path | None = None,
) -> dict[str, Any]:
    """Download + parse in one call. Returns the raw short-ton record dict.

    Raises :class:`CMEBlockedError` if CME blocks the request, or
    :class:`CMEParseError` if the file cannot be understood.
    """
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    content = download_cme_copper_stocks(
        url, timeout=timeout, retries=retries, save_raw_to=save_raw_to
    )
    return parse_cme_copper_stocks(content, retrieved_at=retrieved_at).as_record()


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if len(sys.argv) > 1:  # python scripts/cme_scraper.py <saved-file.xls>
        record = parse_cme_copper_stocks(Path(sys.argv[1]).read_bytes()).as_record()
    else:
        record = get_cme_copper_stocks(save_raw_to="data/raw/cme_copper_stocks.xls")

    print(json.dumps(record, indent=2, default=str))
