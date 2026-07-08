#!/usr/bin/env python3
"""
finviz_scan.py — Finviz Elite screener for swing trading setups.

Strategies:
  minervini  — Trend Template + RS + tight consolidation candidates
  canslim    — EPS/Sales growth leaders near pivot points
  reversion  — Oversold quality stocks for mean-reversion bounces
  custom     — Pass your own Finviz filter string via --filters

AUTH — используй auth token из Finviz Elite:
  Найти токен: finviz.com -> Account -> API Token (UUID)

Usage:
  python finviz_scan.py --strategy minervini --auth YOUR-UUID-TOKEN
  python finviz_scan.py --strategy canslim   --auth YOUR-UUID-TOKEN
  python finviz_scan.py --strategy reversion --auth YOUR-UUID-TOKEN --max 50
  python finviz_scan.py --tickers NVDA,UBER,ZETA --auth YOUR-UUID-TOKEN

Full pipeline (Finviz screen -> TradingView enrich -> HTML dashboard):
  python finviz_scan.py --strategy minervini --auth TOKEN | python tv_enrich.py --html --output setup.html
"""

import argparse
import json
import sys
import time
from datetime import datetime

import requests

# ── Filter presets ─────────────────────────────────────────────────────────────

FILTERS = {
    "minervini": (
        # ── Trend structure ──────────────────────────────────────────────────
        "ta_sma200_pa,"     # Price > SMA200
        "ta_sma150_pa,"     # Price > SMA150
        "ta_sma50_pa,"      # Price > SMA50
        "ta_sma50_pa200,"   # SMA50 > SMA200 (Golden Cross — true uptrend)
        # ── 52-week position ─────────────────────────────────────────────────
        "ta_highlow52w_b0to25h,"  # Within 25% of 52W High (upper quartile)
        "ta_perf_52w_o30,"        # 30%+ above 52W Low (+30% off the bottom)
        # ── Momentum / breakout day ──────────────────────────────────────────
        "ta_perf_d_o2,"     # Today +2% or more (active breakout day)
        "sh_relvol_o1p5,"   # Relative volume > 1.5× (volume confirms breakout)
        "ta_rsi_ob60,"      # RSI > 60 (top 30% relative strength proxy)
        # ── Fundamentals ─────────────────────────────────────────────────────
        "fa_epsqoq_o20,"    # EPS growth QoQ > 20% (earnings acceleration)
        # ── Liquidity / price floor ───────────────────────────────────────────
        "cap_smallover,"    # Market cap > $300M
        "sh_avgvol_o300,"   # Avg volume > 300K
        "sh_price_o10"      # Price > $10
    ),
    "canslim": (
        "fa_epsqoq_o25,"    # EPS growth QoQ >= 25%
        "fa_epsyoy_o25,"    # EPS growth YoY >= 25%
        "fa_salesqoq_o20,"  # Sales growth QoQ >= 20%
        "fa_roe_o15,"       # ROE >= 15%
        "ta_highlow52w_b0to15h,"  # Within 15% of 52W High
        "ta_sma200_pa,"
        "ta_sma50_pa,"
        "cap_smallover,"
        "sh_avgvol_o500,"
        "sh_price_o15"
    ),
    "reversion": (
        "ta_rsi_os35,"      # RSI < 35 (oversold)
        "ta_sma200_pa,"     # Still above 200 MA (quality filter)
        "ta_highlow52w_b15to40h,"  # 15-40% off highs
        "cap_midover,"      # Market cap > $2B
        "sh_avgvol_o500,"
        "sh_price_o10"
    ),
}

BASE_URL = "https://elite.finviz.com/export.ashx"


def get_session(auth: str) -> requests.Session:
    """Build a Finviz Elite session using auth token."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/csv,text/plain,*/*",
    })
    session.params = {"auth": auth}
    return session


def parse_csv(text: str) -> list:
    """Parse Finviz CSV correctly (handles quoted fields with commas)."""
    import csv, io, sys
    reader = csv.DictReader(io.StringIO(text))
    rows = [row for row in reader]
    if rows:
        print(f"[finviz] CSV columns: {list(rows[0].keys())}", file=sys.stderr)
    return rows


def run_screen(session: requests.Session, filter_str: str, max_results: int = 200) -> list:
    """Fetch screener results. export.ashx returns all matches in one CSV — no pagination needed."""
    params = {
        "f": filter_str,
        "o": "-relativevolume",  # Sort: highest relative volume first
        "v": "152",              # Financial view — includes EPS Q/Q, Sales Q/Q, ROE, etc.
    }
    try:
        resp = session.get(BASE_URL, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return []

    if resp.status_code == 401:
        print("AUTH ERROR: token invalid or expired.", file=sys.stderr)
        return []

    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return []

    text = resp.text.strip()
    if not text or "\n" not in text:
        print("  Empty response from Finviz", file=sys.stderr)
        return []

    rows = parse_csv(text)
    print(f"  Finviz returned {len(rows)} total matches", file=sys.stderr)

    # Truncate to max_results (highest rel-volume first, already sorted)
    if len(rows) > max_results:
        print(f"  Trimming to top {max_results} by relative volume", file=sys.stderr)
        rows = rows[:max_results]

    return rows


def analyze_specific_tickers(session: requests.Session, tickers: list) -> list:
    """Get Finviz data for specific tickers in one request."""
    params = {"t": ",".join(tickers)}
    try:
        resp = session.get(BASE_URL, params=params, timeout=15)
        if resp.status_code == 401:
            print("AUTH ERROR: token invalid or expired.", file=sys.stderr)
            return []
        if resp.status_code == 200 and resp.text.strip():
            return parse_csv(resp.text)
    except requests.RequestException as e:
        print(f"Request failed: {e}", file=sys.stderr)
    return []


def format_output(raw: list, strategy: str) -> dict:
    """Clean and structure the output."""
    tickers = []
    for row in raw:
        ticker = row.get("Ticker", "").strip()
        if not ticker or ticker.isdigit():
            continue
        tickers.append({
            "ticker": ticker,
            "company": row.get("Company", "").strip(),
            "sector": row.get("Sector", "").strip(),
            "industry": row.get("Industry", "").strip(),
            "market_cap": row.get("Market Cap", "").strip(),
            "pe": row.get("P/E", "").strip(),
            "eps_ttm": row.get("EPS (ttm)", "").strip(),
            "eps_this_y": row.get("EPS Growth This Year", "").strip(),
            "eps_qoq": row.get("EPS Growth Quarter Over Quarter", "").strip(),
            "sales_qoq": row.get("Sales Growth Quarter Over Quarter", "").strip(),
            "rsi": row.get("RSI (14)", "").strip(),
            "rel_volume": row.get("Rel Volume", "").strip(),
            "high_52w": row.get("52W High", "").strip(),
            "low_52w": row.get("52W Low", "").strip(),
        })

    return {
        "scan_time": datetime.now().isoformat(),
        "strategy": strategy,
        "count": len(tickers),
        "tickers": tickers,
    }


def main():
    parser = argparse.ArgumentParser(description="Finviz Elite swing trading scanner")
    parser.add_argument("--auth", required=False, default=None,
                        help="Finviz Elite API token. Can also set FINVIZ_TOKEN env variable.")
    parser.add_argument("--strategy", choices=["minervini", "canslim", "reversion", "custom"],
                        default="minervini")
    parser.add_argument("--filters", help="Custom Finviz filter string (with --strategy custom)")
    parser.add_argument("--tickers", help="Comma-separated specific tickers (skips screener)")
    parser.add_argument("--max", type=int, default=100, help="Max screener results (default: 100)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    import os as _os
    _token = args.auth or _os.environ.get("FINVIZ_TOKEN", "")
    if not _token:
        print("ERROR: Finviz token required (--auth or FINVIZ_TOKEN env)", file=__import__("sys").stderr)
        __import__("sys").exit(1)
    session = get_session(auth=_token)

    if args.tickers:
        ticker_list = [t.strip().upper() for t in args.tickers.split(",")]
        print(f"Analyzing {len(ticker_list)} tickers: {', '.join(ticker_list)}", file=sys.stderr)
        raw = analyze_specific_tickers(session, ticker_list)
    else:
        filter_str = args.filters if args.strategy == "custom" else FILTERS[args.strategy]
        print(f"Running {args.strategy.upper()} screen...", file=sys.stderr)
        raw = run_screen(session, filter_str, max_results=args.max)

    output = format_output(raw, args.strategy)
    print(f"Found {output['count']} candidates", file=sys.stderr)

    indent = 2 if args.pretty else None
    print(json.dumps(output, indent=indent, ensure_ascii=False))


if __name__ == "__main__":
    main()
