# base-metals-inventory
Compiles and aggregates global inventory stock status for base metals 
Currently only supports Copper since it is the most easily tradable base metal

Data Architecture and Workflow
1. Data scraping from websites
    - CME (Registered - warranted / Eligible - offwarrant stock)
    - LME (On Warrant / Off Warrant / Cancelled Warrants)
    - SHFE (CHECK THIS)
# Check that scraping went successfully

2. Stores scraped data in Parquet file 
    - For historical analysis and timeseries in the future 
    - Parquet as a more efficient use of memory

3. Use data found in parquet to generate dashboard and timeseries
    - Deployed on Streamlit and accessible wherever
    - Either a screenshot emailed to stakeholders or just a static dashboard within streamlit

---

## Layout

| Path | Purpose |
|------|---------|
| `scripts/cme_scraper.py`  | CME (COMEX) `Copper_Stocks.xls` — Registered / Eligible (short tons); Chrome-impersonation transport (`curl_cffi`) with a plain-`requests` fallback to get past the `/delivery_reports/` datacentre-IP block |
| `scripts/lme_scraper.py`  | LME stock-breakdown (live + cancelled warrants, plus the per-location / per-country breakdown) + OWSR (off-warrant, plus the ASIA/EUROPE/NORTH AMERICAS region totals), via the reports JSON API behind Cloudflare (`curl_cffi`) |
| `scripts/shfe_scraper.py` | SHFE weekly stock report (库存 + 仓单) + daily warrant report (side feed) |
| `scripts/price_scraper.py`| COMEX copper (CME settlements API, most-active month, USD/lb; Yahoo `HG=F` fallback) vs LME cash + 3-month (Westmetall `LME_Cu_cash` table, USD/t — sole LME source) → CME−LME spread + LME cash−3M term-structure spread, all USD/t |
| `scripts/aggregate.py`    | runs all scrapers, converts CME short tons ×0.907185, harmonises, computes the global total, upserts `data/copper_inventory.parquet` and (best-effort) `data/lme_geo.parquet` |
| `scripts/schema.py`       | shared column schema + inventory taxonomy + LOCF daily-calendar helper; `staleness()`, the exchange-native as-of series, and the tidy geo-parquet upsert |
| `scripts/backfill.py`     | one-off: recover ~2 weeks of history each source still exposes |
| `scripts/analytics.py`    | scarcity-vs-reshuffling analytics: warrant-lifecycle / "phantom tightness" flows, rolling 30/90-day Z-score anomaly scan, configurable CME–LME arbitrage-hurdle model, hub concentration / load-out response / `diagnose_anomalies`, and a combined `scarcity_scorecard` |
| `app.py`                  | Streamlit dashboard — overview page |
| `pages/1_Scarcity_Analysis.py` | Streamlit dashboard — "Physical vs Paper Scarcity" page (KPI row, spatial concentration, warrant-vs-load-out, term-structure/arb band with an adjustable cost hurdle, anomaly table) |
| `views/`                  | render helpers for the pages (`common.py` loaders, `scarcity.py` charts) |
| `.github/workflows/daily.yml` | daily cron: run aggregator, commit the parquet back |
| `tests/`                  | offline parser tests + fixtures (no network) |

## Run locally

```bash
python -m venv venv && venv/Scripts/activate      # or: source venv/bin/activate
pip install -r requirements.txt

python scripts/aggregate.py --dry-run -v          # run every scraper, print, don't write
python scripts/aggregate.py                        # write/upsert today's row into the parquet
python -m pytest tests/ -q                         # offline tests

streamlit run app.py                               # dashboard
```

Individual scrapers print JSON when run directly, e.g. `python scripts/lme_scraper.py`
(add `--offwarrant` for OWSR) or `python scripts/shfe_scraper.py`.

## Automation

`.github/workflows/daily.yml` runs `scripts/aggregate.py` at **10:17 UTC** daily
(and on manual dispatch), then commits `data/copper_inventory.parquet` and
`data/lme_geo.parquet`. Needs no secrets — `permissions: contents: write` + the
default `GITHUB_TOKEN`. A source blocked by an anti-bot edge (LME Cloudflare /
SHFE WAF) from the runner IP does **not** fail the job: the aggregator writes a
partial row and forward-fills the other exchanges. GitHub emails you if a whole
run fails.

### CME `/delivery_reports/` block

CME's `Copper_Stocks.xls` path (unlike the settlements API the price leg uses) is
blocked for datacentre IPs, so it fails from the Actions runner even though it
works from a residential connection. Mitigations, in the order the code tries /
the project would escalate:

1. **`curl_cffi` Chrome impersonation** (implemented) — `cme_scraper` now makes
   the `.xls` request with a real Chrome TLS/JA3 fingerprint before falling back
   to plain `requests`. This is the same trick that beats LME's Cloudflare check.
2. **A CmeWS depository-stocks JSON endpoint** — the settlements host works from
   CI; a sibling stocks endpoint would be as reliable as the price leg.
3. **A free Cloudflare Worker proxy** that `fetch()`es the `.xls` — Cloudflare
   egress IPs aren't caught by the rule. Set `CME_STOCKS_URL` to the Worker.
4. **A short second workflow on a self-hosted / residential runner** for the CME
   leg only, committing `data/cme_latest.json` for the main run to read.
5. **Accept + surface staleness** — CME copper stocks move slowly, so the
   dashboard badges the feed as *N business days stale* and carries it forward;
   `scripts/backfill.py` from a residential IP refreshes the history.

## Deploy the dashboard

Streamlit Community Cloud → new app → this repo, branch `main`, main file
`app.py`, Python **3.11**. No secrets. It redeploys automatically each time the
Action commits a fresh parquet.

## Data model (`data/copper_inventory.parquet`)

One row per pipeline run (`run_date`). Per exchange: `*_on_warrant_t`,
`*_cancelled_t`, `*_off_warrant_t`, and `*_total_t` = that exchange's **headline
reported stock** (the figure public trackers quote — CME's TOTAL COPPER, LME's
on-warrant closing = live + cancelled, SHFE's 库存). All metric tonnes. Plus each
exchange's own `*_data_date` (report as-of date) and a `*_stale` flag when the
value was carried forward.

Global columns:

| column | meaning |
|--------|---------|
| `global_on_warrant_t` / `global_cancelled_t` / `global_off_warrant_t` | sum of the per-exchange buckets (`global_off_warrant_t` excludes SHFE — it doesn't report off-warrant) |
| `global_reported_stock_t` | `cme_total + lme_total + shfe_total` — add up each exchange's headline figure |
| `global_total_t` | grand total per spec = `global_reported_stock_t` + LME off-warrant |

Sanity check against public sources: `lme_total_t` should equal the "LME copper
stock" on Westmetall; `cme_total_t` ÷ 0.907185 should equal COMEX "TOTAL COPPER"
in short tons; `shfe_total_t` should equal the SMM weekly SHFE copper stock. Note
LME/press often date a given `lme_total_t` one business day later than the source
file (e.g. the "28 Aug" file's 233,500 t shows as "01 Sep" on Westmetall).

### Harmonisation notes

- **CME**: Registered = on-warrant, Eligible = off-warrant, cancelled = 0 (COMEX
  has no cancelled-warrant concept). Reported in short tons → ×0.907185.
- **LME**: Open Tonnage = on-warrant (live), Cancelled Tonnage = cancelled,
  `lme_total_t` = Closing Stock = live + cancelled (the headline "LME copper
  stock"). Off-warrant comes from the separate T+3 `Daily_OWSR` report and is
  **not** part of `lme_total_t`.
- **SHFE**: 仓单 = on-warrant, `库存 − 仓单` = implied non-warranted (shown in the
  *Cancelled* column — **not** a true cancelled-warrant figure), off-warrant not
  reported. The **weekly** report is the source of truth; the daily warrant
  report rides along as `shfe_warrant_daily_t` only.
- `scripts/backfill.py` recovers ~2 weeks of history where the sources still
  expose it (CME's PREV column, LME's last-7-days listing, recent SHFE Fridays)
  so the as-of chart doesn't step up when a later-lagging source first appears.
- Missing days (holidays, blocked scrapes) are forward-filled (LOCF) for
  charting. The dashboard's **View** selector picks the series builder:
  - **Same-Day Synced (LOCF)** — `scripts/schema.build_asof_series`: every
    exchange carried forward to a common daily calendar. Carried-forward tails
    render dashed and a grey band + banner flag how many business days stale each
    leg is (`staleness()` vs `STALE_AFTER_BDAYS`). Dashboard default.
  - **Exchange-Native (as-of)** — `build_native_asof_series`: no fill past each
    exchange's last real report; the per-exchange lines simply end, and the
    cross-exchange `global_*` sum requires **all** legs (`min_count`) so the
    global line stops at the earliest stale date instead of showing a false
    step. `dod()` (day-on-day net change) is computed from this series so a stale
    leg can never inject a spike.
  - **Pipeline run date** — `build_daily_series`: x-axis is the pipeline run
    date, no as-of staggering.

### Price spread

`comex_copper_usd_t` = Yahoo `HG=F` previous completed close (USD/lb) × 2204.62.
`lme_copper_cash_usd_t` / `lme_copper_3m_usd_t` from the Westmetall table.
`cme_lme_spread_usd_t` = COMEX − LME cash (positive = COMEX rich to LME);
`cme_lme_spread_3m_usd_t` = COMEX − LME 3-month;
`lme_cash_3m_spread_usd_t` = LME cash − LME 3-month, the LME term structure
(positive = backwardation). `comex_price_date` / `lme_price_date` record which
session each leg is from; the spread is only filled when both legs are present.
Either leg can fail independently without failing the run.

### Geo breakdown (`data/lme_geo.parquet`)

A separate tidy/long parquet, one row per (report, region, location), built
best-effort from the **same** LME downloads (no extra fetch). `report_type` is
`breakdown` (per-country / per-location on-warrant, cancelled, opening,
delivered-in, delivered-out, closing tonnes from the Metals Totals report) or
`owsr` (per-region off-warrant tonnes — ASIA / EUROPE / NORTH AMERICAS / GLOBAL).
Schema: `run_date, report_date, report_type, region, location, on_warrant_t,
cancelled_t, opening_t, delivered_in_t, delivered_out_t, closing_t,
off_warrant_t, retrieved_at`; dedupe key is
`report_date + report_type + region + location`. The dashboard's **LME by
location** section reads it; if the file is missing the section shows a hint and
the rest of the app is unaffected.

## Scarcity vs. reshuffling analytics (`scripts/analytics.py`)

Is a stock drawdown real metal leaving, or warrants being churned / financed?
`python scripts/analytics.py` prints the read; the functions also import cleanly
(pandas + numpy only). It reads both parquets and has three layers:

1. **Warrant lifecycle & "phantom tightness"** — `location_warrant_flows(geo,
   level=…)` decomposes each LME location's period-over-period move into fresh
   cancellations vs. actual load-out (`delivered_out_t`), an
   `implied_rewarrant_t` (cancelled tonnage that went back on warrant instead of
   physically leaving), a `cancellation_drawdown_ratio`, and boolean
   `is_rewarranting` / `phantom_tightness` flags. `cancelled_share(on, canc)` =
   `canc / (on + canc) * 100`. Re-warranting and phantom flags are per-location
   by design — a global roll-up hides one shed's churn behind another's load-out.
2. **Anomaly detection** — `rolling_zscore(series, 30|90)` (calendar-day window
   when the index is datetime, `ddof=0`, zero-variance → NaN not ∞);
   `zscore_frame` adds `z30d/z90d` + `alert_*` at `abs(z) > 2.0`; `anomaly_scan`
   bundles daily net cancellations and load-outs by location plus the LME
   cash-to-3M spread.
3. **Arbitrage hurdle** — `ArbCostBand(freight_usd_mt, finance_insurance_usd_mt,
   tariff_pct, tariff_basis)` defines the transfer cost; `net_arb_margin =
   (cme_price_mt − lme_3m_price_mt) − transfer_cost`; `classify_arb_regime`
   returns **"Open Physical Arb"** (net > 0), **"Paper Dislocation"** (gross
   spread > 0 but inside the cost band) or **"No Dislocation"**. CLI flags:
   `--freight`, `--finance`, `--tariff-pct`.

`scarcity_scorecard(runs, geo, band=…)` folds these into a signed score in
`[-1, 1]` (−1 = warehouse reshuffling / financing, +1 = physical scarcity) with a
plain-language `verdict` and `rationale`. Sparse history ⇒ an honest
"Mixed / inconclusive".

Dashboard helpers on top of the three layers: `hub_of` / `hub_warrant_status` /
`cancellation_concentration` bucket LME delivery points into hubs (Singapore,
Rotterdam, Busan, Port Klang, US, Other) and measure where the cancelled tonnage
sits; `loadout_response` flags cancellation spikes that produced < 50% load-out
within 10 trading days; `net_draw_rate` is the business-day inventory draw rate;
`diagnose_anomalies` returns the per-location alert table with an automated
interpretation tag (`_interpret_anomaly`: *Isolated Cancellation – Low Physical
Drain*, *Broad Regional Tightening*, *Transpacific Arb Delivery Candidate*,
*Active Physical Load-Out*, *Re-warranting / Paper Hold*, *Watch*).

## Scarcity Analysis page

`streamlit run app.py` now has two pages; **Physical vs Paper Scarcity**
(`pages/1_Scarcity_Analysis.py`) is the institutional view:

- **KPI row** — Total Reported / On-Warrant / Cancelled / Off-Warrant, plus
  *market temperature*: LME Cash–3M (labelled Backwardation / Contango) and
  CME–LME 3M (labelled Arb Open / Closed against the cost hurdle).
- **LME spatial concentration** — On-warrant vs Cancelled stacked bars per hub,
  with the top-location / top-hub share of global cancellations.
- **Warrant dynamics vs physical load-out** — dual-axis Δ-cancelled-warrants vs
  gross Delivered-Out, with ▲ markers on cancellation spikes that never loaded out.
- **Term structure & arb band** — LME Cash–3M vs the inventory draw rate, and the
  CME–LME spread with a shaded transfer-cost band that moves with the sidebar
  **freight / finance+insurance / import-tariff** sliders.
- **Anomaly & diagnostics** — the `diagnose_anomalies` table.

Time-series and Z-score sections show a "builds as the pipeline runs" placeholder
until `data/lme_geo.parquet` has accumulated enough per-location history.