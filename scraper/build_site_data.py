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
"""

import json
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
    print(f"history.json: {len(history)} series, {n_monthly} monthly + "
          f"{n_daily} daily points ({history_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
