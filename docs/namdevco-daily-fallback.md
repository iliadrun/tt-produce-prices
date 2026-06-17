# namdevco daily-wholesale fallback — build notes (not yet built)

_Investigated 2026-06-14. Decision: documented and parked. No app code written._

## Why this exists

The app's daily wholesale prices come from **namistt.com** as a legacy `.xls`
(`scraper/fetch_daily.py` → `scraper/parse_daily_report.py`). The monthly retail
survey already prefers **namdevco.com** (`scraper/fetch_retail.py`), because
namistt's retail mirror lags ~2 months.

namdevco.com/market-information is the more actively maintained site overall.
Snapshot on Sun 2026-06-14:

| Feed | namdevco.com | namistt.com |
|------|--------------|-------------|
| Daily wholesale (Macoya) | **Fri Jun 12** | Thu Jun 11 (Jun 12 missing) |
| Monthly retail | **May 2026** | March 2026 |
| Historical monthly wholesale | **2006–2026** (to May) | link rolled back to 2006–**2023** |
| Fish markets (daily) | Jun 12 | stuck at May 11 |

The one-day daily gap may be timing, but the pattern says namistt is being wound
down. **This doc is insurance:** if namistt's daily `.xls` ever stops updating,
build the fallback below to keep the app running from namdevco.

## The catch: namdevco's daily report is a different shape

namistt's `.xls` is a **processed summary** (77 rows, one representative price per
commodity, root crops normalized to **per-Kg**, e.g. Carrot $15.43/Kg).

namdevco's daily report is the **raw feed**: 250+ rows split by **variety and
grade (A/B/C)**, in **native trade units only** (`100's`, `lb`, `20lb bag`,
`50lb bag`, `80lb bag`, `100lb bag`, `Bundle`, `Each`, `Head`) — **no Kg
anywhere**. So it is *not* a drop-in replacement. To keep charts continuous
(same `commodity_id`, same unit), the fallback must reproduce NAMDEVCO's own
summarization.

## How to fetch it

The market-information page is server-rendered HTML; the existing retail fetcher
already scrapes it. Daily PDFs are CloudFront-hosted with an epoch cache-buster:

```
//dal2rygekk7fq.cloudfront.net/www.namdevco.com/uploads/
  NorrisDeonarineNorthernWholesaleMarket{YYYYMMDD}and{YYYYMMDD}report{group|changes}daily.{epoch}.pdf
```

- `reportchangesdaily` — **use this one.** Day-over-day report; columns match the
  `.xls` schema (prev/current/change for volume and price). Filename spans two
  dates (`...20260611and20260612...` = previous and current day).
- `reportgroupdaily` — single-day Min/Max/**Mode** prices + volume. Different
  schema; ignore unless you decide to redesign around modal prices.

Scrape the newest `reportchangesdaily` href (highest epoch), prepend `https:`,
download. Validate by PDF magic bytes `%PDF`, not Content-Type (CloudFront serves
`application/octet-stream`). The true report date is in the PDF text line
`...Macoya for {YYYY-MM-DD}` and in PDF metadata ModDate.

## How to parse it

`pdfplumber` (NOT yet in `requirements.txt` — add it). `page.extract_text()` then
regex per data line:

```
NAME (Variety) (Grade) UNIT  vol_prev vol_curr vol_chg  $price_prev $price_curr $price_chg
e.g.  LIME (Tahiti) (A) 100's 430.00 105.00 -325.00 $140.00 $120.00 -$20.00
```

- Nulls render as `n/a`. Numbers may carry thousands commas (`$1,200.00`).
- The apostrophe in `100's` extracts mangled — normalize NFKD and replace
  `U+2019` / the replacement char.
- Category rows are ALL-CAPS with no digits: `CITRUS`, `CONDIMENTS AND SPICES`,
  `FRUITS`, `LEAFY VEGETABLES`, `ROOT CROPS`, `VEGETABLES`.
- The changes report has a `+/- :` legend line in the page footer.

## The summarization rule (reverse-engineered + verified)

Verified against the **2026-06-11 overlap**: the changes PDF carries the 06-11
column, and `data/daily/2026-06-11.json` (from namistt) is ground truth. The rule
that reproduces namistt **to the cent**:

1. **Take grade A.**
2. **Convert weight units to Kg** — `lb→kg = 0.45359237`; a bag price is
   `$ / (lbs_in_bag × 0.45359237)`. Verified exact: Carrot 15.43, Christophene
   13.23, Pumpkin 6.61, Cauliflower(Imported) 48.5, Patchoi 7, Cucumber ~11.
3. **Count/bundle units stay native** (`100's`, `Bundle`/`Bndl.`, `Each`,
   `Head`) — no conversion.
4. **namistt size suffix maps to namdevco grade**: `(L)↔A`, `(M)↔B`, `(S)↔C`.
   Every `(L)` row matched grade A exactly (Tomato(L) 17.64, Lime(L) 140,
   Lettuce(L) 8, Melongene(L) 13.23, Orange(L) 480, Coconut(L) 600).

### Mapping still required

- A per-commodity name/variety alias map (extend `data/commodity_aliases.json`):
  `Eddoe`↔`Eddoes` (plural), `Portugal`↔`Mandarine (Portugal)`,
  `Yam (Local)`↔`Yam (Common)`, plus choosing the right variety when several
  trade the same day.
- **Genuinely ambiguous — OMIT on fallback days, never guess** (a missing point
  is honest; a wrong-unit point silently corrupts a chart): ginger (local vs
  imported; ~20% off under the naive A-rule), green vs ripe banana, local
  cauliflower (only Imported seen 06-11), bodi (`5lb Bndl` vs `lb`), shadon beni
  (`Bndl` vs `lb`).

## Build checklist (when namistt breaks)

1. Add `pdfplumber` to `requirements.txt`.
2. `scraper/parse_namdevco_daily.py` — PDF → the same JSON schema as
   `parse_daily_report.py` (same keys, same `commodity_id`s, same units).
3. Validation harness: re-derive `data/daily/2026-06-11.json` from
   `samples/namdevco_NDNWM_changes.pdf`; require an exact match on every
   commodity it can map, and a printed list of any it omits.
4. Extend `data/commodity_aliases.json` with the namdevco name/variety map.
5. Wire into `scraper/fetch_daily.py` as a fallback: when namistt returns
   `not-published`/stale for a date, scrape namdevco, parse, write the same
   `data/daily/{date}.json` with a `"source": "namdevco"` marker.
6. Verify `build_site_data.py` output and the live site charts stay continuous
   across a fallback day.

## Reference artifacts

- `samples/namdevco_NDNWM_changes.pdf`, `samples/namdevco_NDNWM_group.pdf`
  (both 2026-06-12). namdevco **overwrites files in place**, so these captured
  copies won't be re-fetchable — keep them.
