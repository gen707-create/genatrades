#!/usr/bin/env python3
"""
track_new_tickers.py — Compare current scan results with previous day.
Writes /tmp/new_tickers.json with tickers first seen today.
Updates prev_tickers.json in the repo root.

Usage:
  python track_new_tickers.py scan1.json scan2.json scan3.json
"""
import json
import os
import sys
from datetime import date

today = date.today().isoformat()
scan_files = sys.argv[1:] if len(sys.argv) > 1 else []
prev_file = "prev_tickers.json"
out_file = "/tmp/new_tickers.json"

# Load history {ticker: first_seen_date}
history = {}
if os.path.exists(prev_file):
    with open(prev_file) as f:
        history = json.load(f)

# Collect all current tickers from all scan files
current = set()
for fn in scan_files:
    try:
        with open(fn) as f:
            data = json.load(f)
        for t in data.get("tickers", []):
            ticker = t["ticker"] if isinstance(t, dict) else str(t)
            if ticker:
                current.add(ticker)
    except Exception as e:
        print(f"Warning reading {fn}: {e}", file=sys.stderr)

# Update history — record first seen date for new tickers
for t in current:
    if t not in history:
        history[t] = today

# Save updated history
with open(prev_file, "w") as f:
    json.dump(history, f, indent=2, sort_keys=True)

# New tickers = first seen today
new_today = [t for t in current if history.get(t) == today]

# Save new tickers list for tv_enrich.py
with open(out_file, "w") as f:
    json.dump(new_today, f)

print(f"Total tickers: {len(current)}, New today: {len(new_today)}")
if new_today:
    print(f"NEW: {', '.join(sorted(new_today)[:30])}")
