# T&T Produce Prices 🥕

A free web app that makes Trinidad and Tobago's daily wholesale produce
prices easy to browse and search, with price history charts going back to
2006\. Prices are collected and published by
[NAMIS](https://www.namistt.com) — the National Agricultural Market
Information System, a service of NAMDEVCO (the National Agricultural
Marketing and Development Corporation of Trinidad and Tobago). This project
is an independent, non-commercial presentation of that public data.

## How it works

```
namistt.com spreadsheets ──► scraper (Python) ──► data/ (JSON) ──► site/ (static website)
```

1. **`scraper/fetch_daily.py`** downloads the daily wholesale report for the
   Norris Deonarine Northern Wholesale Market, Macoya (published weekdays,
   except public holidays) and parses it with **`parse_daily_report.py`**
   into `data/daily/YYYY-MM-DD.json`.
2. **`scraper/backfill_history.py`** parsed NAMIS's 2006–2026 historical
   workbook into `data/history/monthly_wholesale_avg.json` (a one-time
   backfill). `data/commodity_aliases.json` bridges spelling differences
   between the two sources.
3. **`scraper/fetch_retail.py`** checks NAMDEVCO's monthly retail survey
   (average shop and market prices by outlet type) and parses it with
   **`parse_retail_report.py`** into `data/retail/YYYY-MM.json`. The source
   workbook is overwritten in place each month with no official archive, so
   every captured month is preserved here. `data/retail_aliases.json`
   bridges retail base names onto the wholesale ones.
4. **`scraper/build_site_data.py`** compiles everything into the two small
   files the website reads: `site/data/summary.json` and
   `site/data/history.json`.
5. **`site/`** is a plain HTML/CSS/JavaScript website — no build step.
6. **`.github/workflows/update-prices.yml`** runs steps 1, 3 and 4
   automatically on GitHub's servers every weekday afternoon (Trinidad
   time), commits any new data, and redeploys the site to GitHub Pages.

## Running it locally

Requires Python 3.12+ with `xlrd` and `openpyxl`
(`pip install -r requirements.txt`).

```
python scraper/fetch_daily.py --since 2026-06-01   # download recent reports
python scraper/build_site_data.py                  # rebuild site data files
python -m http.server 8742 --directory site        # then open localhost:8742
```

## Data notes

- Prices are **wholesale** (what vendors pay at Macoya), in TT dollars, per
  the unit shown (Kg, bundles, per-100, …). Retail prices are higher.
- The "in shops and markets" lines are monthly survey averages, best grade
  where graded (mixing grades made some items look cheaper retail than
  wholesale). Per-Kg retail is shown only where both sides price by
  weight — a per-lb shop price is never compared against a per-bundle or
  per-100 wholesale price.
- A price change is only shown when the commodity traded with a posted
  price on both days — the source computes change cells even for items
  that stopped trading, which would otherwise show phantom moves.
- Commodities are joined across days and datasets by a normalised
  `commodity_id`, never by raw name (the source spreadsheets are hand-typed
  and spellings drift).

## Terms

NAMDEVCO's site terms allow free use of this data for non-commercial,
non-profit purposes; commercial use requires their written permission.
This project is and stays non-commercial. Data © NAMDEVCO; always confirm
prices at the market.
