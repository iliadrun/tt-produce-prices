"""Fetch NAMDEVCO's monthly retail price survey and store it as JSON.

Two official copies of the workbook exist, and they disagree:
  - namdevco.com's Market Information page links the FRESH copy through a
    CloudFront URL whose filename changes on every upload
    (…/uploads/reportavgretailpricemonthly.<epoch>.xlsx), so the page is
    scraped for the link on every run — the file URL is never cached.
  - namistt.com mirrors the workbook at a stable URL but lags a month or
    two behind. It is the fallback when the namdevco fetch fails.

The workbook holds ONE month and is overwritten in place on the server —
months not captured when they appear are lost for good (there is no
official archive; Jan/Feb 2026 are already unrecoverable). Raw copies go
to data/raw/ and parsed JSON to data/retail/, both named by the survey
month read from the workbook's own TITLE cell, never the download date or
link text (both have lied before).

Validation is by file magic (xlsx files start with 'PK'), not Content-Type:
CloudFront serves the workbook as application/octet-stream, and namistt
answers some missing files with an HTML page and status 200.

Usage:
  py scraper/fetch_retail.py                              # fetch live
  py scraper/fetch_retail.py --file samples/foo.xlsx      # ingest a local copy
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from parse_retail_report import parse_retail_report

PAGE_URL = "https://www.namdevco.com/market-information/"
# The href is protocol-relative ("//dal2….cloudfront.net/…"). The page also
# carries a decoy "Retail Prices 2021" link to an unrelated file, so the
# match is keyed on the upload path, never on link text.
LINK_RE = re.compile(
    r'href="((?:https?:)?//[^"]*?/uploads/reportavgretailpricemonthly\.(\d+)\.xlsx)"')
# NAMDEVCO has renamed the filename scheme before (it was RetailPrices2021…
# in 2022) — if the exact pattern goes missing, any CloudFront-hosted
# retail xlsx is worth trying before giving up.
LOOSE_LINK_RE = re.compile(
    r'href="((?:https?:)?//[^"]*?cloudfront[^"]*?retail[^"]*?\.xlsx)"', re.I)
MIRROR_URL = ("https://www.namistt.com/DocumentLibrary/Market%20Reports/"
              "Yearly/Retail%20Prices%20{year}.xlsx")
XLSX_MAGIC = b"PK\x03\x04"
USER_AGENT = "TT-Produce-Prices/0.1 (civic project; contact: nlalai@gmail.com)"

PROJECT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_DIR / "data" / "raw"
RETAIL_DIR = PROJECT_DIR / "data" / "retail"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def known_epochs():
    """Upload timestamps of files already ingested — a cheap "anything new?"
    check that costs zero extra requests (the epoch is in the link itself)."""
    epochs = set()
    for path in RETAIL_DIR.glob("*.json"):
        try:
            epoch = json.loads(path.read_text(encoding="utf-8")).get("source_epoch")
        except (ValueError, OSError, AttributeError):
            continue  # an unreadable file just costs one redundant re-download
        if epoch:
            epochs.add(epoch)
    return epochs


def store(body, source_url=None, source_epoch=None):
    """Validate, parse, and save one workbook. Returns a status string."""
    if not body.startswith(XLSX_MAGIC):
        return "error: not an xlsx file (server may have sent an HTML page)"
    try:
        report = parse_retail_report(io.BytesIO(body))
    except Exception as e:
        # Archive the workbook anyway: the server overwrites it in place,
        # so an unparseable download (format change?) may be the only copy
        # of this month that ever exists.
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        quarantine = RAW_DIR / "Retail_Prices_UNPARSED.xlsx"
        quarantine.write_bytes(body)
        return (f"error: could not parse the workbook ({e}); "
                f"raw copy kept as {quarantine.name}")
    month = report["survey_month"]
    report["source_url"] = source_url
    report["source_epoch"] = source_epoch
    report["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    RETAIL_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RETAIL_DIR / f"{month}.json"
    if json_path.exists():
        try:
            old = json.loads(json_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            old = {}
        if old.get("items") == report["items"]:
            # Same survey content re-downloaded (the mirror and --file
            # paths can't see upload epochs). Keep the stored record —
            # rewriting fetched_at would commit-and-redeploy a no-news
            # diff on every run — but learn the epoch if this download
            # taught us one, so the dedupe works next run.
            if source_epoch and old.get("source_epoch") != source_epoch:
                old["source_epoch"] = source_epoch
                old["source_url"] = source_url
                json_path.write_text(json.dumps(old, indent=2), encoding="utf-8")
                return f"ok: survey for {month} unchanged (noted the new upload)"
            return f"ok: survey for {month} already stored, unchanged"

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"Retail_Prices_{month}.xlsx").write_bytes(body)
    revised = json_path.exists()
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return (f"ok: survey for {month}, {len(report['items'])} rows"
            + (" (replaced an earlier copy of the same month)" if revised else ""))


def fetch_namdevco():
    html = http_get(PAGE_URL).decode("utf-8", "replace")
    epoch = None
    m = LINK_RE.search(html)
    if m:
        epoch = m.group(2)
    else:
        m = LOOSE_LINK_RE.search(html)
    if not m:
        return "error: no retail workbook link on the market-information page"
    url = m.group(1)
    if url.startswith("//"):
        url = "https:" + url
    if epoch and epoch in known_epochs():
        return f"no new upload (still epoch {epoch})"
    time.sleep(2)  # politeness pause between the page hit and the download
    return store(http_get(url), source_url=url, source_epoch=epoch)


def fetch_mirror():
    """The namistt.com fallback. The mirror lags a month or two, so well
    into a new year its new file may not exist yet — the previous year is
    tried too (harmless: files are stored by the month in their title).
    Any per-year failure moves on to the next year, including the server's
    trick of answering a missing file with an HTML page and status 200."""
    today = datetime.now(timezone.utc)
    years = [today.year] + ([today.year - 1] if today.month <= 3 else [])
    last_error = "error: mirror has no retail file for the current year"
    for year in years:
        url = MIRROR_URL.format(year=year)
        try:
            body = http_get(url)
        except urllib.error.HTTPError as e:
            last_error = f"error: mirror HTTP {e.code}"
            continue
        status = store(body, source_url=url)
        if not status.startswith("error"):
            return status
        last_error = status
    return last_error


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="Ingest a local workbook instead of fetching")
    args = ap.parse_args()

    if args.file:
        status = store(Path(args.file).read_bytes())
        print(f"{args.file}: {status}")
        sys.exit(1 if status.startswith("error") else 0)

    try:
        status = fetch_namdevco()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        status = f"error: {e}"
    print(f"namdevco.com: {status}")
    failed = status.startswith("error")

    if failed:
        # Whatever broke the primary — network, a page redesign, an
        # unparseable workbook — the lagging mirror is still worth trying:
        # a month not captured somewhere is lost for good.
        try:
            status = fetch_mirror()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status = f"error: {e}"
        print(f"namistt.com mirror: {status}")

    # A namdevco failure stays a failure even when the mirror delivered:
    # the mirror lags, and a quietly-green run would hide a broken primary
    # until the gap becomes a permanently lost month.
    sys.exit(1 if failed or status.startswith("error") else 0)


if __name__ == "__main__":
    main()
