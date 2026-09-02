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
  * LME copper   : lme.com day-delayed trading-data API — the **Closing 3-month
                   price** (what the LME website shows, e.g. 14,274.50) plus the
                   Official cash price. curl_cffi (Cloudflare). Falls back to the
                   Westmetall table (LME Official prices) if lme.com is unreachable.

Both legs soft-fail independently; :func:`get_cme_lme_copper_spread` returns
whatever it could get and only fills the spread when both legs are present
(a warning is logged if their price dates differ).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
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

# CME Group settlements API — product 438 = Copper.
CME_COPPER_SETTLE_URL = (
    "https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements/438/FUT"
    "?tradeDate={date}&strategy=DEFAULT&pageSize=90"
)
CME_REFERER = "https://www.cmegroup.com/markets/metals/base/copper.settlements.html"
_MONTH_CODE = "FGHJKMNQUVXZ"  # Jan..Dec

# LME day-delayed trading-data API (drives the tables on lme.com/.../lme-copper).
LME_COPPER_PAGE = "https://www.lme.com/en/metals/non-ferrous/lme-copper"
LME_DAYDELAYED_URL = "https://www.lme.com/api/trading-data/day-delayed?datasourceId={ds}"
LME_CU_CLOSING_DS = "2a431297-6620-4ba7-a991-8335423f994b"   # "LME Copper Closing Prices"
LME_CU_OFFICIAL_DS = "762a3883-b0e1-4c18-b34b-fe97a1f2d3a5"  # "LME Copper Official Prices"

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
    source: str = "LME closing"


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
    strictly before ``cutoff``."""
    errors: list[str] = []
    for k in range(1, 9):
        d = cutoff - dt.timedelta(days=k)
        url = CME_COPPER_SETTLE_URL.format(date=d.strftime("%m/%d/%Y"))
        try:
            r = cffi_requests.get(
                url, impersonate="chrome", timeout=DEFAULT_TIMEOUT,
                headers={"Accept": "application/json", "Referer": CME_REFERER},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{d}: {exc}")
            continue
        if r.status_code != 200 or not r.content[:1] == b"{":
            errors.append(f"{d}: HTTP {r.status_code}")
            continue
        j = r.json()
        rows = [x for x in (j.get("settlements") or []) if x.get("month", "").lower() != "total"]
        cand = [
            (x["month"], _to_settle(x.get("settle", "")), _to_settle(x.get("openInterest", "")) or 0.0)
            for x in rows
        ]
        cand = [(mo, s, oi) for mo, s, oi in cand if s is not None and 0.1 < s < 100]
        if not cand:
            errors.append(f"{d}: no numeric settlements")
            continue
        month, settle, _oi = max(cand, key=lambda t: t[2])  # most open interest
        tdate = _parse_westmetall_date(j.get("tradeDate", "")) or d
        log.info("COMEX copper %s [%s, CME settle]: %.4f USD/lb (%.2f USD/t)",
                 tdate, _contract_code(month), settle, settle * LB_PER_TONNE)
        return ComexCopperPrice(price_date=tdate, usd_per_lb=settle,
                                contract=_contract_code(month), source="CME")
    raise PriceScraperError("CME settlements unavailable: " + "; ".join(errors[:4]))


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
# LME  (lme.com day-delayed API — closing + official prices)
# --------------------------------------------------------------------------- #
def _lme_daydelayed(datasource_id: str) -> dict[str, Any]:
    if cffi_requests is None:  # pragma: no cover
        raise PriceScraperError("curl_cffi is required for the LME price feed")
    r = cffi_requests.get(
        LME_DAYDELAYED_URL.format(ds=datasource_id), impersonate="chrome",
        timeout=DEFAULT_TIMEOUT,
        headers={"Accept": "application/json", "Referer": LME_COPPER_PAGE},
    )
    if r.status_code != 200 or r.content[:1] != b"{":
        raise PriceScraperError(f"LME day-delayed HTTP {r.status_code} for {datasource_id}")
    return r.json()


def _lme_row_value(payload: dict[str, Any], row_title: str) -> float | None:
    for row in payload.get("Rows", []):
        if str(row.get("RowTitle", "")).strip().lower() == row_title.lower():
            vals = row.get("Values") or []
            for v in reversed(vals):  # [bid, offer] -> take offer; [price] -> take it
                f = _to_price(str(v))
                if f is not None and 1000 < f < 60000:
                    return f
    return None


def _lme_from_website(cutoff: dt.date) -> LmeCopperPrice:
    """LME Copper Closing 3-month (+ Official cash) from lme.com's day-delayed API."""
    closing = _lme_daydelayed(LME_CU_CLOSING_DS)
    official = None
    try:
        official = _lme_daydelayed(LME_CU_OFFICIAL_DS)
    except PriceScraperError as exc:
        log.warning("LME official prices unavailable: %s", exc)

    m3 = _lme_row_value(closing, "3-month")
    if m3 is None and official is not None:
        m3 = _lme_row_value(official, "3-month")
    cash = _lme_row_value(official, "Cash") if official else None
    if m3 is None and cash is None:
        raise PriceScraperError("LME day-delayed: no 3-month or cash price parsed")

    d = _parse_iso_date(closing.get("DateOfData")) or _parse_iso_date(
        (official or {}).get("DateOfData")
    ) or (cutoff - dt.timedelta(days=1))
    log.info("LME copper %s [lme.com day-delayed]: 3m-close %s, cash %s", d, m3, cash)
    return LmeCopperPrice(price_date=d, cash_usd_per_tonne=cash,
                          three_month_usd_per_tonne=m3, source="LME closing")


def _lme_from_westmetall(cutoff: dt.date) -> LmeCopperPrice:
    """Fallback: Westmetall table (LME Official cash + 3-month)."""
    if requests is None:  # pragma: no cover
        raise PriceScraperError("the 'requests' package is required for the Westmetall fallback")
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"})
    try:
        r = sess.get(WESTMETALL_LME_CU, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200 or "<tr" not in r.text.lower():
            raise PriceScraperError(f"Westmetall HTTP {r.status_code} / no table")
        rows = [row for row in _parse_westmetall(r.text) if row[0] < cutoff]
        if not rows:
            raise PriceScraperError(f"Westmetall: no LME row before {cutoff}")
        d, cash, m3 = rows[0]
        log.info("LME copper %s [Westmetall]: cash %.2f, 3m %s", d, cash, m3)
        return LmeCopperPrice(price_date=d, cash_usd_per_tonne=cash,
                              three_month_usd_per_tonne=m3, source="Westmetall")
    finally:
        sess.close()


def get_lme_copper_price(*, before: dt.date | None = None) -> LmeCopperPrice:
    """Previous trading day's LME copper price. Primary: lme.com day-delayed API —
    Closing 3-month price (matches the LME website) + Official cash. Falls back to
    the Westmetall table if lme.com is unreachable."""
    cutoff = before or dt.datetime.now(dt.timezone.utc).date()
    try:
        return _lme_from_website(cutoff)
    except PriceScraperError as exc:
        log.warning("LME: lme.com day-delayed failed (%s); falling back to Westmetall", exc)
        return _lme_from_westmetall(cutoff)


def _parse_iso_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


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
    """COMEX minus LME copper, USD per tonne. Both legs soft-fail independently."""
    retrieved_at = dt.datetime.now(dt.timezone.utc)
    rec: dict[str, Any] = {
        "retrieved_at": retrieved_at,
        "comex_copper_usd_lb": None, "comex_copper_usd_t": None,
        "comex_price_date": None, "comex_contract": None,
        "lme_copper_cash_usd_t": None, "lme_copper_3m_usd_t": None, "lme_price_date": None,
        "cme_lme_spread_usd_t": None, "cme_lme_spread_3m_usd_t": None,
        "price_legs_ok": [], "price_legs_failed": [],
    }

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
        lme = get_lme_copper_price()
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

    cx_t = rec["comex_copper_usd_t"]
    if cx_t is not None and rec["lme_copper_3m_usd_t"] is not None:
        rec["cme_lme_spread_3m_usd_t"] = round(cx_t - rec["lme_copper_3m_usd_t"], 2)
    if cx_t is not None and rec["lme_copper_cash_usd_t"] is not None:
        rec["cme_lme_spread_usd_t"] = round(cx_t - rec["lme_copper_cash_usd_t"], 2)
    if rec["cme_lme_spread_3m_usd_t"] is not None or rec["cme_lme_spread_usd_t"] is not None:
        if rec["comex_price_date"] != rec["lme_price_date"]:
            log.warning("price dates differ: COMEX %s vs LME %s",
                        rec["comex_price_date"], rec["lme_price_date"])

    if not rec["price_legs_ok"]:
        raise PriceScraperError("both price legs failed: " + ", ".join(rec["price_legs_failed"]))
    return rec


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    print(json.dumps(get_cme_lme_copper_spread(), indent=2, default=str))
