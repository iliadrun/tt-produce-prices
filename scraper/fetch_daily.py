"""Fetch NAMIS daily wholesale market reports and store them as clean JSON.

Downloads the Norris Deonarine Northern Wholesale Market daily report for a
date (or range of dates), saves the raw .xls under data/raw/, parses it with
parse_daily_report, and writes JSON under data/daily/.

Server behavior this must handle (verified June 2026):
  - Reports exist for weekdays only; missing dates return a real 404.
  - BUT some bad URLs return HTTP 200 with a stale HTML page instead of a
    404, so a 200 alone is not proof of success — the Content-Type must be
    an Excel MIME type.
  - Reports are uploaded between roughly 11:20 and 14:30 Trinidad time
    (UTC-4), so "today's" report may simply not be up yet.

Usage:
  py scraper/fetch_daily.py                    # today (Trinidad time)
  py scraper/fetch_daily.py --date 2026-06-10
  py scraper/fetch_daily.py --since 2026-06-01 # catch up a date range
  py scraper/fetch_daily.py --force            # re-download even if present
"""

import argparse
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from parse_daily_report import parse_daily_report
import json

BASE_URL = ("https://www.namistt.com/DocumentLibrary/Market%20Reports/Daily/"
            "Norris%20Deonarine%20NWM%20Daily%20Market%20Report%20-%20"
            "{day:02d}%20{month}%20{year}.xls")
EXCEL_MIME_TYPES = (
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
# Identify ourselves honestly so NAMDEVCO can see who is fetching and
# reach out if they ever have concerns.
USER_AGENT = ("TT-Produce-Prices/0.1 (civic project; contact: nlalai@gmail.com)")

PROJECT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_DIR / "data" / "raw"
DAILY_DIR = PROJECT_DIR / "data" / "daily"

TRINIDAD_TZ = timezone(timedelta(hours=-4))  # AST year-round, no DST


def report_url(d):
    return BASE_URL.format(day=d.day, month=d.strftime("%B"), year=d.year)


def fetch_report(d, force=False):
    """Fetch one date's report. Returns a status string:
    'ok', 'cached', 'not-published', or 'error: ...'."""
    raw_path = RAW_DIR / f"NDNWM_Daily_{d.isoformat()}.xls"
    json_path = DAILY_DIR / f"{d.isoformat()}.json"
    if json_path.exists() and not force:
        return "cached"

    req = urllib.request.Request(report_url(d), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content_type = resp.headers.get_content_type()
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "not-published"
        return f"error: HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return f"error: {e}"

    # A 200 with an HTML Content-Type is the server's stale fallback page,
    # not a report.
    if content_type not in EXCEL_MIME_TYPES:
        return f"error: unexpected content type {content_type!r}"

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(body)

    report = parse_daily_report(raw_path)
    if report["report_date"] != d.isoformat():
        return (f"error: file says report_date={report['report_date']}, "
                f"expected {d.isoformat()}")

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    traded = sum(1 for c in report["commodities"] if c["traded"])
    return f"ok ({len(report['commodities'])} commodities, {traded} traded)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="Fetch this date (YYYY-MM-DD)")
    ap.add_argument("--since", help="Fetch every weekday from this date through today")
    ap.add_argument("--force", action="store_true", help="Re-download even if already stored")
    args = ap.parse_args()

    today = datetime.now(TRINIDAD_TZ).date()
    if args.since:
        start = date.fromisoformat(args.since)
        dates = [start + timedelta(days=i) for i in range((today - start).days + 1)]
        # Weekends are skipped in range mode: reports are weekday-only.
        dates = [d for d in dates if d.weekday() < 5]
    elif args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        dates = [today]

    failures = 0
    hit_server_last_time = False
    for d in dates:
        # Pause between consecutive server hits so a long catch-up range
        # doesn't burst-download; cached dates skip the server entirely.
        if hit_server_last_time:
            time.sleep(2)
        status = fetch_report(d, force=args.force)
        hit_server_last_time = status != "cached"
        print(f"{d.isoformat()} ({d.strftime('%a')}): {status}")
        if status.startswith("error"):
            failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
