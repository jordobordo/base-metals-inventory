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
| `scripts/cme_scraper.py`  | CME (COMEX) `Copper_Stocks.xls` — Registered / Eligible (short tons) |
| `scripts/lme_scraper.py`  | LME stock-breakdown (live + cancelled warrants) + OWSR (off-warrant), via the reports JSON API behind Cloudflare (`curl_cffi`) |
| `scripts/shfe_scraper.py` | SHFE weekly stock report (库存 + 仓单) + daily warrant report (side feed) |
| `scripts/price_scraper.py`| COMEX copper (Yahoo `HG=F`, USD/lb) vs LME cash + 3-month (Westmetall, USD/t) → CME−LME spread in USD/t |
| `scripts/aggregate.py`    | runs all scrapers, converts CME short tons ×0.907185, harmonises, computes the global total, upserts `data/copper_inventory.parquet` |
| `scripts/schema.py`       | shared column schema + inventory taxonomy + LOCF daily-calendar helper |
| `scripts/backfill.py`     | one-off: recover ~2 weeks of history each source still exposes |
| `app.py`                  | Streamlit dashboard |
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
(and on manual dispatch), then commits `data/copper_inventory.parquet`. Needs no
secrets — `permissions: contents: write` + the default `GITHUB_TOKEN`. A source
blocked by an anti-bot edge (LME Cloudflare / SHFE WAF) from the runner IP does
**not** fail the job: the aggregator writes a partial row and forward-fills the
other exchanges. GitHub emails you if a whole run fails.

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
  charting: `scripts/schema.build_daily_series` (x-axis = pipeline run date) and
  `build_asof_series` (x-axis = each report's own as-of date, staggered by lag —
  the dashboard default).

### Price spread

`comex_copper_usd_t` = Yahoo `HG=F` previous completed close (USD/lb) × 2204.62.
`lme_copper_cash_usd_t` / `lme_copper_3m_usd_t` from the Westmetall table.
`cme_lme_spread_usd_t` = COMEX − LME cash (positive = COMEX rich to LME);
`cme_lme_spread_3m_usd_t` = COMEX − LME 3-month. `comex_price_date` /
`lme_price_date` record which session each leg is from; the spread is only filled
when both legs are present. Either leg can fail independently without failing the
run.