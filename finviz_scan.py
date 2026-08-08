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
        "ta_sma50_pa,"      # Price > SMA50
        "ta_sma20_pa,"      # Price > SMA20 (short-term trend confirmation)
        "ta_sma50_pa200,"   # SMA50 > SMA200 (Golden Cross — valid Elite filter)
        # ── 52-week position ─────────────────────────────────────────────────
        "ta_highlow52w_b0to25h,"  # Within 25% of 52W High — near highs but not at the absolute top
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

    # ── Early Base Breakout / Pocket Pivot ────────────────────────────────────
    # Catches stocks at the BEGINNING of a move out of base consolidation.
    # Signal: strong candle + volume surge right as price crosses SMA50 —
    # before the stock gets extended. Finviz casts a broad net; Python
    # post-filter (apply_base_breakout_postfilter) refines proximity to SMA50.
    "base_breakout": (
        # Uptrend structure — all three MAs aligned
        "ta_sma200_pa,"        # Price > SMA200
        "ta_sma50_pa,"         # Price just crossed above SMA50 (breakout signal)
        "ta_sma50_pa200,"      # SMA50 > SMA200 (trend healthy)
        # Pocket pivot volume — institutional buying
        "sh_relvol_o1p5,"      # Relative volume > 1.5×
        # Early in move — within 25% of 52W high (near highs, not extended)
        "ta_highlow52w_b0to25h,"  # Within 25% of 52W High
        # Liquidity
        "cap_smallover,"       # Market cap > $300M
        "sh_avgvol_o200,"      # Avg vol > 200K
        "sh_price_o10"         # Price > $10
    ),

    # ── Day Trading — "Trend Join Long" ──────────────────────────────────────
    # Premarket criteria: gap > 3%, price > $3, mcap > $1B, RVOL > 1.5.
    # Finviz filters are broad (confirmed-valid codes only); Python post-filter
    # enforces exact thresholds: change > 3%, price > $3, mcap > $1B.
    "day_trading": (
        "ta_perf_d_o2,"        # Day change > 2% (broad net; post-filter tightens to 3%)
        "sh_relvol_o1p5,"      # Relative volume > 1.5x (confirmed working)
        "cap_smallover,"       # Market cap > $300M (post-filtered to $1B in Python)
        "sh_avgvol_o300"       # Avg volume > 300K — intraday liquidity floor
    ),

    # ── Swing Gap ────────────────────────────────────────────────────────────
    # Premarket criteria: gap >= 8%, price > $3, open > 200 SMA, mcap >= $800M.
    # Python post-filter enforces exact thresholds: change >= 8%, price > $3, mcap >= $800M.
    "swing_gap": (
        "ta_perf_d_o2,"        # Day change > 2% (broad net; post-filter tightens to 8%)
        "ta_sma200_pa,"        # Price > 200-day SMA (confirmed working)
        "cap_smallover"        # Market cap > $300M (post-filtered to $800M in Python)
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


def run_screen(session: requests.Session, filter_str: str, max_results: int = 200,
               sort: str = "-relativevolume") -> list:
    """Fetch screener results. export.ashx returns all matches in one CSV — no pagination needed.

    `sort` controls the Finviz ordering, which matters because results are trimmed to
    max_results BEFORE the Python post-filter runs. Strategies whose defining criterion is
    the size of the daily move must sort by '-change', or high-gap names outside the top
    RVOL slice get discarded before they are ever evaluated.
    """
    params = {
        "f": filter_str,
        "o": sort,
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

    # Truncate to max_results (Finviz already returned them in `sort` order)
    if len(rows) > max_results:
        print(f"  Trimming to top {max_results} by {sort}", file=sys.stderr)
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
            "rel_volume": (row.get("Rel Volume", "")
                           or row.get("Relative Volume", "")).strip(),
            "high_52w": row.get("52W High", "").strip(),
            "low_52w": row.get("52W Low", "").strip(),
            "price": row.get("Price", "").strip(),
            "change": row.get("Change", "").strip(),
            "sma50": row.get("SMA50", "").strip(),
            "sma200": row.get("SMA200", "").strip(),
        })

    return {
        "scan_time": datetime.now().isoformat(),
        "strategy": strategy,
        "count": len(tickers),
        "tickers": tickers,
    }


def _parse_mcap(s: str) -> float:
    """Parse Finviz market cap string to float in USD.

    Handles both formatted ('131.24B', '456.78M') and raw-millions format
    ('131240' = $131.24B) — Finviz screener CSV sometimes exports the latter.
    """
    if not s or s in ("-", "N/A", ""):
        return 0.0
    s = s.replace("$", "").replace(",", "").strip()
    try:
        if s.endswith("T"):
            return float(s[:-1]) * 1e12
        elif s.endswith("B"):
            return float(s[:-1]) * 1e9
        elif s.endswith("M"):
            return float(s[:-1]) * 1e6
        elif s.endswith("K"):
            return float(s[:-1]) * 1e3
        # No suffix: Finviz may return raw millions (e.g. "131240" = $131.24B).
        # Screener cap_smallover guarantees values >= 300 (i.e. $300M+),
        # so anything < 1e7 is safely treated as millions.
        v = float(s)
        return v * 1e6 if 0 < v < 1e7 else v
    except ValueError:
        return 0.0


def _parse_pct(s: str) -> float:
    """Parse Finviz percent string like '+17.44%', '-2.30%' to float."""
    if not s or s in ("-", "N/A", ""):
        return 0.0
    try:
        return float(s.replace("%", "").replace("+", "").strip())
    except ValueError:
        return 0.0


def _parse_price(s: str) -> float:
    """Parse Finviz price string like '$178.07' to float."""
    if not s or s in ("-", "N/A", ""):
        return 0.0
    try:
        return float(s.replace("$", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def apply_day_trading_postfilter(rows: list) -> list:
    """Post-filter: day change > 3%, price > $3, market cap > $1B."""
    kept = []
    for r in rows:
        if (_parse_pct(r.get("change", "")) > 3.0
                and _parse_price(r.get("price", "")) > 3.0
                and _parse_mcap(r.get("market_cap", "")) >= 1e9):
            kept.append(r)
    print(f"  [day_trading] Post-filter (chg>3%, px>$3, mcap>$1B): {len(rows)} -> {len(kept)}", file=sys.stderr)
    return kept


def apply_swing_gap_postfilter(rows: list) -> list:
    """Post-filter: day change >= 8%, price > $3, market cap >= $800M."""
    kept = []
    for r in rows:
        if (_parse_pct(r.get("change", "")) >= 8.0
                and _parse_price(r.get("price", "")) > 3.0
                and _parse_mcap(r.get("market_cap", "")) >= 8e8):
            kept.append(r)
    print(f"  [swing_gap] Post-filter (chg>=8%, px>$3, mcap>=$800M): {len(rows)} -> {len(kept)}", file=sys.stderr)
    return kept


def apply_base_breakout_postfilter(rows: list, max_sma50_pct: float = 0.20) -> list:
    """
    Post-filter for base_breakout strategy.
    When SMA50 data is available: keeps stocks 0-20% above SMA50 (early breakout zone).
    When SMA50 is missing (not in CSV view): passes all rows through (Finviz filters already
    guarantee price > SMA50 via ta_sma50_pa).
    """
    kept = []
    for r in rows:
        try:
            price  = float(r.get("price", "") or 0)
            sma50  = float(r.get("sma50", "") or 0)
            sma200 = float(r.get("sma200", "") or 0)
            chg_s  = (r.get("change", "") or "0").replace("%","").replace("+","")
            chg    = float(chg_s)
        except (ValueError, TypeError):
            continue

        if price <= 0:
            continue

        # Daily change must be a CONTROLLED breakout (+0.3% to +8%)
        # Excludes gap-up explosions (+49%, +28%) which are already extended
        if not (0.3 <= chg <= 8.0):
            continue

        # SMA50 proximity — only filter if data is available in CSV
        if sma50 > 0:
            pct_above_sma50 = (price - sma50) / sma50
            if not (0.0 <= pct_above_sma50 <= max_sma50_pct):
                continue
            if sma200 > 0 and sma50 < sma200:
                continue
            r["sma50_pct"] = "%.1f" % (pct_above_sma50 * 100)

        kept.append(r)

    print("  [base_breakout] Post-filter: %d -> %d" % (len(rows), len(kept)),
          file=__import__("sys").stderr)
    return kept


def main():
    parser = argparse.ArgumentParser(description="Finviz Elite swing trading scanner")
    parser.add_argument("--auth", required=False, default=None,
                        help="Finviz Elite API token. Can also set FINVIZ_TOKEN env variable.")
    parser.add_argument("--strategy", choices=["minervini", "canslim", "reversion", "base_breakout",
                                               "day_trading", "swing_gap", "custom"],
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
        # Gap-driven strategies must rank by size of the daily move, not RVOL —
        # the trim to max_results happens before the Python post-filter.
        sort_by = "-change" if args.strategy in ("day_trading", "swing_gap") else "-relativevolume"
        print(f"Running {args.strategy.upper()} screen (sort={sort_by})...", file=sys.stderr)
        raw = run_screen(session, filter_str, max_results=args.max, sort=sort_by)

    output = format_output(raw, args.strategy)

    # post-filters by strategy
    if args.strategy == "base_breakout" and output["tickers"]:
        output["tickers"] = apply_base_breakout_postfilter(output["tickers"])
        output["count"] = len(output["tickers"])
    elif args.strategy in ("day_trading", "swing_gap"):
        # Diagnostic: show first raw row so we can verify field formats in CI logs
        if output["tickers"]:
            s0 = output["tickers"][0]
            print(
                f"  [{args.strategy}] Pre-filter count={len(output['tickers'])}  "
                f"sample: change={s0.get('change')!r} "
                f"price={s0.get('price')!r} "
                f"market_cap={s0.get('market_cap')!r}",
                file=sys.stderr,
            )
        else:
            print(f"  [{args.strategy}] Finviz returned 0 rows (empty scan)", file=sys.stderr)
        if args.strategy == "day_trading" and output["tickers"]:
            output["tickers"] = apply_day_trading_postfilter(output["tickers"])
            output["count"] = len(output["tickers"])
        elif args.strategy == "swing_gap" and output["tickers"]:
            output["tickers"] = apply_swing_gap_postfilter(output["tickers"])
            output["count"] = len(output["tickers"])

    print(f"Found {output['count']} candidates", file=sys.stderr)

    indent = 2 if args.pretty else None
    print(json.dumps(output, indent=indent, ensure_ascii=False))


if __name__ == "__main__":
    main()
