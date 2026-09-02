"""
CME (COMEX) vs LME copper price spread.

Pulls the previous completed session's settlement/close for both legs and returns
the spread in USD per metric tonne:

    spread = COMEX HG (converted lb -> tonne)  -  LME cash settlement

Sources (both free, no key):
  * COMEX copper : Yahoo Finance chart API for ``HG=F`` (front-month continuous),
                   quoted in USD/lb. Fetched with curl_cffi (Yahoo 403s plain
                   requests from some IPs).
  * LME copper   : Westmetall market-data table — daily LME Copper Cash-Settlement
                   and 3-month, in USD/tonne. Plain requests; no anti-bot.

Both legs soft-fail independently; :func:`get_cme_lme_copper_spread` returns
whatever it could get and only fills the spread when both legs land on the same
calendar date.
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

    @property
    def usd_per_tonne(self) -> float:
        return round(self.usd_per_lb * LB_PER_TONNE, 2)


@dataclasses.dataclass(slots=True)
class LmeCopperPrice:
    price_date: dt.date
    cash_usd_per_tonne: float
    three_month_usd_per_tonne: float | None


# --------------------------------------------------------------------------- #
# COMEX  (Yahoo Finance, HG=F, USD/lb)
# --------------------------------------------------------------------------- #
def get_comex_copper_price(*, before: dt.date | None = None) -> ComexCopperPrice:
    """Most recent completed daily close for COMEX copper strictly before ``before``
    (default: today UTC)."""
    if cffi_requests is None:  # pragma: no cover
        raise PriceScraperError("curl_cffi is required for the Yahoo price feed")
    cutoff = before or dt.datetime.now(dt.timezone.utc).date()

    last_exc: Exception | None = None
    for host in YAHOO_HOSTS:
        try:
            r = cffi_requests.get(host + YAHOO_CHART, impersonate="chrome", timeout=DEFAULT_TIMEOUT)
            if r.status_code != 200:
                last_exc = PriceScraperError(f"Yahoo HTTP {r.status_code} from {host}")
                continue
            payload = r.json()["chart"]["result"][0]
            stamps = payload["timestamp"]
            closes = payload["indicators"]["quote"][0]["close"]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("COMEX price: %s failed: %s", host, exc)
            continue

        series = [
            (dt.datetime.fromtimestamp(ts, dt.timezone.utc).date(), c)
            for ts, c in zip(stamps, closes)
            if c is not None
        ]
        completed = [(d, c) for d, c in series if d < cutoff]
        if not completed:
            last_exc = PriceScraperError(f"no COMEX close before {cutoff} in Yahoo series")
            continue
        d, c = completed[-1]
        px = float(c)
        if not (0.1 < px < 100):  # sanity: USD/lb
            raise PriceScraperError(f"implausible COMEX price {px} USD/lb on {d}")
        log.info("COMEX copper %s: %.4f USD/lb (%.2f USD/t)", d, px, px * LB_PER_TONNE)
        return ComexCopperPrice(price_date=d, usd_per_lb=px)

    raise PriceScraperError(f"COMEX price fetch failed: {last_exc}")


# --------------------------------------------------------------------------- #
# LME  (Westmetall table, USD/tonne)
# --------------------------------------------------------------------------- #
def get_lme_copper_price(*, before: dt.date | None = None, session=None) -> LmeCopperPrice:
    """LME Copper Cash-Settlement (+ 3-month) for the last completed trading day
    strictly before ``before`` (default today UTC) — i.e. the market-on-close."""
    if requests is None:  # pragma: no cover
        raise PriceScraperError("the 'requests' package is required for the LME price feed")
    cutoff = before or dt.datetime.now(dt.timezone.utc).date()
    own = session is None
    sess = session or requests.Session()
    if own:
        sess.headers.update({"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"})
    try:
        r = sess.get(WESTMETALL_LME_CU, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200 or "<tr" not in r.text.lower():
            raise PriceScraperError(f"Westmetall HTTP {r.status_code} / no table")
        rows = [row for row in _parse_westmetall(r.text) if row[0] < cutoff]  # newest first
        if not rows:
            raise PriceScraperError(f"Westmetall: no LME row before {cutoff}")
        d, cash, m3 = rows[0]
        if not (1000 < cash < 60000):  # sanity: USD/tonne
            raise PriceScraperError(f"implausible LME cash price {cash} on {d}")
        log.info("LME copper %s: cash %.2f USD/t, 3m %s", d, cash, m3)
        return LmeCopperPrice(price_date=d, cash_usd_per_tonne=cash, three_month_usd_per_tonne=m3)
    finally:
        if own:
            sess.close()


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
    for fmt in ("%d. %B %Y", "%d %B %Y", "%d.%m.%Y", "%Y-%m-%d"):
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
        "comex_copper_usd_lb": None, "comex_copper_usd_t": None, "comex_price_date": None,
        "lme_copper_cash_usd_t": None, "lme_copper_3m_usd_t": None, "lme_price_date": None,
        "cme_lme_spread_usd_t": None, "cme_lme_spread_3m_usd_t": None,
        "price_legs_ok": [], "price_legs_failed": [],
    }

    try:
        cx = get_comex_copper_price()
        rec.update(comex_copper_usd_lb=round(cx.usd_per_lb, 4),
                   comex_copper_usd_t=cx.usd_per_tonne, comex_price_date=cx.price_date)
        rec["price_legs_ok"].append("COMEX")
    except PriceScraperError as exc:
        rec["price_legs_failed"].append("COMEX")
        log.warning("COMEX price leg failed: %s", exc)

    try:
        lme = get_lme_copper_price()
        rec.update(lme_copper_cash_usd_t=round(lme.cash_usd_per_tonne, 2),
                   lme_copper_3m_usd_t=(round(lme.three_month_usd_per_tonne, 2)
                                        if lme.three_month_usd_per_tonne is not None else None),
                   lme_price_date=lme.price_date)
        rec["price_legs_ok"].append("LME")
    except PriceScraperError as exc:
        rec["price_legs_failed"].append("LME")
        log.warning("LME price leg failed: %s", exc)

    cx_t = rec["comex_copper_usd_t"]
    if cx_t is not None and rec["lme_copper_cash_usd_t"] is not None:
        rec["cme_lme_spread_usd_t"] = round(cx_t - rec["lme_copper_cash_usd_t"], 2)
        if rec["lme_copper_3m_usd_t"] is not None:
            rec["cme_lme_spread_3m_usd_t"] = round(cx_t - rec["lme_copper_3m_usd_t"], 2)
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
