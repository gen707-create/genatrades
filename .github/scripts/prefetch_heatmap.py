"""Pre-fetch Finviz heatmap data BEFORE scanners to avoid rate limiting.
Saves up to 600 stocks to /tmp/heatmap.json for tv_enrich.py to consume.
v=161 = Performance view: Change (1D), Perf Week, Perf Month, Perf Quart, Perf Half, Perf Year, Perf YTD
f=idx_sp500 = S&P 500 index stocks (~500 stocks)
"""
import os, sys, requests, csv, io, json

token = os.environ.get("FINVIZ_TOKEN", "")
if not token:
    print("No FINVIZ_TOKEN set", file=sys.stderr)
    json.dump([], open("/tmp/heatmap.json", "w"))
    sys.exit(0)

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def parse_pct(s):
    """Parse '−2.21%' or '+0.14%' → float."""
    try:
        return float((s or "0").replace("%", "").replace("+", "").strip())
    except ValueError:
        return 0.0


def parse_mc(s):
    """Parse '3.5T' / '450B' / '2500M' or raw number → float (USD)."""
    s = (s or "").strip()
    for suf, mult in [("T", 1e12), ("B", 1e9), ("M", 1e6)]:
        if s.endswith(suf):
            try:
                return float(s[:-1]) * mult
            except ValueError:
                return 0.0
    try:
        return float(s)          # finviz v=161 returns raw millions
    except ValueError:
        return 0.0


try:
    # v=161 = Performance view (1D + weekly/monthly/quarterly... changes)
    # f=idx_sp500 = S&P 500 universe (~500 stocks)
    params = {"v": "161", "f": "idx_sp500", "auth": token}
    resp = session.get("https://elite.finviz.com/export.ashx", params=params, timeout=30)
    print(f"HTTP {resp.status_code}, {len(resp.text)} chars", file=sys.stderr)
    print(f"First 400 chars: {repr(resp.text[:400])}", file=sys.stderr)

    if resp.status_code != 200:
        print("Non-200 — auth failed or rate limited", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    text = resp.text.strip()
    if not text or text.startswith("<"):
        print("Got HTML instead of CSV — auth token may be wrong", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        print("Empty CSV", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    cols = list(rows[0].keys())
    print(f"ALL cols ({len(cols)}): {cols}", file=sys.stderr)

    # ── Detect columns ─────────────────────────────────────────────────────
    mc_col    = next((c for c in cols if c.lower() == "market cap"), None) or \
                next((c for c in cols if "cap" in c.lower()), None)

    chg_col   = next((c for c in cols if c.lower() in ("change", "chg")), None)
    week_col  = next((c for c in cols if "perf week"  in c.lower()), None)
    month_col = next((c for c in cols if "perf month" in c.lower()), None)
    quart_col = next((c for c in cols if "perf quart" in c.lower()), None)
    half_col  = next((c for c in cols if "perf half"  in c.lower()), None)
    year_col  = next((c for c in cols if "perf year"  in c.lower()), None)
    ytd_col   = next((c for c in cols if "perf ytd"   in c.lower()), None)
    sec_col   = next((c for c in cols if "sector"      in c.lower()), None)

    print(f"mc={mc_col!r}  chg={chg_col!r}  sec={sec_col!r}", file=sys.stderr)
    print(f"week={week_col!r}  month={month_col!r}  quart={quart_col!r}  "
          f"half={half_col!r}  year={year_col!r}  ytd={ytd_col!r}", file=sys.stderr)

    if not mc_col:
        print("ERROR: no market-cap column found", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    # ── Build output ───────────────────────────────────────────────────────
    out = []
    for row in rows:
        mc = parse_mc(row.get(mc_col, ""))
        if mc <= 0:
            continue
        ticker = (row.get("Ticker", "") or "").strip()
        if not ticker:
            continue
        out.append({
            "t":   ticker,
            "n":   (row.get("Company", "") or "").strip(),
            "s":   (row.get(sec_col or "Sector", "Other") or "Other").strip(),
            "mc":  mc,
            "c":   parse_pct(row.get(chg_col   or "Change",     "0")),
            "w":   parse_pct(row.get(week_col  or "",            "0")) if week_col  else 0.0,
            "m":   parse_pct(row.get(month_col or "",            "0")) if month_col else 0.0,
            "q":   parse_pct(row.get(quart_col or "",            "0")) if quart_col else 0.0,
            "h":   parse_pct(row.get(half_col  or "",            "0")) if half_col  else 0.0,
            "y":   parse_pct(row.get(year_col  or "",            "0")) if year_col  else 0.0,
            "ytd": parse_pct(row.get(ytd_col   or "",            "0")) if ytd_col   else 0.0,
        })

    out.sort(key=lambda x: -x["mc"])
    result = out[:600]
    json.dump(result, open("/tmp/heatmap.json", "w"))
    print(f"Saved {len(result)} stocks to /tmp/heatmap.json", file=sys.stderr)
    if result:
        print(f"Sample[0]: {result[0]}", file=sys.stderr)

except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    json.dump([], open("/tmp/heatmap.json", "w"))
