#!/usr/bin/env python3
"""
tv_enrich.py — TradingView data enrichment for swing trading setups.

Pulls live technical indicator values from TradingView for a list of tickers
and scores each stock against Minervini Trend Template, CANSLIM technical
requirements, and mean-reversion criteria.

Reads ticker list from:
  - stdin (JSON from finviz_scan.py pipe)
  - --tickers argument
  - --file argument (plain text file, one ticker per line)

Usage:
  # Full pipeline: Finviz screen → TradingView enrichment
  python finviz_scan.py --strategy minervini --cookie "..." | python tv_enrich.py

  # Enrich specific tickers
  python tv_enrich.py --tickers NVDA,TSLA,AAPL,META

  # From a file
  python tv_enrich.py --file watchlist.txt --strategy canslim

  # Output as HTML dashboard (saved to workspace folder)
  python tv_enrich.py --tickers NVDA,META --html --output "D:/Claude Projects/INVEST Stocks/Invest Stocks Portfolio/watchlist_2026-05-19.html"

Requirements:
  pip install tradingview-screener requests pandas

TradingView works without authentication. Data is ~1-2 min delayed for free,
near real-time for TradingView Pro/Pro+ subscribers (session cookie optional).
"""

import argparse
import json
import math
import sys
import requests
from datetime import datetime
from pathlib import Path


def install_deps():
    """Auto-install required packages if missing."""
    import subprocess
    packages = ["tradingview-screener", "pandas", "yfinance"]
    for pkg in packages:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            print(f"📦 Installing {pkg}...", file=sys.stderr)
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


install_deps()

from tradingview_screener import Query, col  # noqa: E402
import pandas as pd  # noqa: E402


# ── TradingView field mapping ─────────────────────────────────────────────────

TV_COLUMNS = [
    "close",                    # Current price
    "open",
    "high",
    "low",
    "volume",
    "relative_volume_10d_calc", # Relative volume vs 10-day avg
    "SMA20",
    "SMA50",
    "SMA150",
    "SMA200",
    "RSI",                      # RSI 14
    "RSI[1]",                   # RSI previous bar (for divergence check)
    "BB.upper",                 # Bollinger Band upper (20, 2σ)
    "BB.lower",                 # Bollinger Band lower
    "BB.basis",                 # Bollinger Band middle (SMA20)
    "MACD.macd",
    "MACD.signal",
    "ADX",
    "High.All",                 # 52-week high
    "Low.All",                  # 52-week low
    "change",                   # % change today
    "change_abs",
    "Perf.W",                   # 1-week performance
    "Perf.1M",                  # 1-month performance
    "Perf.3M",                  # 3-month performance
    "Perf.6M",                  # 6-month performance
    "Perf.Y",                   # 1-year performance
    "average_volume_10d_calc",  # 10-day avg volume
    "average_volume_30d_calc",  # 30-day avg volume
    "market_cap_basic",
    "float_shares_outstanding",
    "sector",
    "description",
]


def fetch_tv_data(tickers, exchange="america"):
    """Pull indicator data from TradingView for given tickers."""
    print(f"📡 Fetching TradingView data for {len(tickers)} tickers...", file=sys.stderr)

    try:
        _, df = (
            Query()
            .select(*TV_COLUMNS)
            .where(col("name").isin(tickers))
            .set_markets(exchange)
            .limit(len(tickers) + 10)
            .get_scanner_data()
        )
        # v3.x may return ticker as index — normalise to column
        if "name" not in df.columns:
            if df.index.name == "name" or df.index.name is None:
                df = df.reset_index()
                if "index" in df.columns:
                    df.rename(columns={"index": "name"}, inplace=True)
        print(f"  → {len(df)} rows, columns: {list(df.columns[:6])}", file=sys.stderr)
        return df
    except Exception as e:
        print(f"⚠  TradingView query failed: {e}", file=sys.stderr)
        return pd.DataFrame()


def fetch_global_tv_data(tickers):
    """Try multiple exchanges for global market coverage."""
    exchanges = ["america", "euronext", "lse", "xetr", "tokyo", "hongkong"]
    frames = []
    found = set()

    remaining = tickers[:]
    for exchange in exchanges:
        if not remaining:
            break
        _, df = (
            Query()
            .select(*TV_COLUMNS)
            .where(col("name").isin(remaining))
            .set_markets(exchange)
            .limit(len(remaining) + 5)
            .get_scanner_data()
        )
        if not df.empty:
            frames.append(df)
            found.update(df["name"].tolist())
            remaining = [t for t in remaining if t not in found]

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ── Market context (Phase 1 additions) ───────────────────────────────────────

SECTOR_ETFS = {
    "XLK": "Technology", "XLV": "Healthcare", "XLF": "Financials",
    "XLE": "Energy", "XLY": "Cons. Discret.", "XLP": "Cons. Staples",
    "XLI": "Industrials", "XLB": "Materials", "XLRE": "Real Estate",
    "XLU": "Utilities", "XLC": "Comm. Services",
}
MARKET_TICKERS = ["SPY", "QQQ", "IWM"]
COMMODITY_TICKERS = {"GC=F": "Gold", "SI=F": "Silver", "CL=F": "Oil", "BTC-USD": "BTC"}


def detect_chart_patterns(df):
    """
    Detect VCP / Cup+Handle / Flat Base from weekly OHLCV DataFrame.
    Returns best pattern dict or None.
    Fields: pattern, score(0-100), weeks, depth_pct, contractions,
            last_contraction_pct, vol_dry, buy_point, volatility_score, details
    """
    try:
        if df is None or len(df) < 5:
            return None
        for _c in ('High', 'Low', 'Close', 'Volume'):
            if _c not in df.columns:
                return None

        H = df['High'].values.astype(float)
        L = df['Low'].values.astype(float)
        C = df['Close'].values.astype(float)
        V = df['Volume'].values.astype(float)
        n = len(C)
        candidates = []

        # ── Volatility contraction score (recent 4w ATR vs prior 4w) ────────
        def _vstab(h, l, c, w=4):
            if len(c) < w * 2:
                return 50
            atr_r = ((h[-w:] - l[-w:]) / c[-w:]).mean() * 100
            atr_o = ((h[-w*2:-w] - l[-w*2:-w]) / c[-w*2:-w]).mean() * 100
            if atr_o == 0:
                return 50
            return int(max(0, min(100, (1 - atr_r / atr_o) * 100 + 50)))

        vstab = _vstab(H, L, C)

        # ── FLAT BASE ────────────────────────────────────────────────────────
        for wlen in range(5, min(14, n + 1)):
            sH = H[-wlen:]; sL = L[-wlen:]; sV = V[-wlen:]
            rng = (max(sH) - min(sL)) / max(sH) * 100
            if rng <= 15:
                vd  = bool(sV[-2:].mean() < sV[:2].mean()) if len(sV) >= 4 else True
                bpt = round(max(sH) * 1.005, 2)
                sc  = int(50 + (15 - rng) * 3 + (10 if vd else 0) + min(10, (wlen - 5) * 2))
                candidates.append({
                    "pattern": "FLAT BASE", "score": min(90, sc),
                    "weeks": wlen, "depth_pct": round(rng, 1),
                    "contractions": 1, "last_contraction_pct": round(rng, 1),
                    "vol_dry": vd, "buy_point": bpt,
                    "volatility_score": vstab,
                    "details": "%dw · range %.1f%%" % (wlen, rng)
                })
                break

        # ── VCP ──────────────────────────────────────────────────────────────
        seg   = df.tail(26) if n >= 26 else df
        dH    = seg['High'].values.astype(float)
        dL    = seg['Low'].values.astype(float)
        dC    = seg['Close'].values.astype(float)
        dV    = seg['Volume'].values.astype(float)
        dn    = len(dH)

        # Pivot highs / lows (window = 2 bars)
        ph, pl = [], []
        for i in range(2, dn - 2):
            if dH[i] >= max(dH[i-2:i]) and dH[i] >= max(dH[i+1:i+3]):
                ph.append((i, dH[i]))
            if dL[i] <= min(dL[i-2:i]) and dL[i] <= min(dL[i+1:i+3]):
                pl.append((i, dL[i]))

        conts = []
        for hi_i, hi_v in ph:
            nxt = [(li, lv) for li, lv in pl if li > hi_i]
            if nxt:
                li, lv = nxt[0]
                conts.append(dict(
                    hi_i=hi_i, hi=hi_v, lo_i=li, lo=lv,
                    amp=(hi_v - lv) / hi_v * 100,
                    vol=dV[hi_i:li + 1].mean()
                ))

        if len(conts) >= 2:
            is_vcp = all(conts[i]['amp'] < conts[i-1]['amp'] * 0.95
                         for i in range(1, len(conts)))
            if is_vcp:
                nc     = len(conts)
                ratios = [conts[i]['amp'] / conts[i-1]['amp'] for i in range(1, nc)]
                ar     = sum(ratios) / len(ratios)
                vd     = conts[-1]['vol'] < conts[0]['vol']
                la     = conts[-1]['amp']
                rhi    = max(dH[-6:]) if dn >= 6 else max(dH)
                prox   = dC[-1] / rhi * 100

                sc = (min(25, nc * 8)
                      + (int(20 * (1 - abs(ar - 0.5) * 2)) if ar < 1 else 0)
                      + (15 if vd else 0)
                      + int(20 * max(0, 1 - la / 15))
                      + (int(20 * (prox - 80) / 20) if prox > 80 else 0))

                if sc >= 25:
                    bw = conts[-1]['lo_i'] - conts[0]['hi_i']
                    candidates.append({
                        "pattern": "VCP", "score": min(95, sc),
                        "weeks": bw, "depth_pct": round(conts[0]['amp'], 1),
                        "contractions": nc,
                        "last_contraction_pct": round(la, 1),
                        "vol_dry": vd, "buy_point": round(conts[-1]['hi'] * 1.005, 2),
                        "volatility_score": vstab,
                        "details": "%dT · last %.1f%% · ratio %.2f" % (nc, la, ar)
                    })

        # ── CUP WITH HANDLE ──────────────────────────────────────────────────
        if n >= 7:
            seg2 = df.tail(65) if n >= 65 else df
            sH2  = seg2['High'].values.astype(float)
            sL2  = seg2['Low'].values.astype(float)
            sC2  = seg2['Close'].values.astype(float)
            sV2  = seg2['Volume'].values.astype(float)
            sn   = len(sC2)
            half = sn // 2

            lhi_i = int(sH2[:half].argmax())
            lhi   = sH2[lhi_i]
            bot_a = sL2[lhi_i:]
            if len(bot_a) > 2:
                bot_i = lhi_i + int(bot_a.argmin())
                bot_v = sL2[bot_i]
                depth = (lhi - bot_v) / lhi * 100
                if 10 <= depth <= 35:
                    rhi2 = max(sH2[bot_i:]) if len(sH2[bot_i:]) > 0 else 0
                    rec  = (rhi2 - bot_v) / (lhi - bot_v) if (lhi - bot_v) > 0 else 0
                    if rec >= 0.7:
                        hlen = min(4, sn - bot_i - 1)
                        if hlen >= 1:
                            hH   = max(sH2[-hlen:])
                            hL   = min(sL2[-hlen:])
                            hdep = (hH - hL) / hH * 100
                            hvd  = sV2[-hlen:].mean() < sV2[bot_i:bot_i+hlen].mean()                                    if bot_i + hlen < sn else True
                            if hdep <= 15:
                                sc2 = (55 + int((35 - depth) * 0.5)
                                       + (10 if 20 <= depth <= 30 else 0)
                                       + (10 if hdep <= 6 else 5 if hdep <= 10 else 0)
                                       + (8 if hvd else 0)
                                       + (7 if 12 <= sn <= 52 else 0))
                                candidates.append({
                                    "pattern": "CUP+HANDLE", "score": min(93, sc2),
                                    "weeks": sn, "depth_pct": round(depth, 1),
                                    "contractions": 1,
                                    "last_contraction_pct": round(hdep, 1),
                                    "vol_dry": hvd,
                                    "buy_point": round(hH * 1.005, 2),
                                    "volatility_score": vstab,
                                    "details": "%dw · depth %.1f%% · handle %.1f%%" % (sn, depth, hdep)
                                })

        if not candidates:
            return None
        return max(candidates, key=lambda x: x["score"])
    except Exception:
        return None


def fetch_market_context():
    """Fetch live prices via fast_info; SMA50 via batch download (90d)."""
    import yfinance as yf
    ctx_tickers = MARKET_TICKERS + ["^VIX"] + list(SECTOR_ETFS.keys()) + list(COMMODITY_TICKERS.keys())
    result = {}

    # Step 1: batch download 90 days for SMA50 only
    sma50_map = {}
    try:
        raw = yf.download(
            ctx_tickers, period="90d", interval="1d",
            progress=False, auto_adjust=True, threads=True, group_by="ticker"
        )
        for sym in ctx_tickers:
            key = sym.replace("^", "")
            try:
                closes = raw["Close"][sym].dropna() if len(ctx_tickers) > 1 else raw["Close"].dropna()
                if len(closes) >= 50:
                    sma50_map[key] = float(closes.tail(50).mean())
            except Exception:
                pass
    except Exception as e:
        print(f"  SMA50 batch failed: {e}", file=sys.stderr)

    # Step 2: fast_info for LIVE price + today change
    for sym in ctx_tickers:
        key = sym.replace("^", "")
        try:
            info   = yf.Ticker(sym).fast_info
            price  = getattr(info, "last_price", None)
            prev   = getattr(info, "previous_close", None)
            change = ((price - prev) / prev * 100) if price and prev else 0.0
            sma50  = sma50_map.get(key)
            result[key] = {
                "price":      round(price, 2) if price else None,
                "change":     round(change, 2),
                "perf_1m":    0.0,
                "above_50ma": (price > sma50) if (price and sma50) else None,
                "label":      COMMODITY_TICKERS.get(sym, key),
            }
        except Exception:
            pass

    print(f"  → {len(result)} market/sector tickers loaded", file=sys.stderr)
    return result


def _news_sentiment(title):
    """Return 1 (positive) / -1 (negative) / 0 (neutral) based on keywords."""
    words = set(title.lower().replace(",","").replace(".","").replace("!","").split())
    pos = len(words & _SENT_POS)
    neg = len(words & _SENT_NEG)
    if pos > neg:   return 1
    if neg > pos:   return -1
    return 0

def _fetch_news_rss(sym):
    """Fetch up to 3 headlines. Tries yfinance first, then Google News RSS."""
    from datetime import datetime
    import urllib.request, xml.etree.ElementTree as ET

    # Method 1: yfinance.Ticker.news
    try:
        import yfinance as yf
        raw = yf.Ticker(sym).news or []
        items = []
        for n in raw[:5]:
            c = n.get("content") or n
            title = (c.get("title") or n.get("title") or "").strip()
            link  = ((c.get("canonicalUrl") or {}).get("url") or
                     (c.get("clickThroughUrl") or {}).get("url") or
                     n.get("link") or "#")
            pub = c.get("pubDate") or n.get("providerPublishTime") or ""
            if isinstance(pub, (int, float)) and pub > 0:
                try: date = datetime.utcfromtimestamp(pub).strftime("%a, %d %b %Y")
                except Exception: date = ""
            else: date = str(pub)[:16]
            if title:
                items.append({"t": title, "u": link, "d": date, "s": _news_sentiment(title)})
            if len(items) >= 3: break
        if items: return items
    except Exception: pass

    # Method 2: Google News RSS
    try:
        url = "https://news.google.com/rss/search?q=%s+stock&hl=en-US&gl=US&ceid=US:en" % sym
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            root = ET.fromstring(resp.read())
        items = []
        for node in root.findall(".//item")[:3]:
            title = (node.findtext("title") or "").strip()
            link  = (node.findtext("link") or "#").strip()
            date  = (node.findtext("pubDate") or "")[:16]
            if title: items.append({"t": title, "u": link, "d": date, "s": _news_sentiment(title)})
        return items
    except Exception: pass

    return []

def fetch_yahoo_data(tickers):
    """Fetch pre/post-market %, earnings, RS Line (3M vs SPY), analyst ratings, news."""
    import yfinance as yf
    import logging, warnings
    from datetime import datetime as _dt
    # Suppress yfinance / urllib3 noise
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", module="yfinance")
    result = {}

    def _calc_atr(df, periods=14):
        """14-period ATR from weekly OHLCV. Returns (atr_val, atr_pct) or (None, None)."""
        try:
            if df is None or len(df) < periods + 1:
                return None, None
            H = df['High'].values.astype(float)
            L = df['Low'].values.astype(float)
            C = df['Close'].values.astype(float)
            tr = []
            for i in range(1, len(C)):
                tr.append(max(H[i] - L[i],
                              abs(H[i] - C[i-1]),
                              abs(L[i] - C[i-1])))
            atr = sum(tr[-periods:]) / periods
            pct = atr / C[-1] * 100 if C[-1] else 0
            return round(atr, 2), round(pct, 2)
        except Exception:
            return None, None

    # ── Batch 6-month weekly OHLCV for pattern detection ─────────────────────
    _weekly_data = {}
    try:
        import io as _io2, contextlib as _cl2
        _buf2 = _io2.StringIO()
        with _cl2.redirect_stderr(_buf2):
            _wdf = yf.download(
                list(tickers), period="6mo", interval="1wk",
                group_by="ticker", auto_adjust=True, progress=False
            )
        if _wdf is not None and not _wdf.empty:
            _tick_list = list(tickers)
            for _sym in _tick_list:
                try:
                    _df_sym = _wdf[_sym] if len(_tick_list) > 1 else _wdf
                    if _df_sym is not None and not _df_sym.empty:
                        _weekly_data[_sym] = _df_sym.dropna(how="all")
                except Exception:
                    pass
    except Exception as _we:
        pass

    # ── Batch 3-month history for RS Line ────────────────────────────────────
    _spy_close = {}
    _sym_close = {}
    try:
        batch_syms = sorted(set(list(tickers) + ["SPY"]))
        _raw = yf.download(batch_syms, period="3mo", interval="1d",
                           auto_adjust=True, progress=False)
        _cl = _raw["Close"] if "Close" in _raw.columns else _raw
        if hasattr(_cl, "columns"):
            for col in _cl.columns:
                col_str = str(col)
                s = _cl[col].dropna()
                d = {str(idx)[:10]: float(v) for idx, v in s.items()}
                if col_str == "SPY":
                    _spy_close = d
                else:
                    _sym_close[col_str] = d
        else:
            s = _cl.dropna()
            k = batch_syms[0]
            _sym_close[k] = {str(i)[:10]: float(v) for i, v in s.items()}
    except Exception as e:
        print(f"  ⚠ batch history: {e}", file=sys.stderr)

    for sym in tickers:
        try:
            t = yf.Ticker(sym)
            info = t.fast_info

            # ── Pre/post market ─────────────────────────────────────────
            def pct(new, base):
                try:
                    return round((new - base) / base * 100, 2) if new and base else None
                except Exception:
                    return None

            reg_close = (getattr(info, "regular_market_previous_close", None)
                         or getattr(info, "previous_close", None))

            # Try fast_info attrs first (yfinance 0.2.x)
            pre_price  = getattr(info, "pre_market_price",  None)
            post_price = getattr(info, "post_market_price", None)

            # Fallback: t.info dict (camelCase keys)
            if pre_price is None or post_price is None:
                try:
                    _full = t.info or {}
                    if pre_price is None:
                        pre_price = (_full.get("preMarketPrice")
                                     or _full.get("pre_market_price"))
                    if post_price is None:
                        post_price = (_full.get("postMarketPrice")
                                      or _full.get("post_market_price"))
                    if reg_close is None:
                        reg_close = (_full.get("regularMarketPreviousClose")
                                     or _full.get("previousClose"))
                except Exception:
                    pass

            # Fallback: ticker history with prepost (silent)
            if pre_price is None and post_price is None:
                try:
                    import io as _io, contextlib as _cl, pandas as _pd
                    _buf = _io.StringIO()
                    with _cl.redirect_stderr(_buf):
                        _hist = t.history(period="1d", interval="1m",
                                          prepost=True, auto_adjust=True)
                    if _hist is not None and not _hist.empty:
                        _now = _pd.Timestamp.now(tz="America/New_York")
                        _mo  = _now.replace(hour=9,  minute=30, second=0, microsecond=0)
                        _mc  = _now.replace(hour=16, minute=0,  second=0, microsecond=0)
                        _idx = _hist.index
                        if _idx.tzinfo is None:
                            _idx = _idx.tz_localize("UTC").tz_convert("America/New_York")
                        else:
                            _idx = _idx.tz_convert("America/New_York")
                        _pre_df  = _hist[_idx < _mo]
                        _post_df = _hist[_idx >= _mc]
                        if not _pre_df.empty:
                            _col = "Close" if "Close" in _pre_df.columns else _pre_df.columns[3]
                            pre_price = float(_pre_df[_col].iloc[-1])
                        if not _post_df.empty:
                            _col = "Close" if "Close" in _post_df.columns else _post_df.columns[3]
                            post_price = float(_post_df[_col].iloc[-1])
                except Exception:
                    pass

            pre_chg  = pct(pre_price,  reg_close)
            post_chg = pct(post_price, reg_close)

            # Earnings date — calendar can be a dict or DataFrame depending on yfinance version
            earnings_date, days_to_earn = None, 999
            try:
                cal = t.calendar
                earn_dt = None

                if isinstance(cal, dict):
                    earn_list = cal.get("Earnings Date") or cal.get("earningsDate")
                    if earn_list:
                        item = earn_list[0] if isinstance(earn_list, (list, tuple)) else earn_list
                        if hasattr(item, "to_pydatetime"):
                            earn_dt = item.to_pydatetime()
                        elif hasattr(item, "date"):
                            earn_dt = _dt.combine(item.date(), _dt.min.time())
                        else:
                            earn_dt = _dt.fromisoformat(str(item)[:10])
                elif cal is not None:
                    try:
                        if not cal.empty:
                            earn_col = [c for c in cal.columns if "Earnings" in str(c)]
                            if earn_col:
                                earn_val = cal[earn_col[0]].iloc[0]
                                if earn_val is not None:
                                    if hasattr(earn_val, "to_pydatetime"):
                                        earn_dt = earn_val.to_pydatetime()
                                    elif hasattr(earn_val, "date"):
                                        earn_dt = _dt.combine(earn_val.date(), _dt.min.time())
                                    else:
                                        earn_dt = _dt.fromisoformat(str(earn_val)[:10])
                    except Exception:
                        pass

                if earn_dt:
                    if hasattr(earn_dt, "tzinfo") and earn_dt.tzinfo:
                        earn_dt = earn_dt.replace(tzinfo=None)
                    now = _dt.now()
                    days_to_earn = (earn_dt - now).days
                    if days_to_earn >= 0:
                        earnings_date = earn_dt.strftime("%b %d")
                    else:
                        earnings_date = None
                        days_to_earn = 999

            except Exception:
                pass

            # ── RS Line (3M normalized vs SPY) ──────────────────────────────
            rs_points = []
            try:
                t_hist = _sym_close.get(sym, {})
                if t_hist and _spy_close:
                    common = sorted(set(t_hist) & set(_spy_close))
                    if len(common) >= 10:
                        rs = [t_hist[d] / _spy_close[d] for d in common]
                        mn, mx = min(rs), max(rs)
                        if mx > mn:
                            rs_points = [round((v - mn) / (mx - mn), 4) for v in rs]
            except Exception:
                pass

            # ── Analyst ratings ──────────────────────────────────────────────
            analyst_target = analyst_rec = analyst_n = None
            try:
                full_info   = t.info
                analyst_target = full_info.get("targetMeanPrice")
                analyst_rec    = (full_info.get("recommendationKey") or "").lower()
                analyst_n      = full_info.get("numberOfAnalystOpinions")
            except Exception:
                pass

            # ── News headlines ───────────────────────────────────────────────
            news_items = _fetch_news_rss(sym)

            # ── Insider transactions (last 30 days) ──────────────────────────
            insider_buys  = []
            insider_sells = []
            try:
                from datetime import timedelta
                _cutoff = _dt.now() - timedelta(days=30)
                _ins_df = t.insider_transactions
                if _ins_df is not None and not _ins_df.empty:
                    for _, _row in _ins_df.iterrows():
                        # Date is in "Start Date" column
                        _d = _row.get("Start Date")
                        if _d is None:
                            continue
                        if hasattr(_d, "to_pydatetime"):
                            _d = _d.to_pydatetime()
                        if hasattr(_d, "tzinfo") and _d.tzinfo:
                            _d = _d.replace(tzinfo=None)
                        if not hasattr(_d, "year") or _d < _cutoff:
                            continue
                        # Transaction type from "Transaction" column (Sale/Purchase/etc.)
                        _txn  = str(_row.get("Transaction") or "").lower()
                        _text = str(_row.get("Text") or "").lower()
                        _name = str(_row.get("Insider") or "")
                        _pos  = str(_row.get("Position") or "")
                        try:
                            _sh = int(float(_row.get("Shares") or 0))
                        except (TypeError, ValueError):
                            _sh = 0
                        _ds = _d.strftime("%b %d") if hasattr(_d, "strftime") else str(_d)[:10]
                        _label = (_name[:22] + " · " + _pos[:15]) if _pos else _name[:30]
                        _entry = {"name": _label, "shares": _sh, "date": _ds}
                        if any(w in _txn for w in ("sale", "sell")):
                            insider_sells.append(_entry)
                        elif any(w in _txn for w in ("purchase", "buy", "acquisition", "gift")):
                            insider_buys.append(_entry)
                        elif any(w in _text for w in ("sale", "sold")):
                            insider_sells.append(_entry)
                        elif any(w in _text for w in ("purchase", "bought")):
                            insider_buys.append(_entry)
            except Exception:
                pass

            # ── Institutional / Hedge Fund holders ───────────────────────────
            institutions = []
            try:
                _inst_df = t.institutional_holders
                if _inst_df is not None and not _inst_df.empty:
                    for _, _row in _inst_df.head(5).iterrows():
                        _hname = str(_row.get("Holder") or "")
                        # pctHeld is stored as fraction (0.0795 = 7.95%)
                        try:
                            _pct = float(_row.get("pctHeld") or 0) * 100
                        except (TypeError, ValueError):
                            _pct = 0.0
                        try:
                            _sh2 = int(float(_row.get("Shares") or 0))
                        except (TypeError, ValueError):
                            _sh2 = 0
                        _dr = _row.get("Date Reported")
                        _dr_s = ""
                        if _dr is not None:
                            if hasattr(_dr, "strftime"):
                                _dr_s = _dr.strftime("%b %Y")
                            else:
                                _dr_s = str(_dr)[:7]
                        # pctChange: fraction → %, positive = bought, negative = sold
                        try:
                            _chg = float(_row.get("pctChange") or 0) * 100
                        except (TypeError, ValueError):
                            _chg = None
                        if _hname:
                            institutions.append({"name": _hname[:35],
                                                  "pct":    round(_pct, 2),
                                                  "change": round(_chg, 1) if _chg is not None else None,
                                                  "shares": _sh2,
                                                  "date":   _dr_s})
            except Exception:
                pass

            result[sym] = {
                "pre_chg":        pre_chg,
                "pre_price":      pre_price,
                "post_chg":       post_chg,
                "post_price":     post_price,
                "earnings_date":  earnings_date,
                "days_to_earn":   days_to_earn,
                "rs_points":      rs_points,
                "atr":            _calc_atr(_weekly_data.get(sym)),
                "analyst_target": analyst_target,
                "analyst_rec":    analyst_rec or "",
                "analyst_n":      analyst_n,
                "news":           news_items,
                "insider_buys":   insider_buys,
                "insider_sells":  insider_sells,
                "institutions":   institutions,
                "chart_pattern":  detect_chart_patterns(_weekly_data.get(sym)),
            }
        except Exception as e:
            print(f"  ⚠ yfinance {sym}: {e}", file=sys.stderr)
    return result


def safe(val, default=None):
    """Return None for NaN/None values."""
    if val is None:
        return default
    try:
        if math.isnan(float(val)):
            return default
    except (TypeError, ValueError):
        pass
    return val


def score_minervini(row):
    """Score stock against Minervini Trend Template. Returns pass/fail per criterion."""
    price  = safe(row.get("close"), 0)
    sma50  = safe(row.get("SMA50"), 0)
    sma150 = safe(row.get("SMA150"), 0)
    sma200 = safe(row.get("SMA200"), 0)
    high52 = safe(row.get("High.All"), 0)
    low52  = safe(row.get("Low.All"), 0)
    rsi    = safe(row.get("RSI"), 50)

    criteria = {}

    criteria["Price > 200 MA"] = {
        "value":    ("$%.2f vs $%.2f" % (price, sma200)) if price and sma200 else "N/A",
        "required": "Price above",
        "pass":     price > sma200 if price and sma200 else False,
    }
    criteria["Price > 150 MA"] = {
        "value":    ("$%.2f vs $%.2f" % (price, sma150)) if price and sma150 else "N/A",
        "required": "Price above",
        "pass":     price > sma150 if price and sma150 else False,
    }
    criteria["Price > 50 MA"] = {
        "value":    ("$%.2f vs $%.2f" % (price, sma50)) if price and sma50 else "N/A",
        "required": "Price above",
        "pass":     price > sma50 if price and sma50 else False,
    }
    criteria["50 MA > 150 MA"] = {
        "value":    ("$%.2f vs $%.2f" % (sma50, sma150)) if sma50 and sma150 else "N/A",
        "required": "50 > 150",
        "pass":     sma50 > sma150 if sma50 and sma150 else False,
    }
    criteria["150 MA > 200 MA"] = {
        "value":    ("$%.2f vs $%.2f" % (sma150, sma200)) if sma150 and sma200 else "N/A",
        "required": "150 > 200",
        "pass":     sma150 > sma200 if sma150 and sma200 else False,
    }

    pct_from_high = ((high52 - price) / high52 * 100) if high52 and price else None
    criteria["Within 25% of 52W High"] = {
        "value":    ("%.1f%% below high" % pct_from_high) if pct_from_high is not None else "N/A",
        "required": "≤ 25% below high",
        "pass":     pct_from_high is not None and pct_from_high <= 25,
        "warn":     pct_from_high is not None and 20 <= pct_from_high <= 25,
    }

    pct_from_low = ((price - low52) / low52 * 100) if low52 and price else None
    criteria["30%+ Above 52W Low"] = {
        "value":    ("%.1f%% above low" % pct_from_low) if pct_from_low is not None else "N/A",
        "required": "≥ 30% above low",
        "pass":     pct_from_low is not None and pct_from_low >= 30,
    }

    criteria["RSI Healthy (45-80)"] = {
        "value":    ("RSI %.1f" % rsi) if rsi else "N/A",
        "required": "45–80",
        "pass":     45 <= rsi <= 80 if rsi else False,
        "warn":     rsi is not None and (rsi > 75 or rsi < 50),
    }

    passed = sum(1 for v in criteria.values() if v.get("pass"))
    total  = len(criteria)
    core_pass = all([
        criteria["Price > 200 MA"]["pass"],
        criteria["Price > 150 MA"]["pass"],
        criteria["Price > 50 MA"]["pass"],
        criteria["50 MA > 150 MA"]["pass"],
        criteria["150 MA > 200 MA"]["pass"],
    ])

    return {
        "criteria":  criteria,
        "passed":    passed,
        "total":     total,
        "core_pass": core_pass,
        "score_pct": round(passed / total * 100),
    }


def score_canslim(row):
    """Score stock against CANSLIM technical requirements."""
    price   = safe(row.get("close"), 0)
    sma50   = safe(row.get("SMA50"), 0)
    sma200  = safe(row.get("SMA200"), 0)
    high52  = safe(row.get("High.All"), 0)
    rsi     = safe(row.get("RSI"), 50)
    rel_vol = safe(row.get("relative_volume_10d_calc"), 1)
    perf_3m = safe(row.get("Perf.3M"), 0)
    perf_6m = safe(row.get("Perf.6M"), 0)

    pct_from_high = ((high52 - price) / high52 * 100) if high52 and price else None

    criteria = {}
    criteria["Price > 200 MA (L=Leader)"] = {
        "value":    ("$%.2f vs $%.2f" % (price, sma200)) if price and sma200 else "N/A",
        "required": "Above",
        "pass":     price > sma200 if price and sma200 else False,
    }
    criteria["Price > 50 MA (L=Leader)"] = {
        "value":    ("$%.2f vs $%.2f" % (price, sma50)) if price and sma50 else "N/A",
        "required": "Above",
        "pass":     price > sma50 if price and sma50 else False,
    }
    criteria["Near 52W High (N=New High)"] = {
        "value":    ("%.1f%% below high" % pct_from_high) if pct_from_high is not None else "N/A",
        "required": "≤ 15% below",
        "pass":     pct_from_high is not None and pct_from_high <= 15,
        "warn":     pct_from_high is not None and 10 <= pct_from_high <= 15,
    }
    criteria["RS Proxy: 3M Perf"] = {
        "value":    ("%.1f%%" % perf_3m) if perf_3m is not None else "N/A",
        "required": "≥ +10%",
        "pass":     perf_3m is not None and perf_3m >= 10,
        "warn":     perf_3m is not None and 5 <= perf_3m < 10,
    }
    criteria["RS Proxy: 6M Perf"] = {
        "value":    ("%.1f%%" % perf_6m) if perf_6m is not None else "N/A",
        "required": "≥ +15%",
        "pass":     perf_6m is not None and perf_6m >= 15,
    }
    criteria["Volume (S=Supply/Demand)"] = {
        "value":    ("%.1fx relative" % rel_vol) if rel_vol else "N/A",
        "required": "≥ 1.0x (breakout: ≥ 1.4x)",
        "pass":     rel_vol is not None and rel_vol >= 1.0,
        "warn":     rel_vol is not None and 1.0 <= rel_vol < 1.4,
    }
    criteria["RSI Not Extended"] = {
        "value":    ("RSI %.1f" % rsi) if rsi else "N/A",
        "required": "< 85 (not climax)",
        "pass":     rsi is not None and rsi < 85,
        "warn":     rsi is not None and rsi > 75,
    }

    passed = sum(1 for v in criteria.values() if v.get("pass"))
    total  = len(criteria)
    core_pass = all([
        criteria["Price > 200 MA (L=Leader)"]["pass"],
        criteria["Price > 50 MA (L=Leader)"]["pass"],
        criteria["Near 52W High (N=New High)"]["pass"],
    ])

    return {
        "criteria":  criteria,
        "passed":    passed,
        "total":     total,
        "core_pass": core_pass,
        "score_pct": round(passed / total * 100),
        "note": "EPS/Sales growth data requires Finviz — run through finviz_scan.py pipeline for full CANSLIM score",
    }


def score_reversion(row):
    """Score stock for mean-reversion bounce setup."""
    price    = safe(row.get("close"), 0)
    sma50    = safe(row.get("SMA50"), 0)
    sma200   = safe(row.get("SMA200"), 0)
    rsi      = safe(row.get("RSI"), 50)
    rsi_prev = safe(row.get("RSI[1]"), 50)
    bb_lower = safe(row.get("BB.lower"), 0)
    bb_upper = safe(row.get("BB.upper"), 0)
    high52   = safe(row.get("High.All"), 0)
    rel_vol  = safe(row.get("relative_volume_10d_calc"), 1)
    perf_1m  = safe(row.get("Perf.1M"), 0)

    pct_from_high  = ((high52 - price) / high52 * 100) if high52 and price else None
    bb_pct         = ((price - bb_lower) / (bb_upper - bb_lower) * 100) if bb_upper and bb_lower and bb_upper != bb_lower else None
    rsi_divergence = rsi > rsi_prev if (rsi and rsi_prev) else False

    criteria = {}
    criteria["RSI Oversold"] = {
        "value":    ("RSI %.1f" % rsi) if rsi else "N/A",
        "required": "< 35 (< 25 = deep)",
        "pass":     rsi is not None and rsi < 35,
        "warn":     rsi is not None and 35 <= rsi <= 40,
    }
    criteria["RSI Turning Up"] = {
        "value":    ("RSI %.1f vs prev %.1f" % (rsi, rsi_prev)) if rsi and rsi_prev else "N/A",
        "required": "Current > Previous (reversal signal)",
        "pass":     rsi_divergence,
    }
    criteria["BB %B Oversold"] = {
        "value":    ("BB%%B %.0f%%" % bb_pct) if bb_pct is not None else "N/A",
        "required": "< 20% (near/below lower band)",
        "pass":     bb_pct is not None and bb_pct < 20,
        "warn":     bb_pct is not None and 20 <= bb_pct <= 30,
    }
    criteria["Significant Pullback"] = {
        "value":    ("%.1f%% from high, %.1f%% this month" % (pct_from_high, perf_1m)) if pct_from_high is not None else "N/A",
        "required": "≥ 10% off high OR -10% in 1 month",
        "pass":     (pct_from_high is not None and pct_from_high >= 10) or (perf_1m is not None and perf_1m <= -10),
    }
    criteria["Above 200 MA (Quality Filter)"] = {
        "value":    ("$%.2f vs 200MA $%.2f" % (price, sma200)) if price and sma200 else "N/A",
        "required": "Above (uptrend context)",
        "pass":     price > sma200 if price and sma200 else False,
    }
    criteria["Volume Spike (Capitulation)"] = {
        "value":    ("%.1fx relative volume" % rel_vol) if rel_vol else "N/A",
        "required": "≥ 1.5x on down move = capitulation",
        "pass":     rel_vol is not None and rel_vol >= 1.5,
        "warn":     rel_vol is not None and 1.0 <= rel_vol < 1.5,
    }

    passed = sum(1 for v in criteria.values() if v.get("pass"))
    total  = len(criteria)
    core_pass = all([
        criteria["RSI Oversold"]["pass"],
        criteria["Significant Pullback"]["pass"],
        criteria["Above 200 MA (Quality Filter)"]["pass"],
    ])

    return {
        "criteria":  criteria,
        "passed":    passed,
        "total":     total,
        "core_pass": core_pass,
        "score_pct": round(passed / total * 100),
    }


def calculate_trade_setup(row, strategy):
    """Calculate entry, stop, T1, T2, and R/R for the trade."""
    price  = safe(row.get("close"), 0)
    sma50  = safe(row.get("SMA50"), 0)
    sma200 = safe(row.get("SMA200"), 0)

    if not price:
        return {}

    if strategy == "minervini":
        entry = round(price * 1.005, 2)
        stop  = round(price * 0.92, 2)
        t1    = round(price * 1.20, 2)
        t2    = round(price * 1.30, 2)
    elif strategy == "canslim":
        entry = round(price * 1.01, 2)
        stop  = round(price * 0.93, 2)
        t1    = round(price * 1.20, 2)
        t2    = round(price * 1.25, 2)
    else:  # reversion
        entry = round(price * 1.01, 2)
        stop  = round(price * 0.95, 2)
        t1    = sma50 if sma50 and sma50 > entry else round(price * 1.08, 2)
        t2    = round(price * 1.15, 2)

    risk   = entry - stop
    reward = t1 - entry
    rr     = round(reward / risk, 1) if risk > 0 else 0

    return {
        "entry":      entry,
        "stop":       stop,
        "t1":         t1,
        "t2":         t2,
        "risk_pct":   round((entry - stop) / entry * 100, 1),
        "reward_pct": round((t1 - entry) / entry * 100, 1),
        "rr":         rr,
        "rr_ok":      rr >= 2.0,
        "note":       "⚠ Entry/stop are estimates. Adjust to actual chart pivot/support before trading.",
    }


def conviction_level(score_pct, core_pass, rr):
    if not core_pass or rr < 2.0:
        return "Low"
    if score_pct >= 80 and rr >= 3.0:
        return "High"
    if score_pct >= 60:
        return "Medium"
    return "Low"


def enrich_tickers(tickers, strategy, global_markets=False):
    """Fetch TV data and score each ticker."""
    df = fetch_global_tv_data(tickers) if global_markets else fetch_tv_data(tickers)

    if df.empty:
        print("❌ No data returned from TradingView", file=sys.stderr)
        return []

    results = []
    for idx, tv_row in df.iterrows():
        row = tv_row.to_dict()
        raw    = row.get("ticker") or row.get("name") or ""
        ticker = str(raw).split(":")[-1].strip().upper()

        if strategy == "minervini":
            score = score_minervini(row)
        elif strategy == "canslim":
            score = score_canslim(row)
        else:
            score = score_reversion(row)

        setup      = calculate_trade_setup(row, strategy)
        conviction = conviction_level(score["score_pct"], score["core_pass"], setup.get("rr", 0))

        results.append({
            "ticker":      ticker,
            "sector":      safe(row.get("sector"), ""),
            "price":       safe(row.get("close")),
            "change_pct":  safe(row.get("change")),
            "rsi":         safe(row.get("RSI")),
            "rel_volume":  safe(row.get("relative_volume_10d_calc")),
            "sma50":       safe(row.get("SMA50")),
            "sma200":      safe(row.get("SMA200")),
            "high52":      safe(row.get("High.All")),
            "low52":       safe(row.get("Low.All")),
            "perf_1w":     safe(row.get("Perf.W")),
            "perf_1m":     safe(row.get("Perf.1M")),
            "perf_3m":     safe(row.get("Perf.3M")),
            "score":       score,
            "setup":       setup,
            "conviction":  conviction,
            "valid_setup": score["core_pass"] and setup.get("rr_ok", False),
        })

    results.sort(key=lambda x: (not x["valid_setup"], -x["score"]["score_pct"]))
    return results


def build_html_dashboard(results, strategy, market_ctx=None, yahoo=None):
    """Generate self-contained HTML dashboard."""
    market_ctx = market_ctx or {}
    yahoo      = yahoo or {}
    now        = datetime.now().strftime("%B %d, %Y %H:%M")
    valid_count    = sum(1 for r in results if r["valid_setup"])
    strategy_label = {
        "minervini": "Minervini SEPA",
        "canslim":   "O'Neil CANSLIM",
        "reversion": "Mean Reversion",
    }.get(strategy, strategy)

    # ── helpers ───────────────────────────────────────────────────────────────
    def chg_color(v):
        return "#10b981" if (v or 0) >= 0 else "#ef4444"

    def chg_arrow(v):
        return "&#9650;" if (v or 0) >= 0 else "&#9660;"

    def conv_badge(c):
        colors = {
            "High":   ("#065f46", "#d1fae5"),
            "Medium": ("#92400e", "#fef3c7"),
            "Low":    ("#991b1b", "#fee2e2"),
        }
        fg, bg = colors.get(c, ("#334155", "#f1f5f9"))
        return ('<span style="background:%s;color:%s;padding:2px 8px;'
                'border-radius:9px;font-size:11px;font-weight:500">%s</span>' % (bg, fg, c))

    def pass_badge(p, warn=False):
        if p:
            return '<span style="color:#059669;font-weight:500">&#10003;</span>'
        if warn:
            return '<span style="color:#d97706;font-weight:500">&#9888;</span>'
        return '<span style="color:#dc2626;font-weight:500">&#10007;</span>'

    # ── Market Pulse ──────────────────────────────────────────────────────────
    pulse_items = []
    for sym in ["SPY", "QQQ", "IWM"]:
        d     = market_ctx.get(sym, {})
        chg   = d.get("change", 0) or 0
        price = d.get("price")
        price_str = ("$%.2f" % price) if price else "—"
        pulse_items.append(
            '<div style="display:flex;flex-direction:column;align-items:center;min-width:80px">'
            '<span style="font-weight:700;color:#f1f5f9">%s</span>'
            '<span style="font-size:12px;color:%s">%s%.2f%%</span>'
            '<span style="font-size:11px;color:#64748b">%s</span>'
            '</div>' % (sym, chg_color(chg), chg_arrow(chg), abs(chg), price_str)
        )

    vix       = market_ctx.get("VIX", {})
    vix_val   = vix.get("price")
    vix_chg   = vix.get("change", 0) or 0
    vix_color = "#ef4444" if (vix_val or 0) > 25 else "#f59e0b" if (vix_val or 0) > 18 else "#10b981"
    vix_html  = (
        '<div style="display:flex;flex-direction:column;align-items:center;min-width:70px">'
        '<span style="font-weight:700;color:#f1f5f9">VIX</span>'
        '<span style="font-size:15px;font-weight:700;color:%s">%.1f</span>'
        '<span style="font-size:10px;color:%s">%s%.2f%%</span>'
        '</div>' % (vix_color, vix_val, chg_color(-vix_chg), chg_arrow(vix_chg), abs(vix_chg))
    ) if vix_val else ""

    spy_ok = market_ctx.get("SPY", {}).get("above_50ma")
    qqq_ok = market_ctx.get("QQQ", {}).get("above_50ma")
    if spy_ok and qqq_ok:
        mkt_light, mkt_label = "#10b981", "UPTREND"
    elif spy_ok or qqq_ok:
        mkt_light, mkt_label = "#f59e0b", "MIXED"
    else:
        mkt_light, mkt_label = "#ef4444", "DOWNTREND"

    # ── Commodities / BTC block ───────────────────────────────────────────────
    _comm_icons = {"GC=F": "🥇", "SI=F": "🥈", "CL=F": "🛢", "BTC-USD": "₿"}
    _comm_html_parts = []
    for _ct, _clabel in COMMODITY_TICKERS.items():
        _ck  = _ct.replace("^", "")
        _cd  = market_ctx.get(_ck, {})
        _cp2 = _cd.get("price")
        _cc2 = _cd.get("change", 0) or 0
        if _cp2:
            _icon = _comm_icons.get(_ct, "")
            # Price formatting: BTC no decimals, Gold/Silver 1 dec, Oil 2 dec
            if _ct == "BTC-USD":
                _ps = "$%s" % "{:,.0f}".format(_cp2)
            elif _ct in ("GC=F", "SI=F"):
                _ps = "$%.1f" % _cp2
            else:
                _ps = "$%.2f" % _cp2
            _comm_html_parts.append(
                '<div style="display:flex;flex-direction:column;align-items:center;min-width:72px">'
                '<span style="font-size:11px;color:#94a3b8">%s%s</span>'
                '<span style="font-size:11px;color:%s">%s%.2f%%</span>'
                '<span style="font-size:10px;color:#64748b">%s</span>'
                '</div>' % (_icon, _clabel, chg_color(_cc2), chg_arrow(_cc2), abs(_cc2), _ps)
            )
    _comm_block = (
        '<div style="display:flex;gap:12px;padding-left:16px;'
        'border-left:1px solid #334155;flex-wrap:wrap">%s</div>' % "".join(_comm_html_parts)
    ) if _comm_html_parts else ""

    market_pulse_html = (
        '<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;'
        'padding:12px 20px;margin-bottom:12px;display:flex;align-items:center;gap:24px;flex-wrap:wrap">'
        '<div style="display:flex;align-items:center;gap:6px">'
        '<div style="width:10px;height:10px;border-radius:50%%;background:%s"></div>'
        '<span style="font-size:12px;font-weight:700;color:%s">%s</span>'
        '</div>'
        '<div style="display:flex;gap:20px;flex-wrap:wrap">%s</div>'
        '%s'
        '<div style="margin-left:auto">%s</div>'
        '</div>'
    ) % (mkt_light, mkt_light, mkt_label, "".join(pulse_items),
         _comm_block.replace("%", "%%"), vix_html)

    # ── Sector Heatmap ────────────────────────────────────────────────────────
    sector_cells = []
    sector_order = ["XLK", "XLC", "XLV", "XLF", "XLE", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLU"]
    for etf in sector_order:
        d         = market_ctx.get(etf, {})
        chg       = d.get("change", 0) or 0
        name      = SECTOR_ETFS.get(etf, etf)
        intensity = min(abs(chg) / 3, 1.0)
        if chg >= 0:
            bg = "rgba(16,185,129,%.2f)" % (0.15 + intensity * 0.45)
            tc = "#6ee7b7" if chg > 1.5 else "#34d399"
        else:
            bg = "rgba(239,68,68,%.2f)" % (0.15 + intensity * 0.45)
            tc = "#fca5a5" if chg < -1.5 else "#f87171"
        sector_cells.append(
            '<div style="background:%s;border-radius:6px;padding:8px 10px;'
            'text-align:center;min-width:90px;flex:1">'
            '<div style="font-size:11px;font-weight:700;color:%s">%s%.2f%%</div>'
            '<div style="font-size:10px;color:#94a3b8;margin-top:2px">%s</div>'
            '<div style="font-size:10px;color:#64748b">%s</div>'
            '</div>' % (bg, tc, chg_arrow(chg), abs(chg), name, etf)
        )

    sector_html = (
        '<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;'
        'padding:12px 16px;margin-bottom:12px">'
        '<div style="font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;'
        'letter-spacing:.5px;margin-bottom:10px">Sector Heatmap — today</div>'
        '<div style="display:flex;gap:6px;flex-wrap:wrap">%s</div>'
        '</div>'
    ) % "".join(sector_cells)

    # ── Table rows and detail cards ───────────────────────────────────────────
    rows_html  = ""
    cards_html = ""

    for r in results:
        ticker = r["ticker"]
        setup  = r.get("setup", {})
        score  = r.get("score", {})
        valid  = r["valid_setup"]

        row_bg = "#0f2a1a" if valid else "#1e1e1e"

        ydata     = yahoo.get(ticker, {})
        pre_chg   = ydata.get("pre_chg")
        post_chg  = ydata.get("post_chg")
        earn_date = ydata.get("earnings_date")
        days_earn = ydata.get("days_to_earn", 999)

        def pp_cell(chg, _label):
            if chg is None:
                return '<td style="padding:8px 10px;color:#475569;font-size:11px">—</td>'
            color = "#10b981" if chg >= 0 else "#ef4444"
            arrow = "&#9650;" if chg >= 0 else "&#9660;"
            return ('<td style="padding:8px 10px;font-size:11px;color:%s">%s%.2f%%</td>'
                    % (color, arrow, abs(chg)))

        if earn_date:
            if days_earn <= 7:
                earn_cell = ('<td style="padding:8px 10px;font-size:11px;color:#ef4444;'
                             'font-weight:700">&#9888; %s</td>' % earn_date)
            elif days_earn <= 14:
                earn_cell = ('<td style="padding:8px 10px;font-size:11px;color:#f59e0b">'
                             '&#128197; %s</td>' % earn_date)
            else:
                earn_cell = ('<td style="padding:8px 10px;font-size:11px;color:#64748b">'
                             '%s</td>' % earn_date)
        else:
            earn_cell = '<td style="padding:8px 10px;color:#334155;font-size:11px">—</td>'

        chg_pct  = r.get("change_pct", 0) or 0
        chg_col  = "#10b981" if chg_pct >= 0 else "#ef4444"
        rr_col   = "#10b981" if setup.get("rr_ok") else "#ef4444"
        vld_sym  = "&#10003;" if valid else "&#10007;"
        vld_col  = "#10b981" if valid else "#ef4444"

        price_val  = r.get("price") or 0
        entry_val  = setup.get("entry") or 0
        stop_val   = setup.get("stop") or 0
        t1_val     = setup.get("t1") or 0
        sector_val = r.get("sector") or ""

        # ── Insider activity cell ─────────────────────────────────────────────
        _row_ibuys  = ydata.get("insider_buys",  [])
        _row_isells = ydata.get("insider_sells", [])
        _row_insts  = ydata.get("institutions",  [])
        _chart_pat  = ydata.get("chart_pattern") or {}

        # ── Pattern cell ───────────────────────────────────────────────────
        _pat_name = _chart_pat.get("pattern")
        _pat_sc   = _chart_pat.get("score", 0)
        _pat_bpt  = _chart_pat.get("buy_point")
        _pat_wks  = _chart_pat.get("weeks", 0)
        _pat_nc   = _chart_pat.get("contractions", 0)
        _pat_vstb = _chart_pat.get("volatility_score", 0)
        _pat_vd   = _chart_pat.get("vol_dry", False)

        if _pat_name:
            # Color by pattern type
            _pc = {"VCP": "#34d399", "CUP+HANDLE": "#60a5fa", "FLAT BASE": "#fbbf24"}.get(_pat_name, "#94a3b8")
            # Score bar width (0-100%)
            _bar = '<div style="height:3px;background:%s;width:%d%%;border-radius:2px;margin-top:2px"></div>' % (_pc, _pat_sc)
            _vd_icon = '<span style="color:#64748b;font-size:9px"> Vol&#8595;</span>' if _pat_vd else ''
            pat_cell = (
                '<td style="padding:5px 8px;white-space:nowrap">'
                '<span style="color:%s;font-weight:700;font-size:10px">%s</span>'
                '<span style="color:#64748b;font-size:9px"> %d</span>'
                '%s%s'
                '</td>' % (_pc, _pat_name, _pat_sc, _vd_icon, _bar)
            )
        else:
            pat_cell = '<td style="padding:5px 8px;color:#334155;font-size:11px">—</td>'

        if _row_ibuys or _row_isells:
            _ins_parts = []
            if _row_ibuys:
                _ib_sh = sum(x["shares"] for x in _row_ibuys)
                _ib_s  = ("%.0fK" % (_ib_sh/1000)) if _ib_sh >= 1000 else str(_ib_sh)
                _ins_parts.append(
                    '<span style="color:#10b981;font-weight:600">&#9650;%d</span>'
                    '<span style="color:#475569;font-size:10px"> %s</span>' % (len(_row_ibuys), _ib_s)
                )
            if _row_isells:
                _is_sh = sum(x["shares"] for x in _row_isells)
                _is_s  = ("%.0fK" % (_is_sh/1000)) if _is_sh >= 1000 else str(_is_sh)
                _ins_parts.append(
                    '<span style="color:#ef4444;font-weight:600">&#9660;%d</span>'
                    '<span style="color:#475569;font-size:10px"> %s</span>' % (len(_row_isells), _is_s)
                )
            ins_cell = ('<td style="padding:6px 8px;white-space:nowrap;font-size:11px">'
                        + ' '.join(_ins_parts) + '</td>')
        else:
            ins_cell = '<td style="padding:6px 8px;color:#334155;font-size:11px">—</td>'

        # ── Top institutional holder cell ─────────────────────────────────────
        if _row_insts:
            # Buyers / sellers across top-5
            _buyers  = [x for x in _row_insts if (x.get("change") or 0) > 0]
            _sellers = [x for x in _row_insts if (x.get("change") or 0) < 0]
            _top = _row_insts[0]
            _chg = _top.get("change")
            if _chg is not None and _chg > 0:
                _arrow, _ac = "&#9650;", "#10b981"
            elif _chg is not None and _chg < 0:
                _arrow, _ac = "&#9660;", "#ef4444"
            else:
                _arrow, _ac = "&#9654;", "#64748b"
            _summary_parts = []
            if _buyers:  _summary_parts.append('<span style="color:#10b981">%d&#9650;</span>' % len(_buyers))
            if _sellers: _summary_parts.append('<span style="color:#ef4444">%d&#9660;</span>' % len(_sellers))
            _summary = " ".join(_summary_parts)
            _chg_str = ("%.1f%%" % abs(_chg)) if _chg is not None else ""
            inst_cell = (
                '<td style="padding:6px 8px;font-size:10px;white-space:nowrap">'
                + '<span style="color:%s;font-weight:600">%s</span>' % (_ac, _arrow)
                + '<span style="color:#e2e8f0"> %s</span>' % _top["name"][:16]
                + ('<br><span style="color:%s">%s</span>' % (_ac, _chg_str) if _chg_str else "")
                + (' <span style="font-size:10px">%s</span>' % _summary if _summary else "")
                + '</td>'
            )
        else:
            inst_cell = '<td style="padding:6px 8px;color:#334155;font-size:11px">—</td>'

        # News sentiment dot for ticker cell
        _news_items_row = ydata.get('news', [])
        _ns_scores = [_x.get('s', 0) for _x in _news_items_row]
        _ns_total  = sum(_ns_scores)
        if _ns_total > 0:
            _news_sdot = '<span style="color:#10b981;font-size:8px;vertical-align:super">&#9679;</span>'
        elif _ns_total < 0:
            _news_sdot = '<span style="color:#ef4444;font-size:8px;vertical-align:super">&#9679;</span>'
        elif _ns_scores:
            _news_sdot = '<span style="color:#475569;font-size:8px;vertical-align:super">&#9675;</span>'
        else:
            _news_sdot = ''

        rows_html += (
            '<tr id="row-%(t)s" style="background:%(bg)s;cursor:pointer"'
            ' data-ticker="%(t)s" data-price="%(p)s" data-entry="%(e)s"'
            ' data-stop="%(s)s" data-t1="%(t1)s" data-strategy="%(strat)s"'
            ' data-sector="%(sec)s"'
            ' onclick="showDetail(\'%(t)s\')">'
            '<td style="padding:8px 12px;font-weight:600;color:#e2e8f0">%(t)s%(sdot)s</td>'
            '<td style="padding:8px 12px;color:#94a3b8;font-size:12px">%(sec)s</td>'
            '<td style="padding:8px 12px;color:#e2e8f0">$%(p)s</td>'
            '<td style="padding:8px 12px;color:%(cc)s">%(cpct)s</td>'
            '%(pre)s%(post)s%(earn)s'
            '<td style="padding:8px 12px;color:#94a3b8">$%(ent)s</td>'
            '<td style="padding:8px 12px;color:#ef4444">$%(stp)s</td>'
            '<td style="padding:8px 12px;color:#10b981">$%(t1d)s</td>'
            '<td style="padding:8px 12px;color:%(rrc)s">%(rr)s:1</td>'
            '<td style="padding:8px 12px">%(conv)s</td>'
            '<td style="padding:8px 12px;color:%(vc)s">%(vs)s</td>'
            '%(pat)s%(ins)s'
            '%(inst)s'
            '<td style="padding:4px 8px;position:sticky;right:0;background:#253347;z-index:1" onclick="event.stopPropagation()">'
            '<button id="wbtn-%(t)s" onclick="toggleWatch(\'%(t)s\',%(p)s,%(e)s,%(s)s,%(t1)s,\'%(strat)s\',\'%(sec)s\')"'
            ' style="background:none;border:1px solid #475569;border-radius:5px;'
            'color:#64748b;cursor:pointer;font-size:14px;padding:2px 7px;transition:all .2s"'
            ' title="Add to watchlist">&#9734;</button>'
            '</td>'
            '</tr>'
        ) % {
            "t": ticker, "bg": row_bg,
            "p": price_val, "e": entry_val, "s": stop_val, "t1": t1_val,
            "strat": strategy, "sec": sector_val,
            "cc": chg_col, "cpct": ("%+.1f%%" % chg_pct),
            "pre": pp_cell(pre_chg, "Pre"),
            "post": pp_cell(post_chg, "Post"),
            "earn": earn_cell,
            "ent": setup.get("entry", "N/A"),
            "stp": setup.get("stop", "N/A"),
            "t1d": setup.get("t1", "N/A"),
            "rrc": rr_col, "rr": setup.get("rr", "—"),
            "conv": conv_badge(r.get("conviction", "Low")),
            "vc": vld_col, "vs": vld_sym,
            "pat": pat_cell, "ins": ins_cell, "inst": inst_cell, "sdot": _news_sdot,
        }

        # ── Criteria scorecard rows ───────────────────────────────────────────
        crit_rows = ""
        for crit_name, c in score.get("criteria", {}).items():
            c_val  = c.get("value") or ""
            c_req  = c.get("required") or ""
            c_pass = c.get("pass", False)
            c_warn = c.get("warn", False)
            crit_rows += (
                '<tr>'
                '<td style="padding:6px 10px;color:#94a3b8;font-size:13px">%s</td>'
                '<td style="padding:6px 10px;color:#e2e8f0;font-size:13px">%s</td>'
                '<td style="padding:6px 10px;color:#64748b;font-size:12px">%s</td>'
                '<td style="padding:6px 10px;text-align:center">%s</td>'
                '</tr>'
            ) % (crit_name, c_val, c_req, pass_badge(c_pass, c_warn))

        # ── Price level SVG bar ───────────────────────────────────────────────
        if setup.get("entry") and setup.get("stop") and setup.get("t1"):
            sv = setup["stop"]
            ev = setup["entry"]
            t1v = setup["t1"]
            t2v = setup.get("t2", t1v * 1.05)
            lo  = min(sv, ev) * 0.98
            hi  = max(t2v, t1v) * 1.02
            rng = hi - lo

            def xp(v):
                return round((v - lo) / rng * 560 + 20, 1)

            xs, xe, xt1, xt2 = xp(sv), xp(ev), xp(t1v), xp(t2v)
            level_svg = (
                '<svg width="600" height="60" viewBox="0 0 600 60" style="margin:12px 0">'
                '<rect x="20" y="25" width="560" height="4" rx="2" fill="#334155"/>'
                '<rect x="%.1f" y="25" width="%.1f" height="4" fill="#ef4444"/>'
                '<rect x="%.1f" y="25" width="%.1f" height="4" fill="#10b981"/>'
                '<rect x="%.1f" y="25" width="%.1f" height="4" fill="#6ee7b7"/>'
                '<line x1="%.1f" y1="18" x2="%.1f" y2="36" stroke="#ef4444" stroke-width="2"/>'
                '<line x1="%.1f" y1="15" x2="%.1f" y2="39" stroke="#94a3b8" stroke-width="2"/>'
                '<line x1="%.1f" y1="18" x2="%.1f" y2="36" stroke="#10b981" stroke-width="2"/>'
                '<line x1="%.1f" y1="18" x2="%.1f" y2="36" stroke="#6ee7b7" stroke-width="2"/>'
                '<text x="%.1f" y="14" fill="#ef4444" font-size="10" text-anchor="middle">STOP $%.2f</text>'
                '<text x="%.1f" y="11" fill="#94a3b8" font-size="10" text-anchor="middle">ENTRY $%.2f</text>'
                '<text x="%.1f" y="14" fill="#10b981" font-size="10" text-anchor="middle">T1 $%.2f</text>'
                '<text x="%.1f" y="14" fill="#6ee7b7" font-size="10" text-anchor="middle">T2 $%.2f</text>'
                '<text x="%.1f" y="52" fill="#ef4444" font-size="9" text-anchor="middle">-%.1f%%</text>'
                '<text x="%.1f" y="52" fill="#10b981" font-size="9" text-anchor="middle">+%.1f%%</text>'
                '</svg>'
            ) % (
                xs, xe - xs,
                xe, xt1 - xe,
                xt1, xt2 - xt1,
                xs, xs, xe, xe, xt1, xt1, xt2, xt2,
                xs, sv, xe, ev, xt1, t1v, xt2, t2v,
                xs, setup.get("risk_pct", 0),
                xt1, setup.get("reward_pct", 0),
            )
        else:
            level_svg = ""

        journal = (
            "Date: %s\n"
            "Ticker: %s\n"
            "Strategy: %s\n"
            "Entry: $%s | Stop: $%s | T1: $%s | T2: $%s\n"
            "R/R: %s:1 | Conviction: %s\n"
            "Criteria passed: %s/%s (%s%%)\n"
            "RSI: %s | Rel Vol: %.1f x | 1M perf: %.1f%%"
        ) % (
            datetime.now().strftime("%Y-%m-%d"),
            ticker,
            strategy_label,
            setup.get("entry", "?"), setup.get("stop", "?"),
            setup.get("t1", "?"), setup.get("t2", "?"),
            setup.get("rr", "?"), r.get("conviction", "?"),
            score.get("passed", 0), score.get("total", 0), score.get("score_pct", 0),
            r.get("rsi", "?"),
            r.get("rel_volume") or 0,
            r.get("perf_1m") or 0,
        )

        rr_card_col = "#10b981" if setup.get("rr_ok") else "#ef4444"
        rr_card_lbl = "&#10003; Valid" if setup.get("rr_ok") else "&#10007; < 2:1"

        # ── Card extras: RS Line, Analyst Ratings, News ──────────────────────
        yh_ex        = yahoo.get(ticker, {})
        rs_pts       = yh_ex.get("rs_points", [])
        _atr_val, _atr_pct = (yh_ex.get("atr") or (None, None))
        a_target     = yh_ex.get("analyst_target")
        a_rec        = yh_ex.get("analyst_rec", "")
        a_n          = yh_ex.get("analyst_n")
        news_list    = yh_ex.get("news", [])
        ins_buys     = yh_ex.get("insider_buys",  [])
        ins_sells    = yh_ex.get("insider_sells", [])
        inst_holders = yh_ex.get("institutions",  [])
        _cp          = yh_ex.get("chart_pattern") or {}

        # RS Line SVG (polyline 190×50, normalised 0-1 vs SPY)
        if rs_pts and len(rs_pts) >= 5:
            _W, _H, _n = 190, 50, len(rs_pts)
            _coords = " ".join(
                "%.1f,%.1f" % (i / (_n - 1) * (_W - 6) + 3,
                               (1 - v) * (_H - 6) + 3)
                for i, v in enumerate(rs_pts)
            )
            _rc   = "#10b981" if rs_pts[-1] > rs_pts[0] else "#ef4444"
            _rpct = (rs_pts[-1] - rs_pts[0]) * 100
            _rsign = "+" if _rpct > 0 else ""
            _rs_svg = (
                '<svg width="190" height="50" viewBox="0 0 190 50" '
                'style="background:#0a0f1e;border-radius:4px;border:1px solid #1e293b;display:block">'
                '<polyline points="' + _coords + '" fill="none" stroke="' + _rc +
                '" stroke-width="1.5" stroke-linejoin="round"/>'
                '</svg>'
                '<div style="color:' + _rc + ';font-size:10px;margin-top:3px;text-align:right">'
                + _rsign + ("%.0f" % abs(_rpct)) + '% vs SPY</div>'
            )
        else:
            _rs_svg = '<span style="color:#475569;font-size:11px">No data</span>'

        # Analyst ratings
        _rec_map = {
            "strongbuy":  ("Strong Buy",  "#10b981"),
            "strong_buy": ("Strong Buy",  "#10b981"),
            "buy":        ("Buy",         "#22c55e"),
            "hold":       ("Hold",        "#f59e0b"),
            "sell":       ("Sell",        "#ef4444"),
            "strongsell": ("Strong Sell", "#dc2626"),
            "strong_sell":("Strong Sell", "#dc2626"),
        }
        # Performance from TradingView data
        _pw = r.get("perf_1w"); _pm = r.get("perf_1m"); _pq = r.get("perf_3m")
        def _pc(v, lbl):
            if v is None: return '<div style="text-align:center"><div style="color:#64748b;font-size:9px;text-transform:uppercase;margin-bottom:3px">' + lbl + '</div><div style="color:#475569;font-size:12px">—</div></div>'
            col = "#10b981" if v > 0 else "#ef4444" if v < 0 else "#94a3b8"
            sign = "+" if v > 0 else ""
            return ('<div style="text-align:center"><div style="color:#64748b;font-size:9px;text-transform:uppercase;margin-bottom:3px">' + lbl
                    + '</div><div style="color:' + col + ';font-size:13px;font-weight:600">' + sign + ("%.1f%%" % v) + '</div></div>')
        _perf_row = (
            '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-top:10px;'
            'background:#1e2d3d;border-radius:6px;padding:8px 6px">'
            + _pc(_pw, "Week") + _pc(_pm, "Month") + _pc(_pq, "Quarter") +
            '</div>'
        )
        if a_rec or a_target:
            _rl, _rc2 = _rec_map.get(a_rec.lower() if a_rec else "", ("—", "#64748b"))
            _tgt = ("$%.2f" % a_target) if a_target else "—"
            _up_str = ""
            _cur = r.get("price") or 0
            if a_target and _cur:
                _up = (a_target - _cur) / _cur * 100
                _uc = "#10b981" if _up > 0 else "#ef4444"
                _up_str = (' <span style="color:' + _uc + ';font-size:10px">('
                           + ("+" if _up > 0 else "") + ("%.0f" % _up) + '%)</span>')
            _n_str = ("%d analysts" % a_n) if a_n else ""
            _analyst_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:6px">ANALYST CONSENSUS</div>'
                '<span style="background:' + _rc2 + '33;color:' + _rc2 + ';padding:3px 10px;'
                'border-radius:5px;font-size:12px;font-weight:600">' + _rl + '</span>'
                '<div style="margin-top:8px;color:#e2e8f0;font-size:13px">Target: ' + _tgt + _up_str + '</div>'
                '<div style="color:#64748b;font-size:10px;margin-top:2px">' + _n_str + '</div>'
                + _perf_row
            )
        else:
            _analyst_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:6px">ANALYST CONSENSUS</div>'
                '<span style="color:#475569;font-size:11px">No data</span>'
                + _perf_row
            )

        # News headlines
        if news_list:
            _news_rows = ""
            _sent_scores = [_ni.get("s", 0) for _ni in news_list[:3]]
            _sent_total  = sum(_sent_scores)
            if _sent_total > 0:
                _sent_agg = ('<span style="color:#10b981;font-size:10px;margin-left:6px">'
                             '&#9679; позитив</span>')
            elif _sent_total < 0:
                _sent_agg = ('<span style="color:#ef4444;font-size:10px;margin-left:6px">'
                             '&#9679; негатив</span>')
            else:
                _sent_agg = ('<span style="color:#64748b;font-size:10px;margin-left:6px">'
                             '&#9679; нейтрал</span>')
            for _ni in news_list[:3]:
                _nt = (_ni.get("t") or "")[:80]
                _nu = _ni.get("u") or "#"
                _nd = (_ni.get("d") or "")[:16]
                _ns = _ni.get("s", 0)
                _sdot = ('&#9679;', '#10b981') if _ns > 0 else                         ('&#9679;', '#ef4444') if _ns < 0 else                         ('&#9675;', '#475569')
                _news_rows += (
                    '<div style="display:flex;gap:5px;align-items:flex-start;margin-bottom:7px">'
                    '<span style="color:%s;font-size:10px;margin-top:2px;flex-shrink:0">%s</span>'
                    '<div>'
                    '<a href="%s" target="_blank" rel="noopener" '
                    'style="color:#93c5fd;font-size:11px;text-decoration:none;line-height:1.4">%s</a>'
                    '<div style="color:#475569;font-size:10px;margin-top:1px">%s</div>'
                    '</div>'
                    '</div>'
                ) % (_sdot[1], _sdot[0], _nu, _nt, _nd)
            _news_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px">'
                'LATEST NEWS' + _sent_agg + '</div>'
                + _news_rows
            )
        else:
            _news_html = ('<div style="font-size:11px;color:#64748b;margin-bottom:8px">LATEST NEWS</div>'
                          '<span style="color:#475569;font-size:11px">No news</span>')

        # ── Insider transactions HTML ─────────────────────────────────────────
        _ib_cnt  = len(ins_buys)
        _is_cnt  = len(ins_sells)
        _ib_sh   = sum(x["shares"] for x in ins_buys)
        _is_sh   = sum(x["shares"] for x in ins_sells)

        def _fmt_sh(n):
            if n >= 1_000_000:
                return "%.1fM" % (n / 1_000_000)
            if n >= 1_000:
                return "%.0fK" % (n / 1_000)
            return str(n)

        if _ib_cnt or _is_cnt:
            _ins_rows = ""
            for _b in ins_buys[:3]:
                _ins_rows += (
                    '<div style="display:flex;justify-content:space-between;'
                    'align-items:center;margin-bottom:4px">'
                    '<span style="color:#94a3b8;font-size:10px">' + _b["name"] + '</span>'
                    '<span style="color:#10b981;font-size:10px;white-space:nowrap;margin-left:6px">'
                    '&#9650; ' + _fmt_sh(_b["shares"]) + ' ' + _b["date"] + '</span>'
                    '</div>'
                )
            for _s in ins_sells[:3]:
                _ins_rows += (
                    '<div style="display:flex;justify-content:space-between;'
                    'align-items:center;margin-bottom:4px">'
                    '<span style="color:#94a3b8;font-size:10px">' + _s["name"] + '</span>'
                    '<span style="color:#ef4444;font-size:10px;white-space:nowrap;margin-left:6px">'
                    '&#9660; ' + _fmt_sh(_s["shares"]) + ' ' + _s["date"] + '</span>'
                    '</div>'
                )
            _buy_badge = (
                '<span style="background:#06402b;color:#10b981;padding:2px 7px;'
                'border-radius:4px;font-size:11px;font-weight:600;margin-right:6px">'
                '&#9650; ' + str(_ib_cnt) + ' Buy' + (' ' + _fmt_sh(_ib_sh) if _ib_sh else '') + '</span>'
            ) if _ib_cnt else ""
            _sell_badge = (
                '<span style="background:#450a0a;color:#ef4444;padding:2px 7px;'
                'border-radius:4px;font-size:11px;font-weight:600">'
                '&#9660; ' + str(_is_cnt) + ' Sell' + (' ' + _fmt_sh(_is_sh) if _is_sh else '') + '</span>'
            ) if _is_cnt else ""
            _insider_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:6px">INSIDER (30D)</div>'
                '<div style="margin-bottom:8px">' + _buy_badge + _sell_badge + '</div>'
                + _ins_rows
            )
        else:
            _insider_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:6px">INSIDER (30D)</div>'
                '<span style="color:#475569;font-size:11px">No activity</span>'
            )

        # ── Institutional holders HTML ────────────────────────────────────────
        if inst_holders:
            _inst_rows = ""
            for _ih in inst_holders[:5]:
                _chg     = _ih.get("change")          # % change in position (can be None)
                _pct_col = "#10b981" if _ih["pct"] >= 1 else "#94a3b8"
                # Change badge: ▲ green / ▼ red / — grey
                if _chg is not None and _chg > 0:
                    _chg_badge = ('<span style="color:#10b981;font-size:10px;'
                                  'white-space:nowrap;margin-left:8px">&#9650; +%.1f%%</span>' % _chg)
                elif _chg is not None and _chg < 0:
                    _chg_badge = ('<span style="color:#ef4444;font-size:10px;'
                                  'white-space:nowrap;margin-left:8px">&#9660; %.1f%%</span>' % _chg)
                else:
                    _chg_badge = '<span style="color:#475569;font-size:10px;margin-left:8px">—</span>'
                _date_str = _ih.get("date", "")
                _inst_rows += (
                    '<div style="margin-bottom:7px">'
                    # Name + date line
                    '<div style="display:flex;justify-content:space-between;align-items:baseline">'
                    '<span style="color:#94a3b8;font-size:10px;flex:1;min-width:0;'
                    'overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                    + _ih["name"] + '</span>'
                    '<span style="color:#475569;font-size:9px;white-space:nowrap;margin-left:6px">'
                    + _date_str + '</span>'
                    '</div>'
                    # %held + change line
                    '<div style="display:flex;align-items:center;gap:6px;margin-top:2px">'
                    '<span style="color:' + _pct_col + ';font-size:10px;font-weight:600">'
                    + ("%.2f%% held" % _ih["pct"]) + '</span>'
                    + _chg_badge +
                    '</div>'
                    '</div>'
                )
            _inst_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px">'
                'INSTITUTIONAL HOLDERS'
                '<span style="font-size:9px;color:#475569;margin-left:6px">%held · Δ position</span>'
                '</div>'
                + _inst_rows
            )
        else:
            _inst_html = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:6px">INSTITUTIONAL HOLDERS</div>'
                '<span style="color:#475569;font-size:11px">No data</span>'
            )

        # ── Chart Pattern Panel ───────────────────────────────────────────────
        _cp_name = _cp.get("pattern")
        _cp_sc   = _cp.get("score", 0)
        _cp_col  = {"VCP": "#34d399", "CUP+HANDLE": "#60a5fa", "FLAT BASE": "#fbbf24"}.get(_cp_name, "#94a3b8")
        if _cp_name:
            _cp_nc   = _cp.get("contractions", 0)
            _cp_wks  = _cp.get("weeks", 0)
            _cp_dep  = _cp.get("depth_pct", 0)
            _cp_last = _cp.get("last_contraction_pct", 0)
            _cp_vstb = _cp.get("volatility_score", 0)
            _cp_vd   = _cp.get("vol_dry", False)
            _cp_bpt  = _cp.get("buy_point")
            _cp_det  = _cp.get("details", "")

            # Minervini VCP checklist
            _checks = []
            if _cp_name == "VCP":
                _checks = [
                    (_cp_nc >= 2,        "%d contractions (need ≥2)" % _cp_nc),
                    (_cp_nc >= 3,        "3+ contractions (ideal)"),
                    (_cp_last <= 10,     "Final contraction ≤10%% (%.1f%%)" % _cp_last),
                    (_cp_vd,             "Volume drying up"),
                    (_cp_vstb >= 60,     "Volatility contracting (score %d)" % _cp_vstb),
                    (_cp_wks >= 3,       "Base ≥3 weeks (%d weeks)" % _cp_wks),
                ]
            elif _cp_name == "CUP+HANDLE":
                _checks = [
                    (10 <= _cp_dep <= 35, "Cup depth 10-35%% (%.1f%%)" % _cp_dep),
                    (_cp_last <= 12,      "Handle depth ≤12%% (%.1f%%)" % _cp_last),
                    (_cp_vd,              "Volume dry on handle"),
                    (_cp_wks >= 7,        "Base ≥7 weeks (%d weeks)" % _cp_wks),
                ]
            elif _cp_name == "FLAT BASE":
                _checks = [
                    (_cp_dep <= 15,  "Range ≤15%% (%.1f%%)" % _cp_dep),
                    (_cp_vd,         "Volume contracting"),
                    (_cp_wks >= 5,   "Base ≥5 weeks (%d weeks)" % _cp_wks),
                ]

            _chk_html = "".join(
                '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
                '<span style="color:%s;font-size:12px">%s</span>'
                '<span style="color:#94a3b8;font-size:11px">%s</span>'
                '</div>' % (("#10b981" if ok else "#ef4444"), ("✓" if ok else "✗"), txt)
                for ok, txt in _checks
            )

            # Score bar
            _sc_bar = (
                '<div style="background:#1e293b;border-radius:4px;height:8px;margin:8px 0">'
                '<div style="background:%s;width:%d%%;height:8px;border-radius:4px"></div>'
                '</div>' % (_cp_col, _cp_sc)
            )

            _pat_panel = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px">CHART PATTERN</div>'
                '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
                '<span style="color:%s;font-size:16px;font-weight:700">%s</span>'
                '<span style="color:#94a3b8;font-size:11px">score %d/100</span>'
                '</div>'
                % (_cp_col, _cp_name, _cp_sc)
                + _sc_bar
                + '<div style="color:#64748b;font-size:10px;margin-bottom:8px">%s</div>' % _cp_det
                + _chk_html
                + (('<div style="margin-top:8px;padding:6px 10px;background:#0d2b1f;'
                    'border:1px solid #34d399;border-radius:6px;color:#34d399;font-size:11px">'
                    '&#9650; Buy point: $%.2f</div>' % _cp_bpt) if _cp_bpt else "")
            )
        else:
            _pat_panel = (
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px">CHART PATTERN</div>'
                '<span style="color:#475569;font-size:11px">No pattern detected</span>'
            )

        # ATR block for RS panel
        if _atr_val:
            _price_now = r.get('close') or r.get('price') or 0
            _atr1  = round(_atr_val, 2)
            _atr15 = round(_atr_val * 1.5, 2)
            _atr_block = (
                '<div style="margin-top:10px;border-top:1px solid #1e293b;padding-top:8px">'
                '<div style="font-size:10px;color:#64748b;margin-bottom:4px">'
                'ATR (14-week) '
                + ('<span style="color:#94a3b8">%.2f%%</span>' % _atr_pct) +
                '</div>'
                '<div style="display:flex;gap:10px">'
                '<div style="background:#132032;border:1px solid #1e3a5f;border-radius:5px;'
                'padding:4px 8px;text-align:center">'
                '<div style="color:#64748b;font-size:9px">1 ATR</div>'
                + ('<div style="color:#60a5fa;font-size:13px;font-weight:700">$%.2f</div>' % _atr1) +
                '</div>'
                '<div style="background:#1a1032;border:1px solid #4c1d95;border-radius:5px;'
                'padding:4px 8px;text-align:center">'
                '<div style="color:#64748b;font-size:9px">1.5 ATR</div>'
                + ('<div style="color:#a78bfa;font-size:13px;font-weight:700">$%.2f</div>' % _atr15) +
                '</div>'
                '</div>'
                '</div>'
            )
        else:
            _atr_block = ''

        _extras_html = (
            # Row 1: RS Line / Analyst / News
            '<div style="display:grid;grid-template-columns:200px 1fr 1fr;gap:10px;margin-bottom:10px">'
            '<div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px">'
            '<div style="font-size:11px;color:#64748b;margin-bottom:6px">RS LINE vs SPY (3M)</div>'
            + _rs_svg
            + (_atr_block if _atr_val else '')
            + '</div>'
            '<div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px">'
            + _analyst_html +
            '</div>'
            '<div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px">'
            ''
            + _news_html +
            '</div>'
            '</div>'
            # Row 2: Pattern / Insider / Institutional
            '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:16px">'
            '<div style="background:#0f172a;border:1px solid #1e3a2f;border-radius:8px;padding:12px">'
            + _pat_panel +
            '</div>'
            '<div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px">'
            + _insider_html +
            '</div>'
            '<div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px">'
            + _inst_html +
            '</div>'
            '</div>'
        )

        _detail_watch_btn = (
            '<button id="wbtn-detail-{t}"'
            ' onclick="toggleWatch(\'{t}\',{p},{e},{s},{t1},\'{strat}\',\'{sec}\')"'
            ' style="background:none;border:1px solid #475569;border-radius:6px;'
            'color:#64748b;cursor:pointer;font-size:18px;padding:2px 8px;'
            'transition:all .2s;line-height:1" title="Add to watchlist">&#9734;</button>'
        ).format(
            t=ticker, p=price_val, e=entry_val, s=stop_val, t1=t1_val,
            strat=strategy, sec=r.get('sector','').replace("'",'')
        )

        cards_html += (
            '<div id="detail-%s" class="detail-card" style="display:none">'

            '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">'
            '<span style="font-size:22px;font-weight:600;color:#e2e8f0">%s</span>'
            '<span style="background:#1e3a5f;color:#60a5fa;padding:3px 10px;border-radius:9px;font-size:12px">%s</span>'
            '%s'
            + _detail_watch_btn +
            '<span style="margin-left:auto;color:#94a3b8;font-size:13px">%s</span>'
            '</div>'

            '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">'
            '<div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;text-align:center">'
            '<div style="color:#64748b;font-size:11px;margin-bottom:4px">ENTRY</div>'
            '<div style="color:#e2e8f0;font-size:18px;font-weight:600">$%s</div>'
            '</div>'
            '<div style="background:#0f172a;border:1px solid #ef4444;border-radius:8px;padding:12px;text-align:center">'
            '<div style="color:#ef4444;font-size:11px;margin-bottom:4px">STOP LOSS</div>'
            '<div style="color:#ef4444;font-size:18px;font-weight:600">$%s</div>'
            '<div style="color:#64748b;font-size:11px">-%.1f%%</div>'
            '</div>'
            '<div style="background:#0f172a;border:1px solid #10b981;border-radius:8px;padding:12px;text-align:center">'
            '<div style="color:#10b981;font-size:11px;margin-bottom:4px">TARGET T1</div>'
            '<div style="color:#10b981;font-size:18px;font-weight:600">$%s</div>'
            '<div style="color:#64748b;font-size:11px">+%.1f%%</div>'
            '</div>'
            '<div style="background:#0f172a;border:1px solid %s;border-radius:8px;padding:12px;text-align:center">'
            '<div style="color:#94a3b8;font-size:11px;margin-bottom:4px">R/R RATIO</div>'
            '<div style="color:%s;font-size:18px;font-weight:600">%s : 1</div>'
            '<div style="color:#64748b;font-size:11px">%s</div>'
            '</div>'
            '</div>'

            '%s'
            '%s'

            '<details style="margin-bottom:16px">'
            '<summary style="color:#94a3b8;cursor:pointer;font-size:13px;margin-bottom:8px">'
            'Criteria scorecard — %s/%s passed (%s%%)'
            '</summary>'
            '<table style="width:100%%;border-collapse:collapse;font-size:13px">'
            '<thead><tr style="border-bottom:1px solid #334155">'
            '<th style="padding:6px 10px;text-align:left;color:#64748b">Criterion</th>'
            '<th style="padding:6px 10px;text-align:left;color:#64748b">Value</th>'
            '<th style="padding:6px 10px;text-align:left;color:#64748b">Required</th>'
            '<th style="padding:6px 10px;text-align:center;color:#64748b">Status</th>'
            '</tr></thead>'
            '<tbody>%s</tbody>'
            '</table>'
            '</details>'

            '<div style="margin-bottom:16px">'
            '<div style="color:#64748b;font-size:11px;margin-bottom:6px">TRADE JOURNAL ENTRY</div>'
            '<textarea id="journal-%s" style="width:100%%;background:#0f172a;border:1px solid #334155;'
            'border-radius:6px;color:#94a3b8;font-family:monospace;font-size:12px;padding:10px;'
            'line-height:1.6;resize:vertical" rows="7">%s</textarea>'
            '<button onclick="copyJournal(\'%s\')" style="margin-top:6px;background:#1e3a5f;'
            'color:#60a5fa;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;'
            'font-size:12px">Copy</button>'
            '</div>'

            '</div>'
        ) % (
            ticker,
            ticker, strategy_label,
            conv_badge(r.get("conviction", "Low")),
            r.get("sector", ""),
            setup.get("entry", "—"),
            setup.get("stop", "—"), setup.get("risk_pct", 0),
            setup.get("t1", "—"), setup.get("reward_pct", 0),
            rr_card_col, rr_card_col, setup.get("rr", "—"), rr_card_lbl,
            level_svg,
            _extras_html,
            score.get("passed", 0), score.get("total", 0), score.get("score_pct", 0),
            crit_rows,
            ticker, journal, ticker,
        )

    # ── Full HTML page ────────────────────────────────────────────────────────
    # JavaScript — watchlist, alerts, export (built as plain string, no format() issues)
    js = """
var WL_KEY='swingtrader_watchlist';
function getWL(){try{return JSON.parse(localStorage.getItem(WL_KEY)||'[]');}catch(e){return[];}}
function saveWL(wl){localStorage.setItem(WL_KEY,JSON.stringify(wl));}
function isWatched(t){return getWL().some(function(x){return x.ticker===t;});}
function toggleWatch(ticker,price,entry,stop,t1,strategy,sector){
  var wl=getWL(),idx=wl.findIndex(function(x){return x.ticker===ticker;});
  if(idx>=0){wl.splice(idx,1);}
  else{wl.push({ticker:ticker,price:price,entry:entry,stop:stop,t1:t1,strategy:strategy,sector:sector,added:new Date().toISOString().slice(0,10)});}
  saveWL(wl);renderWatchlist();updateWatchBtn(ticker);
}
function updateWatchBtn(ticker){
  var ids=['wbtn-'+ticker,'wbtn-detail-'+ticker];
  ids.forEach(function(id){
    var btn=document.getElementById(id);
    if(!btn)return;
    var w=isWatched(ticker);
    btn.innerHTML=w?'&#9733;':'&#9734;';
    btn.style.color=w?'#f59e0b':'#64748b';
    btn.style.borderColor=w?'#f59e0b':'#475569';
  });
}
function alertStatus(item){
  var p=parseFloat(item.price)||0,e=parseFloat(item.entry)||0,s=parseFloat(item.stop)||0;
  if(!p||!e)return{label:'--',color:'#64748b',bg:'transparent'};
  if(p<=s)return{label:'STOP HIT',color:'#ef4444',bg:'rgba(239,68,68,.1)'};
  if(p>=e)return{label:'TRIGGERED',color:'#10b981',bg:'rgba(16,185,129,.1)'};
  var pct=((e-p)/e*100);
  if(pct<=2)return{label:'NEAR '+pct.toFixed(1)+'%',color:'#f59e0b',bg:'rgba(245,158,11,.1)'};
  return{label:pct.toFixed(1)+'% to entry',color:'#94a3b8',bg:'transparent'};
}
function distBar(item){
  var p=parseFloat(item.price)||0,e=parseFloat(item.entry)||0,s=parseFloat(item.stop)||0,t=parseFloat(item.t1)||0;
  if(!p||!e||!s||!t||t<=s)return '';
  var rng=t-s;
  var pp=Math.min(Math.max((p-s)/rng*100,0),100);
  var ep=Math.min(Math.max((e-s)/rng*100,0),100);
  var col=p>=e?'#10b981':p<=s?'#ef4444':'#60a5fa';
  return '<div style="position:relative;height:6px;background:#0f172a;border-radius:3px;margin:6px 0;min-width:140px">'
    +'<div style="position:absolute;left:0;top:0;height:100%;width:'+pp.toFixed(1)+'%;background:'+col+';border-radius:3px;opacity:.6"></div>'
    +'<div style="position:absolute;top:-3px;left:'+ep.toFixed(1)+'%;width:2px;height:12px;background:#94a3b8;border-radius:1px"></div>'
    +'<div style="position:absolute;top:-1px;left:'+pp.toFixed(1)+'%;width:8px;height:8px;background:'+col+';border-radius:50%;transform:translateX(-4px)"></div>'
    +'</div>'
    +'<div style="display:flex;justify-content:space-between;font-size:9px;color:#475569">'
    +'<span>STP $'+s+'</span><span>ENT $'+e+'</span><span>T1 $'+t+'</span></div>';
}
function removeWatch(btn){
  var t=btn.getAttribute('data-ticker');
  saveWL(getWL().filter(function(x){return x.ticker!==t;}));
  renderWatchlist();
  updateWatchBtn(t);
}
function renderWatchlist(){
  var wl=getWL(),panel=document.getElementById('watchlist-panel');
  if(!panel)return;
  wl=wl.map(function(item){
    var row=document.querySelector('[data-ticker="'+item.ticker+'"]');
    if(row){var lp=parseFloat(row.getAttribute('data-price'));if(lp)item.price=lp;}
    return item;
  });
  if(!wl.length){panel.innerHTML='';return;}
  var rows=wl.map(function(item){
    var st=alertStatus(item),bar=distBar(item);
    return '<tr style="background:'+st.bg+'">'
      +'<td style="padding:8px 12px;font-weight:600;color:#e2e8f0">'+item.ticker+'</td>'
      +'<td style="padding:8px 12px;color:#94a3b8;font-size:11px">'+(item.sector||'')+'</td>'
      +'<td style="padding:8px 12px;color:#e2e8f0">$'+(item.price||'--')+'</td>'
      +'<td style="padding:8px 12px;min-width:180px">'+bar+'</td>'
      +'<td style="padding:8px 12px;font-size:12px;font-weight:600;color:'+st.color+'">'+st.label+'</td>'
      +'<td style="padding:8px 12px;color:#64748b;font-size:11px">'+(item.added||'')+'</td>'
      +'<td style="padding:4px 8px"><button data-ticker="'+item.ticker+'" onclick="removeWatch(this)" '
      +'style="background:#7f1d1d;color:#fca5a5;border:none;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px">Remove</button></td>'
      +'</tr>';
  }).join('');
  panel.innerHTML='<div style="background:#1e293b;border:1px solid rgba(245,158,11,.35);border-radius:12px;overflow:hidden;margin-bottom:16px">'
    +'<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid #334155">'
    +'<span style="color:#f59e0b;font-weight:600;font-size:13px">&#9733; Watchlist ('+wl.length+')</span>'
    +'<div style="display:flex;gap:8px">'
    +'<button onclick="exportCSV()" style="background:#1e3a5f;color:#60a5fa;border:none;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:11px">&#8595; CSV</button>'
    +'<button onclick="exportTV()" style="background:#312e81;color:#a78bfa;border:none;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:11px">&#8631; TradingView</button>'
    +'</div></div>'
    +'<table style="width:100%;border-collapse:collapse">'
    +'<thead><tr style="border-bottom:1px solid #334155">'
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Ticker</th>'
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Sector</th>'
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Price</th>'
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Distance to Entry</th>'
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Status</th>'
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Added</th>'
    +'<th></th></tr></thead>'
    +'<tbody>'+rows+'</tbody></table></div>';
  document.querySelectorAll('[id^="wbtn-"]').forEach(function(btn){
    updateWatchBtn(btn.id.replace('wbtn-',''));
  });
}
function exportCSV(){
  var wl=getWL();
  if(!wl.length){alert('Watchlist is empty');return;}
  var csv=['Ticker,Sector,Strategy,Entry,Stop,T1,Added'].concat(
    wl.map(function(x){return[x.ticker,x.sector||'',x.strategy||'',x.entry||'',x.stop||'',x.t1||'',x.added||''].join(',');})
  ).join('\\n');
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download='watchlist_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
}
function exportTV(){
  var wl=getWL();
  if(!wl.length){alert('Watchlist is empty');return;}
  var list=wl.map(function(x){return x.ticker;}).join(',');
  navigator.clipboard.writeText(list).then(function(){
    alert('Copied to clipboard:\\n'+list+'\\n\\nPaste in TradingView Watchlist > Import');
  });
}
function showDetail(ticker){
  document.querySelectorAll('.detail-card').forEach(function(el){el.style.display='none';});
  var card=document.getElementById('detail-'+ticker),panel=document.getElementById('detail-panel');
  if(card){panel.innerHTML='';card.style.display='block';panel.appendChild(card);panel.scrollIntoView({behavior:'smooth'});updateWatchBtn(ticker);}
}
function copyJournal(ticker){
  var el=document.getElementById('journal-'+ticker);
  if(el){navigator.clipboard.writeText(el.value);}
}
document.addEventListener('DOMContentLoaded',function(){renderWatchlist();});
"""

    # ── Assemble final HTML ───────────────────────────────────────────────────
    header_btns = (
        '<button onclick="exportCSV()" style="background:#1e3a5f;color:#60a5fa;border:none;'
        'border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px">&#8595; CSV</button>'
        '<button onclick="exportTV()" style="background:#312e81;color:#a78bfa;border:none;'
        'border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px">&#8631; TradingView</button>'
    )

    html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>Swing Trader Dashboard &mdash; ' + now + '</title>\n'
        '<style>\n'
        '* { box-sizing:border-box; margin:0; padding:0; }\n'
        'body { background:#0f172a; color:#e2e8f0; font-family:system-ui,-apple-system,sans-serif; min-height:100vh; }\n'
        'table { width:100%; border-collapse:collapse; }\n'
        'tr:hover { background:#1e293b !important; }\n'
        'th { color:#64748b; font-size:11px; font-weight:500; text-align:left; padding:8px 12px; border-bottom:1px solid #334155; }\n'
        'details summary { padding:6px 0; cursor:pointer; }\n'
        '.detail-card { background:#1e293b; border:1px solid #334155; border-radius:12px; padding:20px; margin-top:16px; }\n'
        '</style>\n</head>\n<body>\n'
        '<div style="max-width:1150px;margin:0 auto;padding:24px">\n'

        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">\n'
        '  <div>\n'
        '    <h1 style="font-size:20px;font-weight:600;color:#e2e8f0">&#9889; Swing Trader Dashboard</h1>\n'
        '    <div style="color:#64748b;font-size:13px;margin-top:2px">' + now + ' &middot; ' + strategy_label + '</div>\n'
        '  </div>\n'
        '  <div style="display:flex;gap:10px;align-items:center">\n'
        '    ' + header_btns + '\n'
        '    <div style="text-align:center"><div style="font-size:22px;font-weight:600;color:#10b981">' + str(valid_count) + '</div>'
        '<div style="font-size:11px;color:#64748b">Valid</div></div>\n'
        '    <div style="text-align:center"><div style="font-size:22px;font-weight:600;color:#94a3b8">' + str(len(results)) + '</div>'
        '<div style="font-size:11px;color:#64748b">Scanned</div></div>\n'
        '  </div>\n'
        '</div>\n\n'
        + market_pulse_html + '\n'
        + sector_html + '\n\n'

        '<div id="watchlist-panel"></div>\n\n'

        '<div style="background:#1e293b;border:1px solid #334155;border-radius:12px;overflow-x:auto;margin-bottom:20px">\n'
        '<table>\n<thead>\n<tr>\n'
        '<th>Ticker</th><th>Sector</th><th>Price</th><th>Day%</th>\n'
        '<th style="color:#a78bfa">Pre</th><th style="color:#818cf8">Post</th>\n'
        '<th style="color:#fb923c">Earnings</th>\n'
        '<th>Entry</th><th>Stop</th><th>T1</th><th>R/R</th><th>Conv.</th><th>&#10003;</th>\n'
        '<th title="Chart pattern (VCP/Cup/Flat)" style="color:#34d399">Pattern</th>\n'
        '<th title="Insider activity (30d)" style="color:#a78bfa">Insider</th>\n'
        '<th title="Top institutional holder" style="color:#818cf8">Inst. Top</th>\n'
        '<th title="Watchlist" style="position:sticky;right:0;background:#1e293b;z-index:2">&#9734;</th>\n'
        '</tr>\n</thead>\n<tbody>\n'
        + rows_html +
        '</tbody>\n</table>\n</div>\n\n'

        '<div id="detail-panel">'
        '<div style="color:#64748b;font-size:13px;text-align:center;padding:20px">'
        '&#8593; Click a row to see full trade setup</div></div>\n'
        + cards_html + '\n\n'

        '</div>\n'
        '<script>' + js + '</script>\n'
        '</body></html>'
    )

    return html


def main():
    parser = argparse.ArgumentParser(description="TradingView data enricher + trade setup generator")
    parser.add_argument("--tickers", help="Comma-separated ticker list (NVDA,TSLA,...)")
    parser.add_argument("--file",    help="Text file with tickers (one per line)")
    parser.add_argument("--strategy", choices=["minervini", "canslim", "reversion"],
                        default="minervini")
    parser.add_argument("--global", dest="global_markets", action="store_true",
                        help="Search global exchanges (slower)")
    parser.add_argument("--html",   action="store_true", help="Output HTML dashboard")
    parser.add_argument("--output", help="Output file path (for --html)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    tickers = []
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    elif args.file:
        tickers = [l.strip().upper() for l in Path(args.file).read_text().splitlines() if l.strip()]
    elif not sys.stdin.isatty():
        data = json.load(sys.stdin)
        tickers = [t["ticker"] for t in data.get("tickers", [])]
        if not args.strategy and data.get("strategy"):
            args.strategy = data["strategy"]
        print(f"✅ Received {len(tickers)} tickers from pipe (strategy: {args.strategy})", file=sys.stderr)

    if not tickers:
        print("❌ No tickers provided. Use --tickers or pipe from finviz_scan.py", file=sys.stderr)
        sys.exit(1)

    results = enrich_tickers(tickers, args.strategy, args.global_markets)

    if args.html:
        print("📊 Fetching market context (SPY/QQQ/sectors)...", file=sys.stderr)
        market_ctx = fetch_market_context()
        yahoo_tickers = [r["ticker"] for r in results[:100]]
        print(f"🌙 Fetching pre/post market + earnings for top {len(yahoo_tickers)} tickers...", file=sys.stderr)
        yahoo = fetch_yahoo_data(yahoo_tickers)
        print(f"  → {len(yahoo)} tickers enriched from Yahoo", file=sys.stderr)
        html = build_html_dashboard(results, args.strategy, market_ctx, yahoo)
        out_path = args.output or ("watchlist_%s.html" % datetime.now().strftime("%Y-%m-%d"))
        Path(out_path).write_text(html, encoding="utf-8")
        print(f"✅ HTML dashboard saved to: {out_path}", file=sys.stderr)
    else:
        output = {
            "scan_time":    datetime.now().isoformat(),
            "strategy":     args.strategy,
            "count":        len(results),
            "valid_setups": sum(1 for r in results if r["valid_setup"]),
            "results":      results,
        }
        indent = 2 if args.pretty else None
        print(json.dumps(output, indent=indent, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
