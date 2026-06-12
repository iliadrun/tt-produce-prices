"""Parse a NAMIS daily wholesale market report (.xls) into structured data.

The Norris Deonarine Northern Wholesale Market daily report is a legacy
Excel 97-2003 (.xls) file, so it must be read with xlrd — openpyxl only
handles modern .xlsx files.

Layout of the sheet (verified against the 11 June 2026 report):
  - rows 0-8: merged title banner and blank spacer rows
  - row 9:    group headers ("Volumes" / "Prices ($/Unit)")
  - row 10:   column headers; the four date columns hold raw Excel date
              serial numbers, not text
  - rows 11+: commodity rows, with ALL-CAPS category rows (e.g. "ROOT
              CROPS") interleaved — a category row has a name in column A
              but no unit in column B
Untraded commodities have blank volume/price cells but a computed 0.0 in
the Increase/Decrease columns, so blanks must stay None rather than 0.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import xlrd

COL_COMMODITY = 0
COL_UNIT = 1
COL_VOL_PREV = 2
COL_VOL_CURR = 3
COL_VOL_CHANGE = 4
COL_PRICE_PREV = 5
COL_PRICE_CURR = 6
COL_PRICE_CHANGE = 7


def _text(cell):
    """Cell text, or '' for empty/whitespace cells."""
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return cell.value.strip()
    return ""


def _number(cell):
    """Cell numeric value rounded to 2 decimals (prices are TTD cents and
    volumes are kg — Excel's float arithmetic leaks artifacts like
    -2721.5999999999995 otherwise), or None for anything that isn't a number."""
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return round(cell.value, 2)
    return None


def _serial_to_iso(serial, datemode):
    return xlrd.xldate.xldate_as_datetime(serial, datemode).date().isoformat()


def _commodity_id(name):
    """Stable join key across days. The source files are hand-edited, so
    the same commodity drifts in spacing and case between reports
    ("Dasheen(Local)" vs "Eddoe (Local)", "Thyme (s)" vs "Thyme (S)");
    joining on the raw name would fragment a commodity's price history."""
    s = re.sub(r"\s*\(", " (", name)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _canonical_category(name):
    return name.title().replace(" And ", " and ").replace(" Of ", " of ")


def parse_daily_report(path):
    """Return {"market", "report_date", "previous_date", "source_file",
    "commodities": [...]} for one daily report file."""
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(0)

    # Find the header row by its "Commodity" label instead of assuming row
    # 10 — older reports may have a different amount of banner above it.
    header_row = None
    for r in range(min(sheet.nrows, 30)):
        if _text(sheet.cell(r, COL_COMMODITY)).lower() == "commodity":
            header_row = r
            break
    if header_row is None:
        raise ValueError(f"No 'Commodity' header row found in {path}")

    prev_serial = sheet.cell(header_row, COL_VOL_PREV).value
    curr_serial = sheet.cell(header_row, COL_VOL_CURR).value
    previous_date = _serial_to_iso(prev_serial, book.datemode)
    report_date = _serial_to_iso(curr_serial, book.datemode)

    commodities = []
    category = None
    for r in range(header_row + 1, sheet.nrows):
        name = _text(sheet.cell(r, COL_COMMODITY))
        unit = _text(sheet.cell(r, COL_UNIT))
        if not name:
            continue
        if not unit:
            # Category rows ("ROOT CROPS", "CITRUS", ...) carry the section
            # for every commodity row beneath them. Guard: a commodity row
            # whose unit cell was accidentally left blank must not be
            # swallowed as a category — categories are all-caps and have no
            # numbers in the data columns.
            has_data = any(
                _number(sheet.cell(r, c)) is not None
                for c in range(COL_VOL_PREV, COL_PRICE_CHANGE + 1)
            )
            if not has_data:
                if name.isupper():
                    category = _canonical_category(name)
                else:
                    print(f"WARNING: row {r} ({name!r}) skipped — no unit, "
                          "no data", file=sys.stderr)
                continue
            print(f"WARNING: row {r} ({name!r}) has no unit but has data; "
                  "treating as commodity", file=sys.stderr)
        price = _number(sheet.cell(r, COL_PRICE_CURR))
        volume = _number(sheet.cell(r, COL_VOL_CURR))
        commodities.append({
            "commodity": name,
            "commodity_id": _commodity_id(name),
            "category": category,
            "unit": unit or None,
            "volume_previous": _number(sheet.cell(r, COL_VOL_PREV)),
            "volume": volume,
            "volume_change": _number(sheet.cell(r, COL_VOL_CHANGE)),
            "price_previous": _number(sheet.cell(r, COL_PRICE_PREV)),
            "price": price,
            "price_change": _number(sheet.cell(r, COL_PRICE_CHANGE)),
            "traded": price is not None or volume is not None,
        })

    return {
        "market": "Norris Deonarine Northern Wholesale Market, Macoya",
        "report_date": report_date,
        "previous_date": previous_date,
        "currency": "TTD",
        "source_file": Path(path).name,
        "commodities": commodities,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xls_file", help="Path to a NAMIS daily report .xls")
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()

    report = parse_daily_report(args.xls_file)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        traded = sum(1 for c in report["commodities"] if c["traded"])
        print(f"Report date: {report['report_date']} (vs {report['previous_date']})")
        print(f"Commodities: {len(report['commodities'])} listed, {traded} traded")
        print(f"Wrote {out}")
    else:
        json.dump(report, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
