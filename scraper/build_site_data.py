"""Compile data/ into the compact JSON files the website loads.

Produces:
  site/data/summary.json — the latest day's market board. Small; fetched on
      every page load.
  site/data/history.json — per-commodity price series (monthly averages
      2006+, daily prices June 2026+). Larger; the site fetches it lazily
      the first time a visitor opens a chart.

Rules learned from verifying the source data:
  - price_change is only emitted when BOTH days' prices exist; NAMIS
    computes a change cell even when a commodity stopped trading, which
    would otherwise show phantom moves like -350.
  - Commodities not traded today still appear, with their most recent
    traded price so the page can say "last sold at X on date Y".
  - History series are keyed by the daily report's commodity_id, bridged
    across the two sources' spellings via data/commodity_aliases.json.
  - Retail survey prices (data/retail/, monthly) are attached per commodity
    as the compact "in shops and markets" figures, bridged by base name via
    data/retail_aliases.json. See retail_summary() for the comparison rules
    that keep wholesale-vs-retail honest.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"
SITE_DATA_DIR = PROJECT_DIR / "site" / "data"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(sorted_vals, q):
    """Linear-interpolated percentile of an ascending list, q in [0, 1]."""
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def month_index(date_key):
    """Months since year 0 for a 'YYYY-MM' or 'YYYY-MM-DD' key."""
    return int(date_key[:4]) * 12 + int(date_key[5:7])


LB_PER_KG = 2.20462
RETAIL_OUTLETS = ["farmers_markets", "municipal_markets", "vege_marts",
                  "supermarkets"]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def median(vals):
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def load_retail():
    """The newest retail survey, grouped by wholesale base name."""
    files = sorted((DATA_DIR / "retail").glob("*.json"))
    if not files:
        return None, {}
    retail = load(files[-1])
    aliases = load(DATA_DIR / "retail_aliases.json")["retail_to_wholesale_base"]
    groups = {}
    for item in retail["items"]:
        base = aliases.get(item["base_id"], item["base_id"])
        groups.setdefault(base, []).append(item)
    return retail["survey_month"], groups


def retail_summary(entry, rows):
    """The compact "in shops and markets" figures for one wholesale item.

    Rules from empirically comparing the March 2026 survey against June
    2026 wholesale prices (22 comparable items):
      - Best grade only: pooling grades A/B/C dragged 4 of 22 items below
        the wholesale price, which reads as a bug; grade-A medians fixed
        all 4 (typical markup ~1.4x).
      - Converted to per-Kg ONLY when wholesale sells per Kg and retail
        per lb. Other unit pairs (100's vs Each, Bndl. vs a Bundle of
        unknown size) are not comparable — those keep the retail price in
        its own published unit and imply no comparison.
      - When outlet medians disagree more than 3x (sweet pepper spans
        market stalls to supermarket imports), one "about" number is a
        lie — "wide" makes the site show the range instead of the point.
    """
    # Wholesale (Local)/(Imported) variants compare against the matching
    # retail rows only: imported cauliflower retails well above local, and
    # mixing them skews the figure. The survey writes compound varieties
    # ("Imported Green", "Local Dry", "Green Fig Local"), so the match is
    # by word, never by the whole string. Local produce is usually named
    # by its cultivar with no "Local" marker, so for a (Local) item
    # anything not marked imported counts. An (Imported) item with no
    # imported rows gets NO retail figure — showing it local prices would
    # be worse than showing nothing.
    qual = re.search(r"\(([^()]*)\)", entry["name"])
    if qual and qual.group(1).strip().lower() in ("local", "imported"):
        wanted = qual.group(1).strip().lower()

        def variety_words(r):
            return re.split(r"[^a-z]+", (r["variety"] or "").lower())

        if wanted == "imported":
            rows = [r for r in rows if "imported" in variety_words(r)]
        else:
            explicit = [r for r in rows if "local" in variety_words(r)]
            rows = explicit or [r for r in rows
                                if "imported" not in variety_words(r)]
        if not rows:
            return None

    lb_rows = [r for r in rows if r["unit"].lower() == "lb"]
    if entry["unit"] == "Kg" and lb_rows:
        chosen, kind, unit, factor = lb_rows, "kg", None, LB_PER_KG
    else:
        counts = {}
        for r in rows:
            counts[r["unit"]] = counts.get(r["unit"], 0) + 1
        unit = max(counts, key=lambda u: counts[u])
        chosen = [r for r in rows if r["unit"] == unit]
        kind, factor = "native", 1.0

    graded = [r for r in chosen if (r["grade"] or "").strip().upper() == "A"]
    chosen = graded or chosen

    def values(key):
        return [r["prices"][key] for r in chosen
                if isinstance(r["prices"][key], (int, float))
                and r["prices"][key] > 0]

    vals = values("all_retail")
    if not vals:
        return None
    result = {"kind": kind, "price": round(median(vals) * factor, 2)}
    if kind == "native":
        result["unit"] = unit

    outlet_medians = {}
    for key in RETAIL_OUTLETS:
        ov = values(key)
        if ov:
            outlet_medians[key] = median(ov) * factor
    if len(outlet_medians) >= 2:
        cheap = min(outlet_medians, key=lambda k: outlet_medians[k])
        dear = max(outlet_medians, key=lambda k: outlet_medians[k])
        if outlet_medians[dear] > outlet_medians[cheap]:
            result["cheap"] = {"at": cheap, "price": round(outlet_medians[cheap], 2)}
            result["dear"] = {"at": dear, "price": round(outlet_medians[dear], 2)}
            if outlet_medians[dear] / outlet_medians[cheap] > 3:
                result["wide"] = True
    return result


def main():
    daily_files = sorted((DATA_DIR / "daily").glob("*.json"))
    if not daily_files:
        raise SystemExit("No daily data found — run fetch_daily.py first")
    dailies = [load(p) for p in daily_files]
    latest = dailies[-1]

    # Most recent traded price per commodity, and the full daily series.
    daily_series = {}   # id -> {date: price}
    last_traded = {}    # id -> {"date": ..., "price": ...}
    for report in dailies:
        for c in report["commodities"]:
            if c["price"] is not None:
                cid = c["commodity_id"]
                daily_series.setdefault(cid, {})[report["report_date"]] = c["price"]
                last_traded[cid] = {"date": report["report_date"], "price": c["price"]}

    # Monthly averages re-keyed to the daily report's ids, loaded before the
    # board is built so each commodity can carry its own past-year context.
    aliases = load(DATA_DIR / "commodity_aliases.json")["history_to_daily"]
    latest_ids = {c["commodity_id"] for c in latest["commodities"]}
    monthly_by_id = {}
    for s in load(DATA_DIR / "history" / "monthly_wholesale_avg.json")["series"]:
        cid = aliases.get(s["commodity_id"], s["commodity_id"])
        if cid in latest_ids:
            # The site's 1Y/5Y chart windows slice the last N keys, so
            # chronological order is a hard requirement, not a nicety.
            monthly_by_id[cid] = dict(sorted(s["monthly_avg_price"].items()))

    retail_month, retail_groups = load_retail()

    commodities = []
    for c in latest["commodities"]:
        cid = c["commodity_id"]
        entry = {
            "id": cid,
            "name": c["commodity"],
            "category": c["category"],
            "unit": c["unit"],
            "price": c["price"],
            "volume": c["volume"],
            "traded": c["traded"],
        }
        if c["price"] is not None and c["price_previous"] is not None:
            entry["price_change"] = round(c["price"] - c["price_previous"], 2)
        lt = last_traded.get(cid)
        if lt and c["price"] is None:
            entry["last_traded"] = lt
        # Past-year context: where today's price sits among the last 12
        # published monthly averages (the monthly source lags a few months,
        # so this is "the most recent year on record"). Quartile thresholds
        # rather than min/max, so one freak month doesn't define "normal".
        # Guarded against dead or gappy series: the window must end near the
        # report date and fit inside ~14 calendar months — without this,
        # ginger (whose monthly series stopped in 2024) wears a "low for
        # the year" badge computed entirely from old data.
        months = sorted(monthly_by_id.get(cid, {}).items())[-12:]
        window_ok = (
            len(months) >= 6
            and month_index(latest["report_date"]) - month_index(months[-1][0]) <= 6
            and month_index(months[-1][0]) - month_index(months[0][0]) <= 13
        )
        if c["price"] is not None and window_ok:
            vals = sorted(v for _, v in months)
            entry["year_low"] = round(vals[0], 2)
            entry["year_high"] = round(vals[-1], 2)
            if c["price"] <= percentile(vals, 0.25):
                entry["price_level"] = "low"
            elif c["price"] >= percentile(vals, 0.75):
                entry["price_level"] = "high"
            else:
                entry["price_level"] = "typical"
        rows = retail_groups.get(slugify(c["commodity"].split("(")[0]))
        if rows:
            shops = retail_summary(entry, rows)
            if shops:
                entry["retail"] = shops
        commodities.append(entry)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": latest["market"],
        "report_date": latest["report_date"],
        "previous_date": latest["previous_date"],
        "currency": latest["currency"],
        # Report order, not alphabetical: the market lists staples (root
        # crops, vegetables) before citrus, which suits shoppers better.
        "categories": list(dict.fromkeys(c["category"] for c in latest["commodities"])),
        "commodities": commodities,
    }
    if retail_month:
        summary["retail_month"] = retail_month

    # History: the monthly series loaded above, plus the daily series.
    history = {cid: {"monthly": monthly} for cid, monthly in monthly_by_id.items()}
    for cid, series in daily_series.items():
        history.setdefault(cid, {})["daily"] = dict(sorted(series.items()))

    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = SITE_DATA_DIR / "summary.json"
    history_path = SITE_DATA_DIR / "history.json"
    summary_path.write_text(json.dumps(summary, separators=(",", ":")),
                            encoding="utf-8")
    history_path.write_text(json.dumps(history, separators=(",", ":")),
                            encoding="utf-8")

    n_monthly = sum(len(h.get("monthly", {})) for h in history.values())
    n_daily = sum(len(h.get("daily", {})) for h in history.values())
    print(f"summary.json: {latest['report_date']}, "
          f"{len(commodities)} commodities "
          f"({summary_path.stat().st_size // 1024} KB)")
    n_retail = sum(1 for c in commodities if "retail" in c)
    if retail_month:
        print(f"retail: {retail_month} survey attached to "
              f"{n_retail} of {len(commodities)} commodities")
    print(f"history.json: {len(history)} series, {n_monthly} monthly + "
          f"{n_daily} daily points ({history_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
