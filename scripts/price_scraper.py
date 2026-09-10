"""
CME (COMEX) vs LME copper price spread.

Pulls the previous completed session's close (market-on-close) for both legs and
returns the spread in USD per metric tonne:

    cme_lme_spread_3m_usd_t = COMEX front-active HG (lb -> tonne)  -  LME 3-month
    cme_lme_spread_usd_t    = same COMEX leg                       -  LME cash

Sources (both free, no key):
  * COMEX copper : CME Group's own settlements API
                   (``/CmeWS/mvc/Settlements/Futures/Settlements/438/FUT``, product
                   438 = Copper), fetched with curl_cffi. Gives the **official
                   settlement** for every contract month on a given tradeDate; we
                   take the **most-active** month (highest open interest — usually
                   the Dec/quarterly, not the near-expiry front) for the previous
                   trading day. Falls back to Yahoo ``HG=F`` if CME blocks the runner.
                   (Westmetall carries no COMEX data, so this leg has no
                   Westmetall path.)
  * LME copper   : the Westmetall "LME_Cu_cash" table — LME Copper
                   Cash-Settlement + 3-month, one row per trading day. Plain HTML,
                   no Cloudflare, and it is already the reference the rest of the
                   repo sanity-checks ``lme_total_t`` against.

Both legs soft-fail independently; :func:`get_cme_lme_copper_spread` returns
whatever it could get and only fills the spread when both legs are present
(a warning is logged if their price dates differ).
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

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None  # type: ignore[assignment]

__all__ = [
    "get_cme_lme_copper_spread",
    "get_comex_copper_price",
    "get_lme_copper_price",
    "PriceScraperError",
]

log = logging.getLogger(__name__)

# 1 metric tonne = 1000 kg = 2204.622621848... lb
LB_PER_TONNE = 2204.6226218488

YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
YAHOO_CHART = "/v8/finance/chart/HG=F?range=1mo&interval=1d"
WESTMETALL_LME_CU = "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash"
# Westmetall daily rows quote the LME Official (settlement) cash + 3-month; the
# table is 6 months deep, more than enough to pick "latest before the cutoff".

# CME Group settlements API — product 438 = Copper.
CME_COPPER_SETTLE_URL = (
    "https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements/438/FUT"
    "?tradeDate={date}&strategy=DEFAULT&pageSize=90"
)
CME_REFERER = "https://www.cmegroup.com/markets/metals/base/copper.settlements.html"
_MONTH_CODE = "FGHJKMNQUVXZ"  # Jan..Dec

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30
_RE_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_RE_TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_RE_TAG = re.compile(r"<[^>]+>")
_DATE_MIN = dt.date(2000, 1, 1)


class PriceScraperError(RuntimeError):
    """A price leg could not be fetched or parsed."""


@dataclasses.dataclass(slots=True)
class ComexCopperPrice:
    price_date: dt.date
    usd_per_lb: float
    contract: str  # e.g. "HGZ26" (most-active month) or "HG=F" (Yahoo fallback)
    source: str = "CME"

    @property
    def usd_per_tonne(self) -> float:
        return round(self.usd_per_lb * LB_PER_TONNE, 2)


@dataclasses.dataclass(slots=True)
class LmeCopperPrice:
    price_date: dt.date
    cash_usd_per_tonne: float | None
    three_month_usd_per_tonne: float | None
    source: str = "Westmetall"


# --------------------------------------------------------------------------- #
# COMEX  (CME settlements API — official settle of the most-active month)
# --------------------------------------------------------------------------- #
def _to_settle(raw: str) -> float | None:
    m = re.match(r"-?\d+(?:\.\d+)?", (raw or "").strip().replace(",", ""))
    return float(m.group(0)) if m else None


def _contract_code(month_label: str) -> str:
    """'DEC 26' -> 'HGZ26'."""
    try:
        mon = dt.datetime.strptime(month_label.strip()[:3], "%b").month
        yy = month_label.strip()[-2:]
        return f"HG{_MONTH_CODE[mon - 1]}{yy}"
    except (ValueError, IndexError):
        return month_label.strip().replace(" ", "")


def _comex_from_cme(cutoff: dt.date) -> ComexCopperPrice:
    """Official CME settlement for the most-active copper month, latest tradeDate
    strictly before ``cutoff``.

    The CmeWS settlements endpoint is flaky for the *most recent* trade date — it
    intermittently returns an empty ``settlements`` array for a date that does
    have a settlement (the empty windows can last minutes, so retrying inside one
    scraper run only beats short blips). A miss makes the scraper walk back and
    serve an older — but now **date-aligned** (see :func:`get_cme_lme_copper_spread`)
    and clearly dated — settlement, rather than a wrong same-day number. The 1-3
    most recent candidate dates are retried a few times; older empty dates are
    genuine holidays/weekends and get one shot.
    """
    errors: list[str] = []
    for k in range(1, 9):
        d = cutoff - dt.timedelta(days=k)
        url = CME_COPPER_SETTLE_URL.format(date=d.strftime("%m/%d/%Y"))
        tries = 3 if k <= 3 else 1
        for attempt in range(1, tries + 1):
            try:
                r = cffi_requests.get(
                    url, impersonate="chrome", timeout=DEFAULT_TIMEOUT,
                    headers={"Accept": "application/json", "Referer": CME_REFERER},
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{d} try{attempt}: {exc}")
            else:
                if r.status_code != 200 or not r.content[:1] == b"{":
                    errors.append(f"{d} try{attempt}: HTTP {r.status_code}")
                else:
                    j = r.json()
                    rows = [x for x in (j.get("settlements") or [])
                            if x.get("month", "").lower() != "total"]
                    cand = [
                        (x["month"], _to_settle(x.get("settle", "")),
                         _to_settle(x.get("openInterest", "")) or 0.0)
                        for x in rows
                    ]
                    cand = [(mo, s, oi) for mo, s, oi in cand if s is not None and 0.1 < s < 100]
                    if cand:
                        month, settle, _oi = max(cand, key=lambda t: t[2])  # most open interest
                        tdate = _parse_westmetall_date(j.get("tradeDate", "")) or d
                        log.info("COMEX copper %s [%s, CME settle]: %.4f USD/lb (%.2f USD/t)",
                                 tdate, _contract_code(month), settle, settle * LB_PER_TONNE)
                        return ComexCopperPrice(price_date=tdate, usd_per_lb=settle,
                                                contract=_contract_code(month), source="CME")
                    errors.append(f"{d} try{attempt}: 0 settlements")
            if attempt < tries:
                time.sleep(0.7)
    raise PriceScraperError("CME settlements unavailable: " + "; ".join(errors[:8]))


def _comex_from_yahoo(cutoff: dt.date) -> ComexCopperPrice:
    """Fallback: Yahoo HG=F continuous daily close (last regular-session trade)."""
    last_exc: Exception | None = None
    for host in YAHOO_HOSTS:
        try:
            r = cffi_requests.get(host + YAHOO_CHART, impersonate="chrome", timeout=DEFAULT_TIMEOUT)
            payload = r.json()["chart"]["result"][0]
            series = [
                (dt.datetime.fromtimestamp(ts, dt.timezone.utc).date(), float(c))
                for ts, c in zip(payload["timestamp"], payload["indicators"]["quote"][0]["close"])
                if c is not None
            ]
            completed = [(d, c) for d, c in series if d < cutoff]
            if completed:
                d, c = completed[-1]
                if 0.1 < c < 100:
                    log.info("COMEX copper %s [HG=F, Yahoo close]: %.4f USD/lb", d, c)
                    return ComexCopperPrice(price_date=d, usd_per_lb=c,
                                            contract="HG=F", source="Yahoo")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise PriceScraperError(f"Yahoo HG=F fallback failed: {last_exc}")


def get_comex_copper_price(*, before: dt.date | None = None) -> ComexCopperPrice:
    """Previous trading day's COMEX copper price (market-on-close). Official CME
    settlement of the most-active month; Yahoo HG=F only if CME is unreachable."""
    if cffi_requests is None:  # pragma: no cover
        raise PriceScraperError("curl_cffi is required for the COMEX price feed")
    cutoff = before or dt.datetime.now(dt.timezone.utc).date()
    try:
        return _comex_from_cme(cutoff)
    except PriceScraperError as exc:
        log.warning("COMEX: CME settlements failed (%s); falling back to Yahoo HG=F", exc)
        return _comex_from_yahoo(cutoff)


# --------------------------------------------------------------------------- #
# LME  (Westmetall "LME_Cu_cash" table — LME Official cash + 3-month)
# --------------------------------------------------------------------------- #
def _fetch_westmetall(url: str) -> str:
    """GET a Westmetall market-data page. curl_cffi if present, else plain
    ``requests`` (the site is plain HTML with no bot wall, so either works)."""
    headers = {"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"}
    if cffi_requests is not None:
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=DEFAULT_TIMEOUT, headers=headers)
            if r.status_code == 200 and "<tr" in r.text.lower():
                return r.text
            log.warning("Westmetall via curl_cffi: HTTP %s / no table; trying requests", r.status_code)
        except Exception as exc:  # noqa: BLE001
            log.warning("Westmetall via curl_cffi failed (%s); trying requests", exc)
    if requests is None:  # pragma: no cover
        raise PriceScraperError("no HTTP client available for the Westmetall feed")
    sess = requests.Session()
    sess.headers.update(headers)
    try:
        r = sess.get(url, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200 or "<tr" not in r.text.lower():
            raise PriceScraperError(f"Westmetall HTTP {r.status_code} / no table")
        return r.text
    finally:
        sess.close()


def get_lme_copper_price(
    *, before: dt.date | None = None, on: dt.date | None = None
) -> LmeCopperPrice:
    """LME copper Cash-Settlement + 3-month from the Westmetall ``LME_Cu_cash``
    table. This is the single LME price source (no lme.com path).

    ``on``     — the row **for that trading date**, or the nearest earlier one if
                 that date is a holiday. Use it to date-align the LME leg with the
                 COMEX settlement so the CME-LME spread is a true same-session
                 (market-on-close) figure.
    ``before`` — latest row strictly before this date (default: today); the
                 standalone "previous completed session" behaviour.
    """
    html = _fetch_westmetall(WESTMETALL_LME_CU)
    parsed = _parse_westmetall(html)  # (date, cash, 3m), newest first
    if on is not None:
        on = on.date() if isinstance(on, dt.datetime) else on
        rows = [r for r in parsed if r[0] <= on]
        if not rows:
            raise PriceScraperError(f"Westmetall: no LME copper row on/before {on}")
    else:
        cutoff = before or dt.datetime.now(dt.timezone.utc).date()
        rows = [r for r in parsed if r[0] < cutoff]
        if not rows:
            raise PriceScraperError(f"Westmetall: no LME copper row before {cutoff}")
    d, cash, m3 = rows[0]
    log.info("LME copper %s [Westmetall%s]: cash %.2f, 3m %s",
             d, f", as of {on}" if on else "", cash, m3)
    return LmeCopperPrice(price_date=d, cash_usd_per_tonne=cash,
                          three_month_usd_per_tonne=m3, source="Westmetall")


def _parse_westmetall(html: str) -> list[tuple[dt.date, float, float | None]]:
    out: list[tuple[dt.date, float, float | None]] = []
    for tr in _RE_TR.findall(html):
        cells = [_RE_TAG.sub("", c).strip() for c in _RE_TD.findall(tr)]
        cells = [c for c in cells if c]
        if len(cells) < 2 or cells[0].lower() == "date":
            continue
        d = _parse_westmetall_date(cells[0])
        if d is None:
            continue
        cash = _to_price(cells[1])
        m3 = _to_price(cells[2]) if len(cells) > 2 else None
        if cash is not None:
            out.append((d, cash, m3))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def _parse_westmetall_date(s: str) -> dt.date | None:
    s = s.strip().rstrip(".")
    for fmt in ("%d. %B %Y", "%d %B %Y", "%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
            if _DATE_MIN <= d <= dt.date.today() + dt.timedelta(days=2):
                return d
        except ValueError:
            continue
    return None


def _to_price(s: str) -> float | None:
    s = (s or "").strip().replace(",", "").replace("$", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    return float(s)


# --------------------------------------------------------------------------- #
# Spread
# --------------------------------------------------------------------------- #
def get_cme_lme_copper_spread() -> dict[str, Any]:
    """COMEX minus LME copper, USD per tonne, on a **common trading session**.

    The COMEX settlement posts ~a day later than Westmetall's LME official, so the
    LME leg is fetched *as of the COMEX settlement date* — otherwise the spread
    compares COMEX(T) against LME(T+1) and is off by a full day's move. Both legs
    still soft-fail independently.
    """
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    rec: dict[str, Any] = {
        "retrieved_at": retrieved_at,
        "comex_copper_usd_lb": None, "comex_copper_usd_t": None,
        "comex_price_date": None, "comex_contract": None,
        "lme_copper_cash_usd_t": None, "lme_copper_3m_usd_t": None,
        "lme_cash_3m_spread_usd_t": None, "lme_price_date": None,
        "cme_lme_spread_usd_t": None, "cme_lme_spread_3m_usd_t": None,
        "price_legs_ok": [], "price_legs_failed": [],
    }

    cx: ComexCopperPrice | None = None
    try:
        cx = get_comex_copper_price()
        rec.update(comex_copper_usd_lb=round(cx.usd_per_lb, 4),
                   comex_copper_usd_t=cx.usd_per_tonne, comex_price_date=cx.price_date,
                   comex_contract=cx.contract)
        rec["price_legs_ok"].append("COMEX")
    except PriceScraperError as exc:
        rec["price_legs_failed"].append("COMEX")
        log.warning("COMEX price leg failed: %s", exc)

    try:
        # Align the LME leg to the COMEX session (nearest earlier row on a holiday);
        # fall back to the latest completed session if COMEX is unavailable.
        lme = get_lme_copper_price(on=cx.price_date) if cx else get_lme_copper_price()
        rec.update(
            lme_copper_cash_usd_t=(round(lme.cash_usd_per_tonne, 2)
                                   if lme.cash_usd_per_tonne is not None else None),
            lme_copper_3m_usd_t=(round(lme.three_month_usd_per_tonne, 2)
                                 if lme.three_month_usd_per_tonne is not None else None),
            lme_price_date=lme.price_date,
        )
        rec["price_legs_ok"].append("LME")
    except PriceScraperError as exc:
        rec["price_legs_failed"].append("LME")
        log.warning("LME price leg failed: %s", exc)

    if rec["lme_copper_cash_usd_t"] is not None and rec["lme_copper_3m_usd_t"] is not None:
        # LME term structure: cash - 3-month (positive = backwardation).
        rec["lme_cash_3m_spread_usd_t"] = round(
            rec["lme_copper_cash_usd_t"] - rec["lme_copper_3m_usd_t"], 2
        )

    cx_t = rec["comex_copper_usd_t"]
    if cx_t is not None and rec["lme_copper_3m_usd_t"] is not None:
        rec["cme_lme_spread_3m_usd_t"] = round(cx_t - rec["lme_copper_3m_usd_t"], 2)
    if cx_t is not None and rec["lme_copper_cash_usd_t"] is not None:
        rec["cme_lme_spread_usd_t"] = round(cx_t - rec["lme_copper_cash_usd_t"], 2)
    cpd, lpd = rec["comex_price_date"], rec["lme_price_date"]
    if cpd is not None and lpd is not None and cpd != lpd:
        # After alignment this only happens when the COMEX date was an LME holiday;
        # the spread then uses the nearest earlier LME session.
        log.warning("CME-LME spread legs %d day(s) apart: COMEX %s vs LME %s",
                    abs((cpd - lpd).days), cpd, lpd)

    if not rec["price_legs_ok"]:
        raise PriceScraperError("both price legs failed: " + ", ".join(rec["price_legs_failed"]))
    return rec


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    print(json.dumps(get_cme_lme_copper_spread(), indent=2, default=str))
