"""Fetch NAMDEVCO's monthly wholesale price & volume history and store it as JSON.

The Norris Deonarine Northern Wholesale Market (Macoya) publishes two cumulative
workbooks covering 2006 to the present month, one sheet per year:
  - NWMPrices… — monthly AVERAGE wholesale price per commodity
  - NWMVols…   — monthly TOTAL wholesale volume (Kg) per commodity
Both are linked from namdevco.com's Market Information page through a CloudFront
URL whose filename carries an upload epoch that changes on every re-upload
(…/uploads/NWMPrices20062026.<epoch>.xls). The page is scraped for the link on
every run, and the epoch is a free "is there anything new?" check — when it
matches what we already stored, nothing is downloaded.

Unlike the retail fetcher, there is deliberately NO namistt.com fallback. As of
June 2026 namistt's copy of this same history had been ROLLED BACK to 2006-2023
(see docs/namdevco-daily-fallback.md); falling back to it would overwrite three
years of real data with a stale file. If the namdevco fetch fails, the stored
JSON is left untouched and the run is flagged. A second guard refuses to write
whenever the freshly parsed workbook ends in an EARLIER month than the data
already on disk — insurance against a future rollback or a corrupt upload
silently regressing the site.

These are legacy Excel 97-2003 (.xls / OLE2) files, parsed by xlrd through
backfill_history.parse_history — the very same parser used for the one-time
historical backfill, so the auto-fetched output is identical to what a manual
backfill produces. Validation is by OLE2 file magic, not Content-Type:
CloudFront serves the workbook as application/octet-stream, and a missing file
can come back as an HTML page with status 200.

Usage:
  py scraper/fetch_monthly_wholesale.py            # fetch live
  py scraper/fetch_monthly_wholesale.py --dry-run  # fetch but don't write files
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from backfill_history import METRICS, parse_history

PAGE_URL = "https://www.namdevco.com/market-information/"
# .xls (OLE2 / BIFF) compound-document signature. The retail workbook is a
# modern .xlsx (a zip, magic "PK"); these history workbooks are the old format.
OLE2_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
USER_AGENT = "TT-Produce-Prices/0.1 (civic project; contact: nlalai@gmail.com)"

PROJECT_DIR = Path(__file__).resolve().parents[1]
HISTORY_DIR = PROJECT_DIR / "data" / "history"

# One entry per workbook. The href is matched on the exact NWMPrices / NWMVols
# token anchored to the /uploads/ path, so the page's other monthly wholesale
# files (OVFMWholesalePrices…, POSFMWholesalePrices… for the fish markets) can
# never match. The \d* absorbs the "20062026" year-range label, which rolls
# forward each January; (\d+) captures the upload epoch.
TARGETS = [
    {
        "metric": "price",
        "out": "monthly_wholesale_avg.json",
        "label": "monthly average prices",
        "link_re": re.compile(
            r'href="((?:https?:)?//[^"]*?/uploads/NWMPrices\d*\.(\d+)\.xls)"'),
    },
    {
        "metric": "volume",
        "out": "monthly_wholesale_volume.json",
        "label": "monthly volumes",
        "link_re": re.compile(
            r'href="((?:https?:)?//[^"]*?/uploads/NWMVols\d*\.(\d+)\.xls)"'),
    },
]


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def latest_month(history, field):
    """The most recent YYYY-MM present across every series, or None."""
    newest = ""
    for s in history.get("series", []):
        for key in s.get(field, {}):
            if key > newest:
                newest = key
    return newest or None


def process(target, html, dry_run=False):
    """Fetch, validate, parse, and store one workbook. Returns a status string.

    On any error the stored JSON is left untouched: a bad download must never
    clobber good history, which is exactly what we are trying to keep fresh.
    """
    field = METRICS[target["metric"]][0]
    out_path = HISTORY_DIR / target["out"]
    try:
        stored = json.loads(out_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        stored = {}

    m = target["link_re"].search(html)
    if not m:
        return f"error: no {target['label']} workbook link on the page"
    url, epoch = m.group(1), m.group(2)
    if url.startswith("//"):
        url = "https:" + url

    if stored.get("source_epoch") == epoch:
        return f"no new upload (still epoch {epoch})"

    time.sleep(2)  # politeness pause between the page hit and the download
    body = http_get(url)
    if not body.startswith(OLE2_MAGIC):
        return "error: not an .xls file (server may have sent an HTML page)"

    try:
        history, n_obs = parse_history(body, target["metric"],
                                       source_name=url.rsplit("/", 1)[-1])
    except Exception as e:
        # A format change or a corrupt/truncated workbook must fail loudly and
        # leave the good stored data in place — never crash the whole run.
        return f"error: could not parse the workbook ({e})"

    new_latest = latest_month(history, field)
    old_latest = latest_month(stored, field)
    # Refuse to write a workbook that parsed to nothing (valid .xls magic but no
    # usable rows) — without this the rollback guard below is bypassed when
    # new_latest is None and an empty file would wipe the entire history.
    if n_obs == 0 or new_latest is None:
        return "error: parsed workbook has no monthly data; refusing to overwrite"
    if old_latest and new_latest < old_latest:
        return (f"error: fetched workbook ends {new_latest}, earlier than the "
                f"stored {old_latest}; refusing to overwrite (possible rollback)")

    # No-news guard: NAMDEVCO occasionally re-uploads with a fresh epoch but
    # identical data. Rewriting then would commit-and-redeploy a no-op every
    # such run, so record only the new epoch — enough for the cheap pre-download
    # check to skip it next time (mirrors fetch_retail.py).
    if stored.get("series") == history["series"]:
        if dry_run:
            return (f"ok (dry run): {target['label']} unchanged through "
                    f"{new_latest} (new epoch {epoch})")
        stored["source_url"] = url
        stored["source_epoch"] = epoch
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(stored, indent=1), encoding="utf-8")
        return (f"ok: {target['label']} unchanged through {new_latest} "
                f"(noted new epoch {epoch})")

    history["source_url"] = url
    history["source_epoch"] = epoch
    history["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    grew = "" if not old_latest else (
        f", was through {old_latest}" if new_latest != old_latest else ", same latest month")
    if dry_run:
        return (f"ok (dry run): {target['label']} through {new_latest}, "
                f"{n_obs} points (epoch {epoch}{grew})")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(history, indent=1), encoding="utf-8")
    return (f"ok: {target['label']} through {new_latest}, {n_obs} points "
            f"(epoch {epoch}{grew})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and report, but do not write the JSON files")
    args = ap.parse_args()

    try:
        html = http_get(PAGE_URL).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"namdevco.com: error fetching the market-information page: {e}")
        sys.exit(1)

    failed = False
    for target in TARGETS:
        try:
            status = process(target, html, dry_run=args.dry_run)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status = f"error: {e}"
        print(f"{target['label']}: {status}")
        failed |= status.startswith("error")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
