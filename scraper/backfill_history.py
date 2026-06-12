"""Parse the NAMIS historical workbook of monthly average wholesale prices
(2006-2026, one sheet per year) into a single JSON time-series file.

Sheet layout (verified June 2026; varies slightly by year):
  - a title row, sometimes preceded by a blank row, so the header row
    ("Commodity" in column A) must be located dynamically
  - header: Commodity | Unit | January..December | Yearly Average
  - ALL-CAPS category rows interleaved, same convention as the daily
    report, except here they often carry literal '$' text in the month
    columns (which are not numbers, so a numeric-data check still works)
  - missing months are the literal text 'na' or 'NA'
  - sheet names are years, one with a trailing space ('2017 ')
  - the same produce can change name and unit across years ('Sweet Corn'
    sold per 'each' in 2015, 'Corn' per "100's" in 2016+), so the unit is
    recorded per year, and renamed commodities become separate series
"""

import argparse
import json
import sys
from pathlib import Path

import xlrd

from parse_daily_report import _canonical_category, _commodity_id, _number, _text

COL_COMMODITY = 0
COL_UNIT = 1
FIRST_MONTH_COL = 2  # January; December is col 13, Yearly Average col 14


def _price(cell):
    """Monthly price, or None for blank/'na'/'NA' cells."""
    if cell.ctype == xlrd.XL_CELL_TEXT and cell.value.strip().lower() in ("na", ""):
        return None
    return _number(cell)


def parse_history(path):
    book = xlrd.open_workbook(path)
    series = {}  # commodity_id -> series dict
    years = []

    for sheet in book.sheets():
        year_name = sheet.name.strip()
        if not (year_name.isdigit() and 2000 <= int(year_name) <= 2100):
            print(f"WARNING: skipping sheet {sheet.name!r} — not a year",
                  file=sys.stderr)
            continue
        year = int(year_name)
        years.append(year)

        header_row = None
        for r in range(min(sheet.nrows, 30)):
            if _text(sheet.cell(r, COL_COMMODITY)).lower() == "commodity":
                header_row = r
                break
        if header_row is None:
            print(f"WARNING: skipping sheet {sheet.name!r} — no 'Commodity' "
                  "header found", file=sys.stderr)
            continue

        category = None
        seen_this_year = {}  # commodity_id -> (unit, months) for dup checks
        for r in range(header_row + 1, sheet.nrows):
            name = _text(sheet.cell(r, COL_COMMODITY))
            unit = _text(sheet.cell(r, COL_UNIT))
            if not name:
                continue
            months = [_price(sheet.cell(r, FIRST_MONTH_COL + m)) for m in range(12)]
            if not unit:
                if all(p is None for p in months):
                    if name.isupper():
                        category = _canonical_category(name)
                    else:
                        print(f"WARNING: {year} row {r} ({name!r}) skipped — "
                              "no unit, no data", file=sys.stderr)
                    continue
                print(f"WARNING: {year} row {r} ({name!r}) has prices but no "
                      "unit; keeping", file=sys.stderr)

            cid = _commodity_id(name)
            if cid in seen_this_year:
                # The 2006 sheet has copy-pasted duplicate rows; identical
                # copies are silently fine, conflicting ones are not.
                if seen_this_year[cid] != (unit, months):
                    print(f"ERROR: {year} row {r} ({name!r}) duplicates id "
                          f"{cid!r} with DIFFERENT values; keeping first "
                          "occurrence — review the source file", file=sys.stderr)
                continue
            seen_this_year[cid] = (unit, months)

            s = series.setdefault(cid, {
                "commodity_id": cid,
                "names": [],
                "category": None,
                "unit_by_year": {},
                "monthly_avg_price": {},
            })
            if name not in s["names"]:
                s["names"].append(name)
            # Categories occasionally get re-filed between years; the most
            # recent year wins because sheets come newest-first.
            if s["category"] is None:
                s["category"] = category
            if unit:
                s["unit_by_year"][str(year)] = unit
            for m, price in enumerate(months, start=1):
                if price is not None:
                    s["monthly_avg_price"][f"{year}-{m:02d}"] = price

    for s in series.values():
        s["monthly_avg_price"] = dict(sorted(s["monthly_avg_price"].items()))

    n_obs = sum(len(s["monthly_avg_price"]) for s in series.values())
    return {
        "market": "Norris Deonarine Northern Wholesale Market, Macoya",
        "description": "Monthly average wholesale prices",
        "currency": "TTD",
        "source_file": Path(path).name,
        "years_covered": [min(years), max(years)],
        "series": sorted(series.values(),
                         key=lambda s: (s["category"] or "", s["commodity_id"])),
    }, n_obs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xls_file", help="Path to the WS Avg PRICES workbook")
    ap.add_argument("-o", "--output", required=True, help="Output JSON path")
    args = ap.parse_args()

    history, n_obs = parse_history(args.xls_file)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(history, indent=1), encoding="utf-8")
    lo, hi = history["years_covered"]
    print(f"Years: {lo}-{hi}")
    print(f"Series: {len(history['series'])} commodities, "
          f"{n_obs} monthly price points")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
