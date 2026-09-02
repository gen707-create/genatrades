#!/usr/bin/env python3
"""
track_new_tickers.py — Track when each ticker entered each strategy.

Two files are maintained:

  prev_tickers.json   {ticker: "YYYY-MM-DD"} — first time a ticker was ever
                      seen in any scan. Drives the NEW badge.

  ticker_history.json {ticker: {strategy: {"first","last","streak"}}} — per
                      strategy dates. Drives the "Seen" column.

Why per strategy: DELL sat in the scanner since July 8 via Minervini, but only
gapped into Swing Gap yesterday. A single global date showed "Jul 8" on the
Swing Gap row, which says nothing about when that setup actually appeared.

`streak` is the start of the CURRENT unbroken run in that strategy. If a ticker
drops out and comes back weeks later, the setup is new again and the date
resets. A gap of up to 4 days is tolerated so weekends and holidays do not
reset an otherwise continuous run.

Usage:
  python track_new_tickers.py scan1.json scan2.json ...
"""
import json
import os
import sys
from datetime import date, timedelta

today = date.today()
today_s = today.isoformat()
scan_files = sys.argv[1:] if len(sys.argv) > 1 else []
prev_file = "prev_tickers.json"
hist_file = "ticker_history.json"
out_file = "/tmp/new_tickers.json"

# Longest absence still treated as continuous presence (Fri -> Mon = 3 days).
MAX_GAP_DAYS = 4


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning reading {path}: {e}", file=sys.stderr)
        return default


# ── global first-seen history (NEW badge) ────────────────────────────────────
history = {}
data = load_json(prev_file, {})
if isinstance(data, dict):
    if "tickers" in data:
        seen_date = data.get("date", "1970-01-01")
        for t in data["tickers"]:
            history[t] = seen_date
    else:
        history = data
elif isinstance(data, list):
    yesterday = (today - timedelta(days=1)).isoformat()
    for t in data:
        history[t] = yesterday

# ── per-strategy history (Seen column) ───────────────────────────────────────
per_strat = load_json(hist_file, {})
if not isinstance(per_strat, dict):
    per_strat = {}

# ── collect current tickers, keeping their strategy ──────────────────────────
current = []
current_set = set()
by_strategy = {}

for fn in scan_files:
    d = load_json(fn, {})
    strat = d.get("strategy", "unknown")
    for t in d.get("tickers", []):
        ticker = t["ticker"] if isinstance(t, dict) else str(t)
        if not ticker:
            continue
        by_strategy.setdefault(strat, set()).add(ticker)
        if ticker not in current_set:
            current.append(ticker)
            current_set.add(ticker)

for t in current:
    if t not in history:
        history[t] = today_s

# ── update per-strategy records ──────────────────────────────────────────────
resumed = 0
for strat, tickers in by_strategy.items():
    for t in tickers:
        rec = per_strat.setdefault(t, {}).get(strat)
        if not rec:
            per_strat[t][strat] = {"first": today_s, "last": today_s, "streak": today_s}
            continue
        streak = rec.get("streak") or rec.get("first") or today_s
        last = rec.get("last")
        if last:
            try:
                gap = (today - date.fromisoformat(last)).days
                if gap > MAX_GAP_DAYS:
                    streak = today_s      # setup went away and came back — reset
                    resumed += 1
            except ValueError:
                pass
        rec["last"] = today_s
        rec["streak"] = streak
        rec.setdefault("first", today_s)
        per_strat[t][strat] = rec

with open(prev_file, "w") as f:
    json.dump(history, f, indent=2, sort_keys=True)
with open(hist_file, "w") as f:
    json.dump(per_strat, f, indent=2, sort_keys=True)

new_today = [t for t in current if history.get(t) == today_s]
with open(out_file, "w") as f:
    json.dump(new_today, f)

print(f"Total: {len(current)}, History: {len(history)}, NEW today: {len(new_today)}")
print(f"Per-strategy records: {len(per_strat)} tickers across "
      f"{len(by_strategy)} strategies ({', '.join(sorted(by_strategy))})")
if resumed:
    print(f"Streak reset for {resumed} ticker/strategy pairs (returned after a gap)")
if new_today:
    print(f"NEW: {', '.join(sorted(new_today)[:30])}")
else:
    print("No new tickers today.")
