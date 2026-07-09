#!/usr/bin/env python3
"""
track_new_tickers.py — Track new tickers by first-seen date.
Writes /tmp/new_tickers.json with tickers first seen TODAY.

Logic:
  - prev_tickers.json = history dict {ticker: "YYYY-MM-DD"}
  - NEW = ticker whose first_seen date == today
  - If a ticker appears for the first time today, it stays NEW all day
    (every 30-min scan run keeps marking it NEW until tomorrow)
  - Compatible with legacy format {"tickers": [...], "date": "..."}

Usage:
  python track_new_tickers.py scan1.json scan2.json scan3.json
"""
import json
import os
import sys
from datetime import date

today = date.today().isoformat()
scan_files = sys.argv[1:] if len(sys.argv) > 1 else []
prev_file  = "prev_tickers.json"
out_file   = "/tmp/new_tickers.json"

# Load history {ticker: first_seen_date}
history = {}
if os.path.exists(prev_file):
    try:
        with open(prev_file) as f:
            data = json.load(f)
        if isinstance(data, dict):
            if "tickers" in data:
                # Legacy "prev-scan" format: {"tickers": [...], "date": "..."}
                # Treat all those tickers as seen on the stored date (or yesterday if no date)
                seen_date = data.get("date", "1970-01-01")
                for t in data["tickers"]:
                    history[t] = seen_date
            else:
                # Correct "first-seen" format: {ticker: "YYYY-MM-DD"}
                history = data
        elif isinstance(data, list):
            # bare list — treat as seen yesterday
            from datetime import date, timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            for t in data:
                history[t] = yesterday
    except Exception as e:
        print(f"Warning reading {prev_file}: {e}", file=sys.stderr)

# Collect all current tickers from all scan files (preserve order, deduplicate)
current = []
current_set = set()
for fn in scan_files:
    try:
        with open(fn) as f:
            data = json.load(f)
        for t in data.get("tickers", []):
            ticker = t["ticker"] if isinstance(t, dict) else str(t)
            if ticker and ticker not in current_set:
                current.append(ticker)
                current_set.add(ticker)
    except Exception as e:
        print(f"Warning reading {fn}: {e}", file=sys.stderr)

# Update history — record FIRST seen date (never overwrite an older date)
for t in current:
    if t not in history:
        history[t] = today          # first time ever seen — mark as today

# Save updated history {ticker: first_seen_date}
with open(prev_file, "w") as f:
    json.dump(history, f, indent=2, sort_keys=True)

# NEW = first seen today (stays NEW all day across every 30-min run)
new_today = [t for t in current if history.get(t) == today]

# Save for tv_enrich.py
with open(out_file, "w") as f:
    json.dump(new_today, f)

print(f"Total: {len(current)}, History: {len(history)}, NEW today: {len(new_today)}")
if new_today:
    print(f"NEW: {', '.join(sorted(new_today)[:30])}")
else:
    print("No new tickers today.")
