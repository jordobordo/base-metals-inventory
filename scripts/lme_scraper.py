"""
STEP 2 — LME copper daily Live (on-warrant) + Cancelled warrant scraper.

Source
------
LME "Stock breakdown report" (two-business-day delayed), the report the user
pointed at:

    https://www.lme.com/market-data/reports-and-data/warehouse-and-stocks-reports/stock-breakdown-report

That listing page is a Cloudflare-fronted Vue app; the download links are not in
the HTML. It is driven by a small JSON API which we call directly:

    LIST      GET /Lme-api/ReportsListingSearchApi/Get?SearchConfigId=<guid>&DateFacet=Last 7 days
              -> {"Results":[{"ItemId": "...", "Name": "Metals Reports 26 Aug 2026",
                              "FileExtension": "xls"}, ...], "DateFacets": [...], ...}
    DOWNLOAD  GET /Lme-api/ReportsListingSearchApi/Download?id=<ItemId>
              -> the .xls bytes  (Content-Disposition: Metals Reports_20260826.xls)

Cloudflare passively fingerprints the TLS/JA3 handshake, so a plain ``requests``
call gets a 403 "Just a moment" page. We use ``curl_cffi`` with Chrome
impersonation, which passes. (The LME media CDN — ``/-/media/Files/...`` — is not
fingerprinted and downloads fine with plain requests, but the *current* daily
breakdown files are only reachable through the API above; the ``/-/media`` path
only holds pre-2018 archives.)

The workbook
------------
Genuine BIFF ``.xls`` (OLE2), 4 sheets. We use **"Metals Totals Report"**, which
has one section per metal:

    <blank>            Copper
    Country/Region  Location  Opening Stock  Delivered In  Delivered Out  Closing Stock  Open Tonnage  Cancelled Tonnage
    Belgium         Antwerp   ...
    ...
    Total                     237475         150            2050           235575         107050        128525
    +/-                       -1250                                        -1900

We read the **Total** row of the Copper section:

    * live_warrant_tonnes      <- "Open Tonnage"       (metal under live warrant, deliverable)
    * cancelled_warrant_tonnes <- "Cancelled Tonnage"  (warrants cancelled, awaiting load-out)
    * total_on_warrant_tonnes  <- "Closing Stock"      (== live + cancelled)

Already in **metric tonnes** — no conversion. Off-warrant stock is a separate
T+3 report (``Daily_OWSR``, SearchConfigId ``0626424c-ad1d-4f52-b24c-05b6f136226f``);
that plumbing is identical and can be added when STEP 4 needs it.

Date handling
-------------
There is no date cell inside the file. ``report_date`` is parsed from the report
``Name`` ("Metals Reports 26 Aug 2026") / the download filename
("Metals Reports_20260826.xls"). Note the LME/press convention offset: this file's
"Closing Stock" is the close **of** its stated date; some aggregators (e.g.
Westmetall) label the same number one calendar day later. STEP 4 owns any shift.

Fragility note
--------------
This depends on: curl_cffi beating Cloudflare from the CI runner, the
``SearchConfigId`` GUID (auto-discovered from the page if the hard-coded one
stops working), and the ``/Lme-api/ReportsListingSearchApi`` contract. All three
are more brittle than the CME path — raise loudly, don't paper over.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import logging
import re
import time
from typing import Any

import pandas as pd
from dateutil import parser as dateparser

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - curl_cffi is in requirements.txt
    cffi_requests = None  # type: ignore[assignment]

__all__ = [
    "get_lme_copper_warrants",
    "get_lme_copper_offwarrant",
    "list_lme_reports",
    "list_lme_stock_breakdown_reports",
    "download_lme_report",
    "download_lme_stock_breakdown",
    "parse_lme_stock_breakdown",
    "parse_lme_offwarrant",
    "LMECopperWarrants",
    "LMECopperOffWarrant",
    "LMEScraperError",
    "LMEBlockedError",
    "LMEParseError",
]

log = logging.getLogger(__name__)

LME_BASE = "https://www.lme.com"
STOCK_BREAKDOWN_PAGE = (
    f"{LME_BASE}/market-data/reports-and-data/warehouse-and-stocks-reports/stock-breakdown-report"
)
OFF_WARRANT_PAGE = (
    f"{LME_BASE}/market-data/reports-and-data/warehouse-and-stocks-reports/off-warrant-stock-reporting"
)
REPORTS_LIST_API = f"{LME_BASE}/Lme-api/ReportsListingSearchApi/Get"
REPORTS_DOWNLOAD_API = f"{LME_BASE}/Lme-api/ReportsListingSearchApi/Download"

# SearchConfigId values, discovered from each page's <search-reports
# api-config="..."> attribute. Auto-rediscovered from the page if one 400s.
STOCK_BREAKDOWN_CONFIG_ID = "c7ffb6ea-32dc-47a1-a732-456516373b4c"
OFF_WARRANT_CONFIG_ID = "0626424c-ad1d-4f52-b24c-05b6f136226f"

DEFAULT_IMPERSONATE = "chrome"
DEFAULT_TIMEOUT = 45
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 4.0
DEFAULT_MAX_REPORTS_TO_TRY = 5

_METAL_DEFAULT = "Copper"
_TOTALS_SHEET_HINT = "Metals Totals Report"

_RE_HDR_OPEN = re.compile(r"open\s*tonnage", re.IGNORECASE)
_RE_HDR_CANCELLED = re.compile(r"cancel", re.IGNORECASE)
_RE_HDR_CLOSING = re.compile(r"closing\s*stock", re.IGNORECASE)
_RE_HDR_OPENING = re.compile(r"opening\s*stock", re.IGNORECASE)
_RE_HDR_DEL_IN = re.compile(r"delivered\s*in", re.IGNORECASE)
_RE_HDR_DEL_OUT = re.compile(r"delivered\s*out", re.IGNORECASE)
_RE_TOTAL_ROW = re.compile(r"^\s*total\s*$", re.IGNORECASE)
_RE_CONFIG_ATTR = re.compile(r'api-config="([0-9a-fA-F-]{36})"')
_RE_NAME_DATE = re.compile(r"(\d{1,2})[ _-]([A-Za-z]{3,9})[ _-](\d{4})")
_RE_FILE_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")

_DATE_MIN = dt.date(2000, 1, 1)
_CF_MARKERS = (b"Just a moment", b"cf-chl", b"cf-browser-verification", b"/cdn-cgi/challenge-platform")


class LMEScraperError(RuntimeError):
    """Base class for LME scraper failures."""


class LMEBlockedError(LMEScraperError):
    """Cloudflare / bot-challenge blocked the request."""


class LMEParseError(LMEScraperError):
    """A file downloaded but its structure could not be understood."""


@dataclasses.dataclass(slots=True)
class LMECopperWarrants:
    """One day's LME copper warrant snapshot, in metric tonnes."""

    report_date: dt.date
    metal: str
    live_warrant_tonnes: float
    cancelled_warrant_tonnes: float
    total_on_warrant_tonnes: float
    opening_stock_tonnes: float
    delivered_in_tonnes: float
    delivered_out_tonnes: float
    source_report_name: str
    retrieved_at: dt.datetime

    def as_record(self) -> dict[str, Any]:
        return {
            "source": "LME",
            "report_date": self.report_date,
            "metal": self.metal,
            "live_warrant_tonnes": self.live_warrant_tonnes,
            "cancelled_warrant_tonnes": self.cancelled_warrant_tonnes,
            "total_on_warrant_tonnes": self.total_on_warrant_tonnes,
            "opening_stock_tonnes": self.opening_stock_tonnes,
            "delivered_in_tonnes": self.delivered_in_tonnes,
            "delivered_out_tonnes": self.delivered_out_tonnes,
            "unit": "metric_tonne",
            "source_report_name": self.source_report_name,
            "retrieved_at": self.retrieved_at,
        }


# --------------------------------------------------------------------------- #
# HTTP session
# --------------------------------------------------------------------------- #
def _new_session(impersonate: str = DEFAULT_IMPERSONATE):
    if cffi_requests is None:  # pragma: no cover
        raise LMEScraperError(
            "curl_cffi is required for the LME scraper (Cloudflare TLS fingerprinting). "
            "pip install curl_cffi"
        )
    sess = cffi_requests.Session(impersonate=impersonate)
    sess.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{STOCK_BREAKDOWN_PAGE}?page=1&DateFacet=Last+7+days",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return sess


def _get(sess, url: str, *, params=None, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES,
         backoff=DEFAULT_BACKOFF, expect: str = "any"):
    """GET with retries + Cloudflare-block detection. ``expect`` in {"any","json","binary"}."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - curl_cffi raises its own errors
            last = exc
            log.warning("LME: network error on attempt %d for %s: %s", attempt, url, exc)
            _sleep(attempt, retries, backoff)
            continue

        body = resp.content or b""
        if resp.status_code in (403, 429, 503) or any(m in body[:4096] for m in _CF_MARKERS):
            if any(m in body[:4096] for m in _CF_MARKERS):
                raise LMEBlockedError(
                    f"Cloudflare challenge on {url} (HTTP {resp.status_code}); "
                    "curl_cffi impersonation did not pass."
                )
            last = LMEScraperError(f"HTTP {resp.status_code} for {url}")
            log.warning("LME: HTTP %s on attempt %d for %s", resp.status_code, attempt, url)
            _sleep(attempt, retries, backoff)
            continue

        if resp.status_code != 200:
            raise LMEScraperError(f"HTTP {resp.status_code} for {url}: {body[:200]!r}")

        if expect == "json" and not body[:1] in (b"{", b"["):
            raise LMEScraperError(f"expected JSON from {url}, got {body[:120]!r}")
        if expect == "binary" and body[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" and body[:2] != b"PK":
            raise LMEParseError(f"expected an Excel workbook from {url}, got {body[:32]!r}")
        return resp

    raise LMEScraperError(f"LME GET failed after {retries} attempts: {url}") from last


def _sleep(attempt: int, retries: int, backoff: float) -> None:
    if attempt < retries:
        delay = backoff * attempt
        log.info("LME: sleeping %.1fs before retry", delay)
        time.sleep(delay)


# --------------------------------------------------------------------------- #
# Listing + download
# --------------------------------------------------------------------------- #
def _discover_config_id(sess, page_url: str = STOCK_BREAKDOWN_PAGE) -> str:
    """Scrape a reports listing page for its <search-reports api-config>."""
    resp = _get(sess, f"{page_url}?page=1&DateFacet=Last+7+days")
    ids = _RE_CONFIG_ATTR.findall(resp.text)
    # The first api-config on the page is the global site search; the reports
    # listing component's is the last one.
    if not ids:
        raise LMEScraperError(f"could not discover SearchConfigId from {page_url}")
    log.info("LME: discovered SearchConfigId=%s from %s", ids[-1], page_url)
    return ids[-1]


def list_lme_reports(
    *,
    config_id: str,
    listing_page: str = STOCK_BREAKDOWN_PAGE,
    date_facets: tuple[str, ...] = ("Last 7 days", "Current month"),
    impersonate: str = DEFAULT_IMPERSONATE,
    session=None,
) -> list[dict[str, Any]]:
    """Return the available reports for a listing config, newest first.

    Each item: {"item_id", "name", "file_extension", "report_date"(date|None)}.
    If ``config_id`` 400s it is re-discovered from ``listing_page`` once.
    """
    own = session is None
    sess = session or _new_session(impersonate)
    try:
        cfg = config_id
        for facet in date_facets:
            try:
                resp = _get(sess, REPORTS_LIST_API,
                            params={"SearchConfigId": cfg, "DateFacet": facet}, expect="json")
            except LMEScraperError as exc:
                if "HTTP 400" in str(exc) and cfg == config_id:
                    cfg = _discover_config_id(sess, listing_page)
                    resp = _get(sess, REPORTS_LIST_API,
                                params={"SearchConfigId": cfg, "DateFacet": facet}, expect="json")
                else:
                    raise
            results = resp.json().get("Results") or []
            if results:
                items = [
                    {
                        "item_id": r.get("ItemId"),
                        "name": r.get("Name", ""),
                        "file_extension": (r.get("FileExtension") or "xls").lower(),
                        "report_date": _date_from_name(r.get("Name", "")),
                    }
                    for r in results
                    if r.get("ItemId")
                ]
                items.sort(key=lambda d: d["report_date"] or _DATE_MIN, reverse=True)
                log.info("LME: %d report(s) for facet %r (config %s)", len(items), facet, cfg)
                return items
            log.info("LME: no results for facet %r, trying next", facet)
        return []
    finally:
        if own:
            sess.close()


def list_lme_stock_breakdown_reports(**kwargs) -> list[dict[str, Any]]:
    """Back-compat wrapper: :func:`list_lme_reports` for the stock-breakdown config."""
    kwargs.setdefault("config_id", STOCK_BREAKDOWN_CONFIG_ID)
    kwargs.setdefault("listing_page", STOCK_BREAKDOWN_PAGE)
    return list_lme_reports(**kwargs)


def download_lme_report(
    item_id: str,
    *,
    impersonate: str = DEFAULT_IMPERSONATE,
    session=None,
) -> tuple[bytes, str]:
    """Download one report by ItemId. Returns (content_bytes, filename)."""
    own = session is None
    sess = session or _new_session(impersonate)
    try:
        resp = _get(sess, REPORTS_DOWNLOAD_API, params={"id": item_id}, expect="binary")
        cd = resp.headers.get("Content-Disposition", "") or ""
        m = re.search(r'filename="?([^";]+)"?', cd)
        filename = m.group(1).strip() if m else f"{item_id}.xls"
        log.info("LME: downloaded %s (%d bytes)", filename, len(resp.content))
        return resp.content, filename
    finally:
        if own:
            sess.close()


# Back-compat alias.
download_lme_stock_breakdown = download_lme_report


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_lme_stock_breakdown(
    content: bytes,
    *,
    report_date: dt.date | None = None,
    source_report_name: str = "",
    metal: str = _METAL_DEFAULT,
    retrieved_at: dt.datetime | None = None,
) -> LMECopperWarrants:
    """Parse the 'Metals Totals Report' sheet and return the metal's Total row."""
    if retrieved_at is None:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
    if not content:
        raise LMEParseError("empty payload")

    try:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="xlrd")
    except Exception:
        try:
            book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="openpyxl")
        except Exception as exc:  # noqa: BLE001
            raise LMEParseError(f"could not open workbook: {exc}") from exc

    sheet = _pick_totals_sheet(book)
    grid = _as_str_grid(sheet)

    sec_start, sec_end = _find_metal_section(grid, metal)
    col = _find_columns(grid, sec_start, sec_end)
    total_row = _find_total_row(grid, sec_start, sec_end)

    def val(name: str) -> float:
        j = col.get(name)
        if j is None or j >= len(grid[total_row]):
            raise LMEParseError(f"column {name!r} not found in {metal} section")
        n = _to_number(grid[total_row][j])
        if n is None:
            raise LMEParseError(f"non-numeric {name!r} on Total row: {grid[total_row][j]!r}")
        return n

    live = val("open")
    cancelled = val("cancelled")
    closing = val("closing")
    opening = val("opening")
    del_in = val("delivered_in")
    del_out = val("delivered_out")

    if report_date is None:
        report_date = _date_from_name(source_report_name)
    if report_date is None:
        raise LMEParseError("no report_date supplied and none parseable from the report name")

    rec = LMECopperWarrants(
        report_date=report_date,
        metal=metal,
        live_warrant_tonnes=live,
        cancelled_warrant_tonnes=cancelled,
        total_on_warrant_tonnes=closing,
        opening_stock_tonnes=opening,
        delivered_in_tonnes=del_in,
        delivered_out_tonnes=del_out,
        source_report_name=source_report_name,
        retrieved_at=retrieved_at,
    )
    _validate(rec)
    return rec


def _pick_totals_sheet(book: dict[str, pd.DataFrame]) -> pd.DataFrame:
    for name, df in book.items():
        if _TOTALS_SHEET_HINT.lower() in str(name).lower():
            return df
    # Fallback: the sheet that mentions "Open Tonnage" but not "Premium".
    for name, df in book.items():
        if "premium" in str(name).lower():
            continue
        blob = " ".join(str(v) for v in df.to_numpy().ravel()[:400] if isinstance(v, str)).lower()
        if "open tonnage" in blob and "cancelled tonnage" in blob:
            return df
    raise LMEParseError(f"no 'Metals Totals Report' sheet among {list(book)}")


def _as_str_grid(df: pd.DataFrame) -> list[list[str]]:
    out: list[list[str]] = []
    for r in range(df.shape[0]):
        row = []
        for v in df.iloc[r].tolist():
            if v is None or (isinstance(v, float) and pd.isna(v)):
                row.append("")
            else:
                row.append(str(v).strip())
        out.append(row)
    return out


def _find_metal_section(grid: list[list[str]], metal: str) -> tuple[int, int]:
    """Row range [start, end) for the metal's block (header cell -> next metal header)."""
    target = metal.strip().lower()
    starts: list[int] = []
    header_like = re.compile(r"^[A-Za-z][A-Za-z /()-]{1,40}$")
    for i, row in enumerate(grid):
        c1 = row[1] if len(row) > 1 else ""
        if c1.lower() == target:
            starts.append(i)
    if not starts:
        # tolerate "Copper" vs "Copper Cathodes" etc.
        for i, row in enumerate(grid):
            c1 = row[1] if len(row) > 1 else ""
            if c1.lower().startswith(target):
                starts.append(i)
    if not starts:
        raise LMEParseError(f"metal section {metal!r} not found in Metals Totals Report")
    start = starts[0]

    # Section ends at the next row that looks like a lone metal name in col 1.
    end = len(grid)
    for i in range(start + 3, len(grid)):
        row = grid[i]
        c0 = row[0] if row else ""
        c1 = row[1] if len(row) > 1 else ""
        rest_empty = all(not c for c in row[2:8]) if len(row) >= 8 else True
        if not c0 and c1 and header_like.match(c1) and rest_empty and c1.lower() != target:
            end = i
            break
    return start, end


def _find_columns(grid: list[list[str]], start: int, end: int) -> dict[str, int]:
    for i in range(start, min(end, start + 8)):
        row = grid[i]
        joined = " | ".join(row).lower()
        if "open tonnage" in joined and "cancelled tonnage" in joined:
            col: dict[str, int] = {}
            for j, cell in enumerate(row):
                c = cell.lower()
                if _RE_HDR_OPENING.search(c):
                    col["opening"] = j
                elif _RE_HDR_DEL_IN.search(c):
                    col["delivered_in"] = j
                elif _RE_HDR_DEL_OUT.search(c):
                    col["delivered_out"] = j
                elif _RE_HDR_CLOSING.search(c):
                    col["closing"] = j
                elif _RE_HDR_OPEN.search(c):
                    col["open"] = j
                elif _RE_HDR_CANCELLED.search(c):
                    col["cancelled"] = j
            missing = {"opening", "delivered_in", "delivered_out", "closing", "open", "cancelled"} - col.keys()
            if missing:
                raise LMEParseError(f"header row found but missing columns: {sorted(missing)}")
            return col
    raise LMEParseError("could not find the Country/Location header row in the metal section")


def _find_total_row(grid: list[list[str]], start: int, end: int) -> int:
    for i in range(start, end):
        c0 = grid[i][0] if grid[i] else ""
        if _RE_TOTAL_ROW.match(c0):
            return i
    raise LMEParseError("no 'Total' row in the metal section")


def _validate(rec: LMECopperWarrants) -> None:
    vals = [rec.live_warrant_tonnes, rec.cancelled_warrant_tonnes, rec.total_on_warrant_tonnes]
    if any(v < 0 for v in vals):
        raise LMEParseError(f"negative tonnage in {rec.as_record()}")
    if rec.total_on_warrant_tonnes <= 0:
        raise LMEParseError(f"non-positive closing stock: {rec.total_on_warrant_tonnes}")
    drift = abs((rec.live_warrant_tonnes + rec.cancelled_warrant_tonnes) - rec.total_on_warrant_tonnes)
    tol = max(1.0, rec.total_on_warrant_tonnes * 0.005)
    if drift > tol:
        raise LMEParseError(
            f"live ({rec.live_warrant_tonnes}) + cancelled ({rec.cancelled_warrant_tonnes}) "
            f"!= closing ({rec.total_on_warrant_tonnes}); drift {drift:.1f} > tol {tol:.1f}"
        )
    if not (_DATE_MIN <= rec.report_date <= dt.date.today() + dt.timedelta(days=2)):
        raise LMEParseError(f"report_date {rec.report_date} outside sane window")


# --------------------------------------------------------------------------- #
# Value / date helpers
# --------------------------------------------------------------------------- #
def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if pd.isna(f) else f
    s = str(value).strip()
    if s in {"", "-", "--", "—", "N/A", "n/a", "NA", "nan"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    f = float(s)
    return -f if neg else f


def _date_from_name(name: str) -> dt.date | None:
    """'Metals Reports 26 Aug 2026' or 'Metals Reports_20260826.xls' -> date."""
    if not name:
        return None
    m = _RE_NAME_DATE.search(name)
    if m:
        try:
            d = dateparser.parse(f"{m.group(1)} {m.group(2)} {m.group(3)}").date()
            if _DATE_MIN <= d <= dt.date.today() + dt.timedelta(days=2):
                return d
        except (ValueError, OverflowError):
            pass
    m = _RE_FILE_DATE.search(name)
    if m:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if _DATE_MIN <= d <= dt.date.today() + dt.timedelta(days=2):
                return d
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def get_lme_copper_warrants(
    *,
    metal: str = _METAL_DEFAULT,
    max_reports_to_try: int = DEFAULT_MAX_REPORTS_TO_TRY,
    config_id: str = STOCK_BREAKDOWN_CONFIG_ID,
    impersonate: str = DEFAULT_IMPERSONATE,
) -> dict[str, Any]:
    """List -> download -> parse the newest good stock-breakdown report.

    Returns the raw metric-tonne record dict. Raises :class:`LMEBlockedError`
    if Cloudflare blocks us, :class:`LMEParseError` if no report parses.
    """
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    sess = _new_session(impersonate)
    try:
        reports = list_lme_stock_breakdown_reports(config_id=config_id, session=sess)
        if not reports:
            raise LMEScraperError("no stock-breakdown reports listed for any date facet")

        errors: list[str] = []
        for item in reports[:max_reports_to_try]:
            try:
                content, filename = download_lme_stock_breakdown(item["item_id"], session=sess)
                rec = parse_lme_stock_breakdown(
                    content,
                    report_date=item["report_date"] or _date_from_name(filename),
                    source_report_name=item["name"] or filename,
                    metal=metal,
                    retrieved_at=retrieved_at,
                )
                return rec.as_record()
            except LMEBlockedError:
                raise
            except LMEScraperError as exc:
                log.warning("LME: report %r failed: %s", item["name"], exc)
                errors.append(f"{item['name']}: {exc}")

        raise LMEParseError(
            "no stock-breakdown report parsed. Tried:\n  " + "\n  ".join(errors)
        )
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# Off-warrant stock report (Daily_OWSR, T+3) — separate report the user asked for
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(slots=True)
class LMECopperOffWarrant:
    """LME copper off-warrant stock (metal in LME-listed sheds, not warranted)."""

    report_date: dt.date
    metal: str
    off_warrant_tonnes: float
    source_report_name: str
    retrieved_at: dt.datetime

    def as_record(self) -> dict[str, Any]:
        return {
            "source": "LME",
            "report_date": self.report_date,
            "metal": self.metal,
            "off_warrant_tonnes": self.off_warrant_tonnes,
            "unit": "metric_tonne",
            "source_report_name": self.source_report_name,
            "retrieved_at": self.retrieved_at,
        }


# CU column header in the OWSR "Reconciled Inventory" sheet, and the grand-total row.
_OWSR_METAL_CODES = {"Copper": "CU"}
_RE_OWSR_GLOBAL_TOTAL = re.compile(r"^\s*global\s+total\b", re.IGNORECASE)


def parse_lme_offwarrant(
    content: bytes,
    *,
    report_date: dt.date | None = None,
    source_report_name: str = "",
    metal: str = _METAL_DEFAULT,
    retrieved_at: dt.datetime | None = None,
) -> LMECopperOffWarrant:
    """Parse the Daily_OWSR workbook: the metal's column on the GLOBAL TOTAL row.

    Sheet 'OWSR Reconciled Inventory'. Header row:
        REGION | COUNTRY/REGION | DELIVERY POINT | AA | AL | CU | ... | NI | PB | SN | ZN | CO | TOTAL
    We locate the metal's 2-letter code column, then read the 'GLOBAL TOTAL' row.
    """
    if retrieved_at is None:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
    if not content:
        raise LMEParseError("empty OWSR payload")

    code = _OWSR_METAL_CODES.get(metal, metal.strip().upper())
    try:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="openpyxl")
    except Exception as exc:  # noqa: BLE001
        raise LMEParseError(f"could not open OWSR workbook: {exc}") from exc

    df = next(
        (d for n, d in book.items() if "reconciled" in str(n).lower() or "owsr" in str(n).lower()),
        next(iter(book.values()), None),
    )
    if df is None or df.empty:
        raise LMEParseError("OWSR workbook has no usable sheet")

    grid = _as_str_grid(df)
    col_idx = hdr_idx = None
    for i, row in enumerate(grid[:12]):
        upper = [c.strip().upper() for c in row]
        if "REGION" in upper and code in upper:
            hdr_idx = i
            col_idx = upper.index(code)
            break
    if col_idx is None:
        raise LMEParseError(f"OWSR: header row with a {code!r} column not found")

    for row in grid[hdr_idx + 1:]:
        if row and _RE_OWSR_GLOBAL_TOTAL.match(row[0]):
            val = _to_number(row[col_idx]) if col_idx < len(row) else None
            if val is None:
                raise LMEParseError(f"OWSR GLOBAL TOTAL {code} cell not numeric: {row[col_idx]!r}")
            if val < 0:
                raise LMEParseError(f"OWSR {code} off-warrant negative: {val}")
            if report_date is None:
                report_date = _date_from_name(source_report_name)
            if report_date is None:
                raise LMEParseError("OWSR: no report_date supplied and none parseable")
            log.info("LME OWSR %s: %s off-warrant = %.0f t", report_date, code, val)
            return LMECopperOffWarrant(
                report_date=report_date,
                metal=metal,
                off_warrant_tonnes=val,
                source_report_name=source_report_name,
                retrieved_at=retrieved_at,
            )
    raise LMEParseError("OWSR: no 'GLOBAL TOTAL' row found")


def get_lme_copper_offwarrant(
    *,
    metal: str = _METAL_DEFAULT,
    max_reports_to_try: int = DEFAULT_MAX_REPORTS_TO_TRY,
    config_id: str = OFF_WARRANT_CONFIG_ID,
    impersonate: str = DEFAULT_IMPERSONATE,
) -> dict[str, Any]:
    """List -> download -> parse the newest good Daily_OWSR off-warrant report."""
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    sess = _new_session(impersonate)
    try:
        reports = list_lme_reports(
            config_id=config_id, listing_page=OFF_WARRANT_PAGE, session=sess
        )
        if not reports:
            raise LMEScraperError("no off-warrant (OWSR) reports listed for any date facet")

        errors: list[str] = []
        for item in reports[:max_reports_to_try]:
            try:
                content, filename = download_lme_report(item["item_id"], session=sess)
                rec = parse_lme_offwarrant(
                    content,
                    report_date=item["report_date"] or _date_from_name(filename),
                    source_report_name=item["name"] or filename,
                    metal=metal,
                    retrieved_at=retrieved_at,
                )
                return rec.as_record()
            except LMEBlockedError:
                raise
            except LMEScraperError as exc:
                log.warning("LME: OWSR report %r failed: %s", item["name"], exc)
                errors.append(f"{item['name']}: {exc}")
        raise LMEParseError("no OWSR report parsed. Tried:\n  " + "\n  ".join(errors))
    finally:
        sess.close()


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] in ("--offwarrant", "-o"):
        out = get_lme_copper_offwarrant()
    elif len(sys.argv) > 1:  # python scripts/lme_scraper.py <saved.xls> [YYYY-MM-DD]
        from pathlib import Path

        p = Path(sys.argv[1])
        raw = p.read_bytes()
        rd = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
        if "owsr" in p.name.lower():
            out = parse_lme_offwarrant(raw, report_date=rd, source_report_name=p.name).as_record()
        else:
            out = parse_lme_stock_breakdown(raw, report_date=rd, source_report_name=p.name).as_record()
    else:
        out = get_lme_copper_warrants()

    print(json.dumps(out, indent=2, default=str))
