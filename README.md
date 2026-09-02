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
| `scripts/aggregate.py`    | runs all scrapers, converts CME short tons ×0.907185, harmonises, computes the global total, upserts `data/copper_inventory.parquet` |
| `scripts/schema.py`       | shared column schema + LOCF daily-calendar helper |
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
`*_cancelled_t`, `*_off_warrant_t`, `*_total_t` (all metric tonnes), plus that
exchange's own `*_data_date` (report as-of date) and a `*_stale` flag when the
value was carried forward. `global_*` columns sum the three exchanges
(`global_off_warrant_t` excludes SHFE, which does not report it).

### Harmonisation notes

- **CME**: Registered = on-warrant, Eligible = off-warrant, cancelled = 0 (COMEX
  has no cancelled-warrant concept). Reported in short tons → ×0.907185.
- **LME**: Open Tonnage = on-warrant (live), Cancelled Tonnage = cancelled,
  off-warrant from the separate T+3 `Daily_OWSR` report.
- **SHFE**: 仓单 = on-warrant, `库存 − 仓单` = implied non-warranted (shown in the
  *Cancelled* column — **not** a true cancelled-warrant figure), off-warrant not
  reported. The **weekly** report is the source of truth; the daily warrant
  report rides along as `shfe_warrant_daily_t` only.
- Missing days (holidays, blocked scrapes) are forward-filled (LOCF) for
  charting: `scripts/schema.build_daily_series` (x-axis = pipeline run date) and
  `build_asof_series` (x-axis = each report's own as-of date, staggered by lag —
  the dashboard default).