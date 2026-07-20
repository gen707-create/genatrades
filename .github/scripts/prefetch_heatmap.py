"""Pre-fetch Finviz heatmap data BEFORE scanners.
Dual-fetch strategy:
  Step 1 - v=111 (Overview): Ticker, Company, Sector, Market Cap, Change (1D)
  Step 2 - v=161 (Performance): weekly / monthly / quarterly / half / yearly / YTD
  Merged on Ticker for a complete dataset with all period data.
"""
import os, sys, requests, csv, io, json, time

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
    """Parse '-2.21%' or '+0.14%' or '5.32' -> float."""
    try:
        return float((s or "0").replace("%", "").replace("+", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def parse_mc(s):
    """Parse '3.5T' / '450B' / '2500M' or raw number -> float (USD)."""
    s = (s or "").strip()
    for suf, mult in [("T", 1e12), ("B", 1e9), ("M", 1e6)]:
        if s.upper().endswith(suf):
            try:
                return float(s[:-1]) * mult
            except ValueError:
                return 0.0
    try:
        return float(s)  # raw millions from some Finviz views
    except ValueError:
        return 0.0


def finviz_fetch(view, filters="idx_sp500"):
    """Fetch CSV from Finviz Elite export. Returns (rows, cols) or ([], []) on error."""
    params = {"v": str(view), "f": filters, "auth": token}
    try:
        resp = session.get(
            "https://elite.finviz.com/export.ashx", params=params, timeout=30
        )
        print(f"v={view}: HTTP {resp.status_code}, {len(resp.text)} chars", file=sys.stderr)
        if resp.status_code != 200:
            print(f"v={view}: non-200 -- auth failed or rate limited", file=sys.stderr)
            return [], []
        text = resp.text.strip()
        if not text or text.startswith("<"):
            print(f"v={view}: got HTML instead of CSV", file=sys.stderr)
            return [], []
        rows = list(csv.DictReader(io.StringIO(text)))
        cols = list(rows[0].keys()) if rows else []
        print(f"v={view}: {len(rows)} rows | cols: {cols}", file=sys.stderr)
        return rows, cols
    except Exception as exc:
        print(f"v={view}: ERROR {exc}", file=sys.stderr)
        return [], []


def find_col(cols, *patterns):
    """Find first column whose lowercased name contains any of the given patterns."""
    for pat in patterns:
        c = next((x for x in cols if pat in x.lower()), None)
        if c:
            return c
    return None


try:
    # Step 1: v=111 (Overview) -- Market Cap + 1D Change + Sector
    rows111, cols111 = finviz_fetch("111")
    time.sleep(1.5)  # polite pause between requests

    # Step 2: v=151 (Performance view) -- weekly / monthly / quarterly ...
    # NOTE: v=151 = Performance, v=161 = Technical (RSI/SMA) -- do NOT confuse them
    rows161, cols161 = finviz_fetch("151")

    # Build base dict from v=111
    mc_col111  = find_col(cols111, "market cap", "cap")
    chg_col111 = find_col(cols111, "change", "chg")
    sec_col111 = find_col(cols111, "sector")

    base = {}
    for row in rows111:
        ticker = (row.get("Ticker", "") or "").strip()
        if not ticker:
            continue
        mc = parse_mc(row.get(mc_col111 or "", "") or "")
        if mc <= 0:
            continue
        base[ticker] = {
            "t": ticker,
            "n": (row.get("Company", "") or "").strip(),
            "s": (row.get(sec_col111 or "Sector", "Other") or "Other").strip(),
            "mc": mc,
            "c": parse_pct(row.get(chg_col111 or "Change", "0")),
            "w": 0.0, "m": 0.0, "q": 0.0, "h": 0.0, "y": 0.0, "ytd": 0.0,
        }

    print(f"v=111 base: {len(base)} stocks with Market Cap", file=sys.stderr)

    # Merge v=151 performance columns
    if rows161:
        # Finviz v=151 uses "Performance (Week)", "Performance (Month)" etc.
        # Also try "Perf Week" / "perf week" for older API responses
        week_col   = find_col(cols161, "performance (week)",  "perf week",  "week",  "wk")
        month_col  = find_col(cols161, "performance (month)", "perf month", "month", "mo")
        quart_col  = find_col(cols161, "performance (quarter)","perf quart","quart", "3 month")
        half_col   = find_col(cols161, "performance (half)",  "perf half",  "half",  "6 month")
        year_col   = find_col(cols161, "performance (year)",  "perf year",  "52-week","1 year","yr")
        ytd_col    = find_col(cols161, "performance (ytd)",   "perf ytd",   "ytd")
        chg_col161 = find_col(cols161, "change", "chg")
        mc_col161  = find_col(cols161, "market cap", "cap")

        print(
            f"v=151 perf cols -> week={week_col!r} month={month_col!r} "
            f"quart={quart_col!r} half={half_col!r} year={year_col!r} ytd={ytd_col!r}",
            file=sys.stderr,
        )

        merged = 0
        for row in rows161:
            ticker = (row.get("Ticker", "") or "").strip()
            if not ticker:
                continue

            # If ticker not in base (missing from v=111), try to add via v=161
            if ticker not in base and mc_col161:
                mc = parse_mc(row.get(mc_col161, "") or "")
                if mc > 0:
                    sec_col161 = find_col(cols161, "sector")
                    base[ticker] = {
                        "t": ticker,
                        "n": (row.get("Company", "") or "").strip(),
                        "s": (row.get(sec_col161 or "Sector", "Other") or "Other").strip(),
                        "mc": mc,
                        "c": parse_pct(row.get(chg_col161 or "Change", "0")),
                        "w": 0.0, "m": 0.0, "q": 0.0, "h": 0.0, "y": 0.0, "ytd": 0.0,
                    }

            if ticker in base:
                entry = base[ticker]
                if week_col:   entry["w"]   = parse_pct(row.get(week_col,  "0"))
                if month_col:  entry["m"]   = parse_pct(row.get(month_col, "0"))
                if quart_col:  entry["q"]   = parse_pct(row.get(quart_col, "0"))
                if half_col:   entry["h"]   = parse_pct(row.get(half_col,  "0"))
                if year_col:   entry["y"]   = parse_pct(row.get(year_col,  "0"))
                if ytd_col:    entry["ytd"] = parse_pct(row.get(ytd_col,   "0"))
                # Refresh 1D change from v=161 if v=111 had 0
                if chg_col161 and entry["c"] == 0.0:
                    entry["c"] = parse_pct(row.get(chg_col161, "0"))
                merged += 1

        print(f"v=151 merged performance into {merged} stocks", file=sys.stderr)

    # Finalise and save
    result = sorted(base.values(), key=lambda x: -x["mc"])[:600]
    json.dump(result, open("/tmp/heatmap.json", "w"))
    print(f"Saved {len(result)} stocks to /tmp/heatmap.json", file=sys.stderr)
    if result:
        print(f"Sample[0]: {result[0]}", file=sys.stderr)
        nonzero_w = sum(1 for x in result if x["w"] != 0.0)
        nonzero_m = sum(1 for x in result if x["m"] != 0.0)
        print(f"Stocks with non-zero 1W: {nonzero_w}, 1M: {nonzero_m} (out of {len(result)})", file=sys.stderr)

except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    json.dump([], open("/tmp/heatmap.json", "w"))
