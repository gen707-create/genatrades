"""Pre-fetch Finviz heatmap data BEFORE scanners to avoid rate limiting.
Saves up to 600 stocks to /tmp/heatmap.json for tv_enrich.py to consume.
"""
import os, sys, requests, csv, io, json

token = os.environ.get("FINVIZ_TOKEN", "")
if not token:
    print("No FINVIZ_TOKEN set", file=sys.stderr)
    json.dump([], open("/tmp/heatmap.json", "w"))
    sys.exit(0)

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
session.params = {"auth": token}

try:
    resp = session.get(
        "https://elite.finviz.com/export.ashx",
        params={"v": "152", "f": "cap_mid,cap_large,cap_mega"},
        timeout=30,
    )
    print(f"HTTP {resp.status_code}, {len(resp.text)} chars", file=sys.stderr)

    if resp.status_code != 200:
        print(f"Non-200 response: {repr(resp.text[:200])}", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    text = resp.text.strip()
    if not text or text.startswith("<"):
        print(f"HTML/empty response: {repr(text[:200])}", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        print("Empty CSV", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    cols = list(rows[0].keys())
    print(f"Cols ({len(cols)}): {cols[:15]}", file=sys.stderr)

    mc_col  = next((c for c in cols if "cap" in c.lower()), None)
    chg_col = next((c for c in cols if c.lower() in ("change", "chg")), None)
    sec_col = next((c for c in cols if "sector" in c.lower()), None)
    print(f"mc={mc_col!r}  chg={chg_col!r}  sec={sec_col!r}", file=sys.stderr)

    if not mc_col:
        print("ERROR: no market-cap column found", file=sys.stderr)
        json.dump([], open("/tmp/heatmap.json", "w"))
        sys.exit(0)

    def parse_mc(s):
        s = (s or "").strip()
        for suf, mult in [("T", 1e12), ("B", 1e9), ("M", 1e6)]:
            if s.endswith(suf):
                try:
                    return float(s[:-1]) * mult
                except ValueError:
                    return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    out = []
    for row in rows:
        mc = parse_mc(row.get(mc_col, ""))
        if mc <= 0:
            continue
        try:
            chg = float((row.get(chg_col or "Change", "0") or "0")
                        .replace("%", "").replace("+", "").strip())
        except ValueError:
            chg = 0.0
        out.append({
            "t": (row.get("Ticker", "") or "").strip(),
            "n": (row.get("Company", "") or "").strip(),
            "s": (row.get(sec_col or "Sector", "Other") or "Other").strip(),
            "mc": mc,
            "c": chg,
        })

    out.sort(key=lambda x: -x["mc"])
    json.dump(out[:600], open("/tmp/heatmap.json", "w"))
    print(f"Saved {len(out)} stocks to /tmp/heatmap.json", file=sys.stderr)

except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback; traceback.print_exc(file=sys.stderr)
    json.dump([], open("/tmp/heatmap.json", "w"))
