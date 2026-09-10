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
import os
import re
import time
import urllib.parse
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None  # type: ignore[assignment]

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore[assignment]

__all__ = [
    "get_cme_lme_copper_spread",
    "get_comex_copper_price",
    "get_comex_copper_history",
    "get_comex_cme_history",
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

# Barchart — the preferred COMEX source when reachable: its historical/get proxy
# returns ~6 months of daily official settlements for one contract in a single
# call (vs the CmeWS endpoint's flaky ~1-week window). It sits behind a JS
# challenge, though, so a plain HTTP client only gets in when the challenge is
# down or when a browser XSRF token is supplied via the BARCHART_XSRF_TOKEN /
# BARCHART_COOKIE env vars.
BARCHART_ROOT = "HG"  # COMEX copper futures root
BARCHART_QUOTES_PAGE = f"https://www.barchart.com/futures/quotes/{BARCHART_ROOT}*0/futures-prices"
BARCHART_HIST_PAGE = "https://www.barchart.com/futures/quotes/{symbol}/historical-prices"
BARCHART_QUOTES_API = "https://www.barchart.com/proxies/core-api/v1/quotes/get"
BARCHART_HISTORY_API = "https://www.barchart.com/proxies/core-api/v1/historical/get"
_BARCHART_CHALLENGE = ("enable javascript", "challenge-platform", "just a moment")

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


# --------------------------------------------------------------------------- #
# COMEX  (Barchart core-api — one call for ~6 months of daily settlements)
# --------------------------------------------------------------------------- #
def _barchart_headers(referer: str, token: str | None) -> dict[str, str]:
    h = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    if token:
        h["X-XSRF-TOKEN"] = token
    return h


def _barchart_token_from_env() -> str | None:
    """A browser XSRF token / cookie header supplied out of band lets the API be
    reached even while the JS challenge is up. ``BARCHART_XSRF_TOKEN`` is the
    decoded token; ``BARCHART_COOKIE`` is a full ``Cookie:`` header we mine it from."""
    tok = os.environ.get("BARCHART_XSRF_TOKEN")
    if tok:
        return urllib.parse.unquote(tok)
    cookie = os.environ.get("BARCHART_COOKIE", "")
    m = re.search(r"XSRF-TOKEN=([^;]+)", cookie)
    return urllib.parse.unquote(m.group(1)) if m else None


def _barchart_session(warmup_url: str):
    """A curl_cffi session warmed on ``warmup_url`` so it carries Barchart's
    cookies + XSRF token. Raises :class:`PriceScraperError` if the JS challenge is
    up and no token was provided via the environment."""
    if cffi_requests is None:  # pragma: no cover
        raise PriceScraperError("curl_cffi is required for the Barchart feed")
    s = cffi_requests.Session(impersonate="chrome")
    env_token = _barchart_token_from_env()
    if env_token:
        s.cookies.set("XSRF-TOKEN", urllib.parse.quote(env_token))
        cookie = os.environ.get("BARCHART_COOKIE")
        if cookie:
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    s.cookies.set(k, v)
        return s, env_token
    try:
        r = s.get(warmup_url, headers={"User-Agent": _UA,
                                       "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                  timeout=DEFAULT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise PriceScraperError(f"Barchart warmup failed: {exc}") from exc
    body = (r.text or "")[:4000].lower()
    raw = r.cookies.get("XSRF-TOKEN")
    if not raw or any(m in body for m in _BARCHART_CHALLENGE):
        raise PriceScraperError(
            f"Barchart JS challenge is up (HTTP {r.status_code}, no XSRF token) — "
            "set BARCHART_XSRF_TOKEN/BARCHART_COOKIE from a browser session to use it"
        )
    return s, urllib.parse.unquote(raw)


def _barchart_get(session, token: str, url: str, params: dict, referer: str) -> list[dict]:
    r = session.get(url, headers=_barchart_headers(referer, token), params=params,
                    timeout=DEFAULT_TIMEOUT)
    if r.status_code != 200 or r.content[:1] not in (b"{", b"["):
        raise PriceScraperError(f"Barchart {url.rsplit('/', 1)[-1]} HTTP {r.status_code}")
    data = (r.json() or {}).get("data") or []
    return [row.get("raw", row) for row in data]


def _barchart_active_symbol(session, token: str) -> str:
    """Most-active COMEX copper contract (highest open interest — the liquid
    Dec/quarterly, matching the CmeWS path), e.g. ``HGZ26``."""
    rows = _barchart_get(
        session, token, BARCHART_QUOTES_API,
        {"list": "futures.contractInRoot", "root": BARCHART_ROOT,
         "fields": "symbol,contractSymbol,contractName,lastPrice,volume,openInterest",
         "orderBy": "openInterest", "orderDir": "desc", "raw": "1"},
        BARCHART_QUOTES_PAGE,
    )
    if not rows:
        raise PriceScraperError("Barchart: empty contract list for HG")

    def _oi(row: dict) -> float:
        try:
            return float(str(row.get("openInterest") or 0).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    best = max(rows, key=_oi)  # most-active = highest open interest (don't trust order)
    sym = str(best.get("symbol") or best.get("contractSymbol") or "").strip()
    if not re.fullmatch(r"HG[FGHJKMNQUVXZ]\d{2}", sym):
        raise PriceScraperError(f"Barchart: unexpected active symbol {sym!r}")
    return sym


def _barchart_history_df(session, token: str, symbol: str, *, limit: int):
    rows = _barchart_get(
        session, token, BARCHART_HISTORY_API,
        {"symbol": symbol, "fields": "tradeTime.format(Y-m-d),openPrice,highPrice,"
         "lowPrice,lastPrice,volume,openInterest", "type": "eod",
         "orderBy": "tradeTime", "orderDir": "desc", "limit": str(limit), "raw": "1"},
        BARCHART_HIST_PAGE.format(symbol=symbol),
    )
    if not rows:
        raise PriceScraperError(f"Barchart: no history rows for {symbol}")
    df = pd.DataFrame(rows).rename(columns={"tradeTime": "date", "lastPrice": "settle",
                                            "openInterest": "open_interest"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "settle", "volume", "open_interest",
              "openPrice", "highPrice", "lowPrice"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "settle"])
    df = df[(df["settle"] > 0.1) & (df["settle"] < 100)].copy()  # sane USD/lb
    df["contract"] = symbol
    df["settle_usd_t"] = (df["settle"] * LB_PER_TONNE).round(2)
    keep = ["date", "contract", "settle", "settle_usd_t", "volume", "open_interest"]
    return df[[c for c in keep if c in df.columns]].sort_values("date").reset_index(drop=True)


def get_comex_copper_history(*, days: int = 180) -> "pd.DataFrame":
    """~``days`` of the most-active COMEX copper contract's daily official
    settlements from Barchart: ``date, contract, settle, settle_usd_t, volume,
    open_interest`` (oldest first). Raises :class:`PriceScraperError` if Barchart
    is unreachable (JS challenge, no token)."""
    if pd is None:  # pragma: no cover
        raise PriceScraperError("pandas is required for get_comex_copper_history")
    session, token = _barchart_session(BARCHART_QUOTES_PAGE)
    symbol = _barchart_active_symbol(session, token)
    df = _barchart_history_df(session, token, symbol, limit=max(days, 20))
    log.info("COMEX copper history [Barchart, %s]: %d rows %s..%s",
             symbol, len(df), df["date"].min().date(), df["date"].max().date())
    return df


def get_comex_cme_history(*, days: int = 12) -> "pd.DataFrame":
    """Whatever daily COMEX settlements the CmeWS endpoint still exposes (only a
    rolling ~1 week), same columns as :func:`get_comex_copper_history`. A thin
    backfill fallback for when Barchart is unreachable."""
    if pd is None or cffi_requests is None:  # pragma: no cover
        raise PriceScraperError("pandas + curl_cffi required for get_comex_cme_history")
    today = dt.datetime.now(dt.timezone.utc).date()
    recs: list[dict] = []
    for k in range(1, days + 1):
        d = today - dt.timedelta(days=k)
        if d.weekday() >= 5:
            continue
        try:
            r = cffi_requests.get(
                CME_COPPER_SETTLE_URL.format(date=d.strftime("%m/%d/%Y")),
                impersonate="chrome", timeout=DEFAULT_TIMEOUT,
                headers={"Accept": "application/json", "Referer": CME_REFERER},
            )
            if r.status_code != 200 or r.content[:1] != b"{":
                continue
            rows = [x for x in (r.json().get("settlements") or [])
                    if x.get("month", "").lower() != "total"]
            cand = [(x["month"], _to_settle(x.get("settle", "")),
                     _to_settle(x.get("openInterest", "")) or 0.0) for x in rows]
            cand = [c for c in cand if c[1] is not None and 0.1 < c[1] < 100]
            if not cand:
                continue
            month, settle, _oi = max(cand, key=lambda t: t[2])
            recs.append({"date": pd.Timestamp(d), "contract": _contract_code(month),
                         "settle": settle, "settle_usd_t": round(settle * LB_PER_TONNE, 2)})
        except Exception:  # noqa: BLE001
            continue
    if not recs:
        raise PriceScraperError("CmeWS exposed no recent settlements")
    return pd.DataFrame(recs).sort_values("date").reset_index(drop=True)


def _comex_from_barchart(cutoff: dt.date) -> ComexCopperPrice:
    df = get_comex_copper_history(days=30)
    before = df[df["date"].dt.date < cutoff]
    if before.empty:
        raise PriceScraperError(f"Barchart: no settlement before {cutoff}")
    row = before.iloc[-1]
    d, settle = row["date"].date(), float(row["settle"])
    log.info("COMEX copper %s [%s, Barchart settle]: %.4f USD/lb (%.2f USD/t)",
             d, row["contract"], settle, settle * LB_PER_TONNE)
    return ComexCopperPrice(price_date=d, usd_per_lb=settle,
                            contract=str(row["contract"]), source="Barchart")


def get_comex_copper_price(*, before: dt.date | None = None) -> ComexCopperPrice:
    """Previous completed session's COMEX copper settlement (market-on-close) for
    the most-active month. Source order: **Barchart** (cleanest, ~6mo history in
    one call) → CmeWS settlements API → Yahoo ``HG=F`` continuous close."""
    if cffi_requests is None:  # pragma: no cover
        raise PriceScraperError("curl_cffi is required for the COMEX price feed")
    cutoff = before or dt.datetime.now(dt.timezone.utc).date()
    errs: list[str] = []
    for name, fn in (("Barchart", _comex_from_barchart),
                     ("CmeWS", _comex_from_cme),
                     ("Yahoo", _comex_from_yahoo)):
        try:
            return fn(cutoff)
        except PriceScraperError as exc:
            errs.append(f"{name}: {exc}")
            log.warning("COMEX: %s unavailable (%s)", name, exc)
    raise PriceScraperError("all COMEX sources failed — " + " | ".join(errs))


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
