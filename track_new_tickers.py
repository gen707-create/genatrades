#!/usr/bin/env python3
"""
track_new_tickers.py — Compare current scan with the PREVIOUS scan.
Writes /tmp/new_tickers.json with tickers that are NEW this scan
(not present in the previous scan).

Logic:
  - prev_tickers.json stores the LAST scan's ticker list
  - NEW = in current scan but NOT in previous scan
  - Any ticker that dropped out and came back is also flagged NEW

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

# Load previous scan's tickers
prev_set = set()
if os.path.exists(prev_file):
    try:
        with open(prev_file) as f:
            data = json.load(f)
        # Support both formats: legacy {ticker: date} dict or new {"tickers": [...]}
        if isinstance(data, list):
            prev_set = set(data)
        elif isinstance(data, dict):
            # New format: {"tickers": [...], "date": "..."}
            if "tickers" in data:
                prev_set = set(data["tickers"])
            else:
                # Legacy format: {ticker: first_seen_date}
                prev_set = set(data.keys())
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

# NEW = present in current scan but absent in previous scan
new_today = [t for t in current if t not in prev_set]

# Save current tickers as the new "previous" for next run
with open(prev_file, "w") as f:
    json.dump({"tickers": current, "date": today}, f, indent=2)

# Save new tickers list for tv_enrich.py
with open(out_file, "w") as f:
    json.dump(new_today, f)

print(f"Total tickers: {len(current)}, Previous: {len(prev_set)}, NEW this scan: {len(new_today)}")
if new_today:
    print(f"NEW: {', '.join(sorted(new_today)[:30])}")
else:
    print("No new tickers vs previous scan.")
