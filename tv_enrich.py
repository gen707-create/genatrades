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

# ── Market Pulse ETF universes ────────────────────────────────────────────────
THEME_ETFS = {
    "SMH":  "Semiconductors",
    "SOXX": "Semicon. Equipment",
    "HACK": "Cybersecurity",
    "CLOU": "Cloud Computing",
    "AIQ":  "AI & Big Data",
    "BOTZ": "Robotics / AI",
    "XBI":  "Biotech (Equal Wt)",
    "IBB":  "Biotech (Large Cap)",
    "IHI":  "Medical Devices",
    "KRE":  "Regional Banks",
    "KBE":  "Banks",
    "KIE":  "Insurance",
    "XOP":  "Oil & Gas E&P",
    "OIH":  "Oil Services",
    "TAN":  "Solar Energy",
    "ICLN": "Clean Energy",
    "XAR":  "Aerospace / Defense",
    "PAVE": "Infrastructure",
    "XRT":  "Retail",
    "XHB":  "Homebuilders",
    "COPX": "Copper Miners",
    "GDX":  "Gold Miners",
    "SLX":  "Steel",
    "ARKK": "Innovation",
    "ARKG": "Genomics",
    "ARKW": "Internet / Next Gen",
}

SECTOR_INDUSTRY_MAP = {
    "XLK":  [("SMH","Semiconductors"), ("SOXX","Semicon. Equipment"),
              ("CLOU","Cloud Computing"), ("AIQ","AI & Big Data"),
              ("HACK","Cybersecurity"), ("BOTZ","Robotics / AI"),
              ("ARKK","Innovation"), ("ARKW","Next Gen Internet")],
    "XLF":  [("KBE","Banks"), ("KRE","Regional Banks"), ("KIE","Insurance")],
    "XLV":  [("IBB","Biotech (LC)"), ("XBI","Biotech (EW)"),
              ("IHI","Medical Devices"), ("ARKG","Genomics")],
    "XLE":  [("XOP","Oil & Gas E&P"), ("OIH","Oil Services")],
    "XLC":  [],
    "XLY":  [("XRT","Retail"), ("XHB","Homebuilders")],
    "XLP":  [],
    "XLI":  [("XAR","Aerospace / Defense"), ("PAVE","Infrastructure")],
    "XLB":  [("COPX","Copper Miners"), ("GDX","Gold Miners"), ("SLX","Steel")],
    "XLRE": [],
    "XLU":  [("TAN","Solar Energy"), ("ICLN","Clean Energy")],
}

COUNTRY_ETFS = {
    "ACWI": "World (MSCI ACWI)",
    "EEM":  "Emerging Markets",
    "EFA":  "Developed ex-US",
    "FXI":  "China Large Cap",
    "KWEB": "China Internet",
    "EWJ":  "Japan",
    "EWZ":  "Brazil",
    "EWG":  "Germany",
    "EWY":  "South Korea",
    "INDA": "India",
    "EWT":  "Taiwan",
    "EWH":  "Hong Kong",
    "EWU":  "UK",
    "EWC":  "Canada",
    "EWA":  "Australia",
    "EWL":  "Switzerland",
    "EWQ":  "France",
    "ARGT": "Argentina",
    "ECH":  "Chile",
    "EWW":  "Mexico",
}

# Fallback top-5 holdings for Country ETFs (used when yfinance data unavailable)
# Weights are approximate; updated periodically
COUNTRY_ETF_FALLBACK_HOLDINGS = {
    "ACWI": [
        {"ticker":"AAPL","name":"Apple Inc","pct":4.1},{"ticker":"MSFT","name":"Microsoft","pct":3.8},
        {"ticker":"NVDA","name":"NVIDIA","pct":3.2},{"ticker":"AMZN","name":"Amazon","pct":2.1},
        {"ticker":"META","name":"Meta Platforms","pct":1.5},{"ticker":"GOOGL","name":"Alphabet A","pct":1.3},
        {"ticker":"GOOG","name":"Alphabet C","pct":1.2},{"ticker":"TSLA","name":"Tesla","pct":0.9},
        {"ticker":"BRK.B","name":"Berkshire Hath","pct":0.8},{"ticker":"JPM","name":"JPMorgan Chase","pct":0.7},
    ],
    "EEM": [
        {"ticker":"TSM","name":"TSMC","pct":7.5},{"ticker":"005930","name":"Samsung Electronics","pct":4.5},
        {"ticker":"PDD","name":"PDD Holdings","pct":3.2},{"ticker":"700","name":"Tencent Holdings","pct":2.9},
        {"ticker":"BABA","name":"Alibaba","pct":2.6},{"ticker":"000660","name":"SK Hynix","pct":1.8},
        {"ticker":"INFY","name":"Infosys","pct":1.6},{"ticker":"HDFCBANK","name":"HDFC Bank","pct":1.5},
        {"ticker":"939","name":"China Const Bank","pct":1.2},{"ticker":"RELIANCE","name":"Reliance Inds","pct":1.1},
    ],
    "EFA": [
        {"ticker":"NESN","name":"Nestlé SA","pct":2.4},{"ticker":"005930","name":"Samsung Elec","pct":2.3},
        {"ticker":"ASML","name":"ASML Holding","pct":2.1},{"ticker":"MC","name":"LVMH","pct":1.9},
        {"ticker":"7203","name":"Toyota Motor","pct":1.7},{"ticker":"AZN","name":"AstraZeneca","pct":1.6},
        {"ticker":"RHHBY","name":"Roche Holding","pct":1.5},{"ticker":"SHEL","name":"Shell plc","pct":1.4},
        {"ticker":"NVS","name":"Novartis","pct":1.3},{"ticker":"ALV","name":"Allianz SE","pct":1.2},
    ],
    "FXI": [
        {"ticker":"700","name":"Tencent Holdings","pct":10.2},{"ticker":"9988","name":"Alibaba Group","pct":8.8},
        {"ticker":"BIDU","name":"Baidu","pct":5.3},{"ticker":"3988","name":"Bank of China","pct":4.7},
        {"ticker":"939","name":"China Const Bank","pct":4.5},{"ticker":"9618","name":"JD.com","pct":4.2},
        {"ticker":"3690","name":"Meituan","pct":4.0},{"ticker":"PDD","name":"PDD Holdings","pct":3.8},
        {"ticker":"1398","name":"ICBC","pct":3.5},{"ticker":"1810","name":"Xiaomi","pct":3.2},
    ],
    "KWEB": [
        {"ticker":"700","name":"Tencent Holdings","pct":12.4},{"ticker":"9988","name":"Alibaba Group","pct":10.1},
        {"ticker":"3690","name":"Meituan","pct":8.7},{"ticker":"9618","name":"JD.com","pct":6.2},
        {"ticker":"BIDU","name":"Baidu","pct":5.4},{"ticker":"PDD","name":"PDD Holdings","pct":5.1},
        {"ticker":"9999","name":"NetEase","pct":4.8},{"ticker":"9961","name":"Trip.com","pct":4.2},
        {"ticker":"9626","name":"Bilibili","pct":3.6},{"ticker":"1024","name":"Kuaishou Tech","pct":3.1},
    ],
    "EWJ": [
        {"ticker":"7203","name":"Toyota Motor","pct":5.8},{"ticker":"8306","name":"Mitsubishi UFJ","pct":4.2},
        {"ticker":"6758","name":"Sony Group","pct":3.9},{"ticker":"6861","name":"Keyence","pct":3.4},
        {"ticker":"8035","name":"Tokyo Electron","pct":2.8},{"ticker":"4063","name":"Shin-Etsu Chem","pct":2.6},
        {"ticker":"9984","name":"SoftBank Group","pct":2.4},{"ticker":"6501","name":"Hitachi","pct":2.2},
        {"ticker":"6098","name":"Recruit Holdings","pct":2.1},{"ticker":"9433","name":"KDDI","pct":1.9},
    ],
    "EWZ": [
        {"ticker":"VALE","name":"Vale SA","pct":12.1},{"ticker":"PBR","name":"Petrobras","pct":10.4},
        {"ticker":"ITUB","name":"Itaú Unibanco","pct":9.7},{"ticker":"BBD","name":"Bradesco","pct":6.8},
        {"ticker":"SBS","name":"SABESP","pct":4.2},{"ticker":"BBAS3","name":"Banco do Brasil","pct":4.0},
        {"ticker":"BPAC11","name":"BTG Pactual","pct":3.8},{"ticker":"WEGE3","name":"WEG SA","pct":3.5},
        {"ticker":"ERJ","name":"Embraer","pct":3.2},{"ticker":"RENT3","name":"Localiza","pct":2.9},
    ],
    "EWG": [
        {"ticker":"SAP","name":"SAP SE","pct":8.2},{"ticker":"SIE","name":"Siemens AG","pct":6.4},
        {"ticker":"ALV","name":"Allianz SE","pct":5.7},{"ticker":"DTE","name":"Deutsche Telekom","pct":5.1},
        {"ticker":"MRK","name":"Merck KGaA","pct":4.6},{"ticker":"BMW","name":"BMW AG","pct":4.3},
        {"ticker":"VOW3","name":"Volkswagen","pct":3.9},{"ticker":"BAYN","name":"Bayer AG","pct":3.7},
        {"ticker":"DBK","name":"Deutsche Bank","pct":3.2},{"ticker":"IFX","name":"Infineon Tech","pct":3.0},
    ],
    "EWY": [
        {"ticker":"005930","name":"Samsung Electronics","pct":22.1},{"ticker":"000660","name":"SK Hynix","pct":6.4},
        {"ticker":"373220","name":"LG Energy Solution","pct":4.8},{"ticker":"005380","name":"Hyundai Motor","pct":4.2},
        {"ticker":"068270","name":"Celltrion","pct":2.9},{"ticker":"006400","name":"Samsung SDI","pct":2.6},
        {"ticker":"105560","name":"KB Financial","pct":2.4},{"ticker":"055550","name":"Shinhan Financial","pct":2.2},
        {"ticker":"207940","name":"Samsung Biologics","pct":2.0},{"ticker":"012330","name":"Hyundai Mobis","pct":1.8},
    ],
    "INDA": [
        {"ticker":"HDFCBANK","name":"HDFC Bank","pct":10.8},{"ticker":"RELIANCE","name":"Reliance Inds","pct":10.2},
        {"ticker":"INFY","name":"Infosys","pct":7.4},{"ticker":"ICICIBANK","name":"ICICI Bank","pct":7.1},
        {"ticker":"TCS","name":"Tata Consultancy","pct":6.8},{"ticker":"BHARTIARTL","name":"Bharti Airtel","pct":4.2},
        {"ticker":"WIPRO","name":"Wipro","pct":3.8},{"ticker":"AXISBANK","name":"Axis Bank","pct":3.5},
        {"ticker":"HCLTECH","name":"HCL Technologies","pct":3.2},{"ticker":"LT","name":"Larsen & Toubro","pct":2.9},
    ],
    "EWT": [
        {"ticker":"TSM","name":"TSMC","pct":25.4},{"ticker":"2454","name":"MediaTek","pct":6.8},
        {"ticker":"2317","name":"Hon Hai Precision","pct":4.7},{"ticker":"2882","name":"Cathay Financial","pct":3.2},
        {"ticker":"3008","name":"Largan Precision","pct":2.9},{"ticker":"2891","name":"CTBC Financial","pct":2.7},
        {"ticker":"2311","name":"ASE Technology","pct":2.4},{"ticker":"2308","name":"Delta Electronics","pct":2.2},
        {"ticker":"2881","name":"Fubon Financial","pct":2.0},{"ticker":"2379","name":"Realtek Semi","pct":1.8},
    ],
    "EWH": [
        {"ticker":"1299","name":"AIA Group","pct":11.2},{"ticker":"5","name":"HSBC Holdings","pct":9.8},
        {"ticker":"3690","name":"Meituan","pct":7.4},{"ticker":"388","name":"HK Exchanges","pct":6.5},
        {"ticker":"669","name":"Techtronic Inds","pct":5.1},{"ticker":"1","name":"CK Hutchison","pct":4.8},
        {"ticker":"16","name":"Sun Hung Kai Props","pct":4.5},{"ticker":"27","name":"Galaxy Entmt","pct":3.9},
        {"ticker":"823","name":"Link REIT","pct":3.6},{"ticker":"2","name":"CLP Holdings","pct":3.2},
    ],
    "EWU": [
        {"ticker":"AZN","name":"AstraZeneca","pct":9.8},{"ticker":"SHEL","name":"Shell plc","pct":8.4},
        {"ticker":"HSBC","name":"HSBC Holdings","pct":7.2},{"ticker":"ULVR","name":"Unilever","pct":6.1},
        {"ticker":"RIO","name":"Rio Tinto","pct":5.3},{"ticker":"BP","name":"BP plc","pct":4.8},
        {"ticker":"GSK","name":"GSK plc","pct":4.5},{"ticker":"DGE","name":"Diageo","pct":4.2},
        {"ticker":"NG.","name":"National Grid","pct":3.8},{"ticker":"RR.","name":"Rolls-Royce","pct":3.5},
    ],
    "EWC": [
        {"ticker":"RY","name":"Royal Bank Canada","pct":10.2},{"ticker":"TD","name":"TD Bank","pct":8.7},
        {"ticker":"SHOP","name":"Shopify","pct":6.4},{"ticker":"BAM","name":"Brookfield Asset","pct":5.1},
        {"ticker":"BNS","name":"Bank Nova Scotia","pct":4.8},{"ticker":"CNQ","name":"Canadian Nat Res","pct":4.5},
        {"ticker":"ENB","name":"Enbridge Inc","pct":4.2},{"ticker":"BMO","name":"BMO Financial","pct":4.0},
        {"ticker":"ABX","name":"Barrick Gold","pct":3.7},{"ticker":"MFC","name":"Manulife Financial","pct":3.4},
    ],
    "EWA": [
        {"ticker":"BHP","name":"BHP Group","pct":10.4},{"ticker":"CBA","name":"Commonwealth Bank","pct":9.8},
        {"ticker":"CSL","name":"CSL Limited","pct":7.6},{"ticker":"RIO","name":"Rio Tinto","pct":5.9},
        {"ticker":"ANZ","name":"ANZ Banking","pct":5.2},{"ticker":"NAB","name":"Natl Australia Bk","pct":5.0},
        {"ticker":"WBC","name":"Westpac Banking","pct":4.8},{"ticker":"MQG","name":"Macquarie Group","pct":4.5},
        {"ticker":"FMG","name":"Fortescue Metals","pct":4.2},{"ticker":"TCL","name":"Transurban Group","pct":3.9},
    ],
    "EWL": [
        {"ticker":"NESN","name":"Nestlé SA","pct":19.2},{"ticker":"NVS","name":"Novartis","pct":13.8},
        {"ticker":"RHHBY","name":"Roche Holding","pct":12.4},{"ticker":"ABB","name":"ABB Ltd","pct":7.1},
        {"ticker":"ZURN","name":"Zürich Insurance","pct":5.8},{"ticker":"CFR","name":"Richemont","pct":5.4},
        {"ticker":"LONN","name":"Lonza Group","pct":4.7},{"ticker":"SREN","name":"Swiss Re","pct":4.2},
        {"ticker":"GIVN","name":"Givaudan","pct":3.9},{"ticker":"HOLN","name":"Holcim Group","pct":3.5},
    ],
    "EWQ": [
        {"ticker":"MC","name":"LVMH","pct":12.4},{"ticker":"TTE","name":"TotalEnergies","pct":8.7},
        {"ticker":"RMS","name":"Hermès Intl","pct":7.9},{"ticker":"SAN","name":"Sanofi","pct":7.2},
        {"ticker":"AI","name":"Air Liquide","pct":5.4},{"ticker":"BNP","name":"BNP Paribas","pct":5.1},
        {"ticker":"AIR","name":"Airbus","pct":4.8},{"ticker":"CS","name":"AXA SA","pct":4.5},
        {"ticker":"SU","name":"Schneider Electric","pct":4.2},{"ticker":"STLA","name":"Stellantis","pct":3.8},
    ],
    "ARGT": [
        {"ticker":"MELI","name":"MercadoLibre","pct":24.1},{"ticker":"GLOB","name":"Globant","pct":18.3},
        {"ticker":"YPF","name":"YPF SA","pct":11.2},{"ticker":"BIOX","name":"Bioceres Crop","pct":6.4},
        {"ticker":"LOMA","name":"Loma Negra","pct":5.1},{"ticker":"BMA","name":"Cia Macro Banco","pct":4.8},
        {"ticker":"TEO","name":"Telecom Argentina","pct":4.5},{"ticker":"PAM","name":"Pampa Energia","pct":4.2},
        {"ticker":"GGAL","name":"Grupo Fin Galicia","pct":3.9},{"ticker":"CEPU","name":"Central Puerto","pct":3.5},
    ],
    "ECH": [
        {"ticker":"SQM","name":"SQM Sociedad","pct":14.8},{"ticker":"CMPC","name":"Empresas CMPC","pct":9.2},
        {"ticker":"ENELAM","name":"Enel Americas","pct":8.7},{"ticker":"FALABELLA","name":"SACI Falabella","pct":7.4},
        {"ticker":"BSANTANDER","name":"Bco Santander CL","pct":6.8},{"ticker":"CENCOSUD","name":"Cencosud SA","pct":5.8},
        {"ticker":"COLBUN","name":"Colbun SA","pct":5.2},{"ticker":"BCH","name":"Banco de Chile","pct":4.7},
        {"ticker":"LTM","name":"Latam Airlines","pct":4.1},{"ticker":"ANTO","name":"Antofagasta","pct":3.8},
    ],
    "EWW": [
        {"ticker":"WALMEX","name":"Walmart Mexico","pct":12.4},{"ticker":"AMX","name":"America Movil","pct":10.8},
        {"ticker":"FEM","name":"Fomento Económico","pct":9.7},{"ticker":"GFNORTE","name":"Banorte","pct":6.3},
        {"ticker":"GRUMA","name":"Gruma SAB","pct":5.1},{"ticker":"GMEXICOB","name":"Grupo Mexico","pct":4.8},
        {"ticker":"FIBRAMQ","name":"Fibra Uno","pct":4.5},{"ticker":"CEMEXCPO","name":"Cemex SAB","pct":4.2},
        {"ticker":"LIVEPOLC","name":"Liverpool","pct":3.9},{"ticker":"TLEVICPO","name":"Televisa Univ","pct":3.6},
    ],
}

# Fallback top-10 holdings for S&P Sector ETFs
SECTOR_ETF_FALLBACK_HOLDINGS = {
    "XLK": [
        {"ticker":"MSFT","name":"Microsoft","pct":22.1},{"ticker":"AAPL","name":"Apple Inc","pct":20.8},
        {"ticker":"NVDA","name":"NVIDIA","pct":9.2},{"ticker":"AVGO","name":"Broadcom","pct":5.1},
        {"ticker":"ORCL","name":"Oracle","pct":2.8},{"ticker":"CSCO","name":"Cisco Systems","pct":2.4},
        {"ticker":"ADBE","name":"Adobe Inc","pct":2.2},{"ticker":"AMD","name":"AMD","pct":2.0},
        {"ticker":"QCOM","name":"Qualcomm","pct":1.9},{"ticker":"TXN","name":"Texas Instruments","pct":1.8},
    ],
    "XLV": [
        {"ticker":"LLY","name":"Eli Lilly","pct":12.4},{"ticker":"UNH","name":"UnitedHealth","pct":11.8},
        {"ticker":"JNJ","name":"Johnson & Johnson","pct":7.2},{"ticker":"ABBV","name":"AbbVie","pct":6.9},
        {"ticker":"MRK","name":"Merck & Co","pct":5.8},{"ticker":"TMO","name":"Thermo Fisher","pct":4.2},
        {"ticker":"DHR","name":"Danaher Corp","pct":3.6},{"ticker":"ABT","name":"Abbott Labs","pct":3.4},
        {"ticker":"AMGN","name":"Amgen Inc","pct":3.2},{"ticker":"ISRG","name":"Intuitive Surgical","pct":2.9},
    ],
    "XLF": [
        {"ticker":"BRK.B","name":"Berkshire Hathaway","pct":12.8},{"ticker":"JPM","name":"JPMorgan Chase","pct":11.4},
        {"ticker":"V","name":"Visa Inc","pct":8.2},{"ticker":"MA","name":"Mastercard","pct":7.1},
        {"ticker":"BAC","name":"Bank of America","pct":5.4},{"ticker":"WFC","name":"Wells Fargo","pct":4.8},
        {"ticker":"GS","name":"Goldman Sachs","pct":3.2},{"ticker":"MS","name":"Morgan Stanley","pct":2.9},
        {"ticker":"SPGI","name":"S&P Global","pct":2.6},{"ticker":"BLK","name":"BlackRock","pct":2.4},
    ],
    "XLE": [
        {"ticker":"XOM","name":"Exxon Mobil","pct":22.4},{"ticker":"CVX","name":"Chevron Corp","pct":15.1},
        {"ticker":"COP","name":"ConocoPhillips","pct":7.2},{"ticker":"SLB","name":"SLB","pct":4.8},
        {"ticker":"EOG","name":"EOG Resources","pct":4.5},{"ticker":"MPC","name":"Marathon Petroleum","pct":3.9},
        {"ticker":"PSX","name":"Phillips 66","pct":3.6},{"ticker":"WMB","name":"Williams Cos","pct":3.2},
        {"ticker":"OKE","name":"ONEOK Inc","pct":3.0},{"ticker":"PXD","name":"Pioneer Natural Res","pct":2.8},
    ],
    "XLY": [
        {"ticker":"AMZN","name":"Amazon","pct":23.5},{"ticker":"TSLA","name":"Tesla","pct":16.8},
        {"ticker":"HD","name":"Home Depot","pct":8.4},{"ticker":"MCD","name":"McDonald's","pct":4.5},
        {"ticker":"BKNG","name":"Booking Holdings","pct":3.8},{"ticker":"LOW","name":"Lowe's Companies","pct":3.5},
        {"ticker":"NKE","name":"Nike Inc","pct":2.9},{"ticker":"SBUX","name":"Starbucks","pct":2.4},
        {"ticker":"TJX","name":"TJX Companies","pct":2.2},{"ticker":"CMG","name":"Chipotle Mexican","pct":2.0},
    ],
    "XLP": [
        {"ticker":"PG","name":"Procter & Gamble","pct":15.2},{"ticker":"COST","name":"Costco Wholesale","pct":13.4},
        {"ticker":"WMT","name":"Walmart Inc","pct":12.8},{"ticker":"KO","name":"Coca-Cola","pct":9.2},
        {"ticker":"PEP","name":"PepsiCo","pct":8.7},{"ticker":"MDLZ","name":"Mondelez Intl","pct":4.1},
        {"ticker":"PM","name":"Philip Morris","pct":3.8},{"ticker":"MO","name":"Altria Group","pct":3.2},
        {"ticker":"CL","name":"Colgate-Palmolive","pct":2.9},{"ticker":"GIS","name":"General Mills","pct":2.4},
    ],
    "XLI": [
        {"ticker":"CAT","name":"Caterpillar","pct":5.8},{"ticker":"RTX","name":"RTX Corp","pct":5.4},
        {"ticker":"HON","name":"Honeywell Intl","pct":5.2},{"ticker":"UNP","name":"Union Pacific","pct":4.8},
        {"ticker":"GE","name":"GE Aerospace","pct":4.5},{"ticker":"DE","name":"Deere & Company","pct":4.1},
        {"ticker":"UPS","name":"United Parcel Svc","pct":3.8},{"ticker":"ETN","name":"Eaton Corp","pct":3.5},
        {"ticker":"FDX","name":"FedEx Corp","pct":3.2},{"ticker":"WM","name":"Waste Management","pct":2.9},
    ],
    "XLB": [
        {"ticker":"LIN","name":"Linde plc","pct":18.4},{"ticker":"SHW","name":"Sherwin-Williams","pct":9.8},
        {"ticker":"APD","name":"Air Products","pct":8.2},{"ticker":"FCX","name":"Freeport-McMoRan","pct":5.4},
        {"ticker":"NEM","name":"Newmont Corp","pct":4.8},{"ticker":"NUE","name":"Nucor Corp","pct":4.2},
        {"ticker":"ALB","name":"Albemarle Corp","pct":3.5},{"ticker":"CE","name":"Celanese Corp","pct":3.1},
        {"ticker":"IFF","name":"Intl Flavors","pct":2.8},{"ticker":"MOS","name":"Mosaic Company","pct":2.6},
    ],
    "XLRE": [
        {"ticker":"AMT","name":"American Tower","pct":10.2},{"ticker":"PLD","name":"Prologis","pct":9.8},
        {"ticker":"EQIX","name":"Equinix Inc","pct":7.4},{"ticker":"CCI","name":"Crown Castle","pct":6.8},
        {"ticker":"SPG","name":"Simon Property","pct":6.2},{"ticker":"PSA","name":"Public Storage","pct":5.8},
        {"ticker":"WELL","name":"Welltower Inc","pct":5.4},{"ticker":"VTR","name":"Ventas Inc","pct":4.1},
        {"ticker":"EXR","name":"Extra Space Storage","pct":3.8},{"ticker":"DLR","name":"Digital Realty","pct":3.5},
    ],
    "XLU": [
        {"ticker":"NEE","name":"NextEra Energy","pct":15.4},{"ticker":"SO","name":"Southern Company","pct":8.2},
        {"ticker":"DUK","name":"Duke Energy","pct":7.8},{"ticker":"AEP","name":"Am. Electric Power","pct":5.4},
        {"ticker":"SRE","name":"Sempra Energy","pct":4.8},{"ticker":"EXC","name":"Exelon Corp","pct":4.5},
        {"ticker":"XEL","name":"Xcel Energy","pct":4.1},{"ticker":"ED","name":"Consolidated Edison","pct":3.8},
        {"ticker":"ES","name":"Eversource Energy","pct":3.5},{"ticker":"AWK","name":"Am. Water Works","pct":3.2},
    ],
    "XLC": [
        {"ticker":"META","name":"Meta Platforms","pct":22.8},{"ticker":"GOOGL","name":"Alphabet A","pct":17.4},
        {"ticker":"GOOG","name":"Alphabet C","pct":14.2},{"ticker":"NFLX","name":"Netflix","pct":8.1},
        {"ticker":"CHTR","name":"Charter Comm","pct":3.8},{"ticker":"DIS","name":"Walt Disney","pct":3.5},
        {"ticker":"T","name":"AT&T Inc","pct":3.2},{"ticker":"VZ","name":"Verizon Comm","pct":2.9},
        {"ticker":"CMCSA","name":"Comcast Corp","pct":2.6},{"ticker":"EA","name":"Electronic Arts","pct":2.2},
    ],
}

# Fallback top-10 holdings for Theme ETFs
THEME_ETF_FALLBACK_HOLDINGS = {
    "SMH": [
        {"ticker":"NVDA","name":"NVIDIA","pct":20.1},{"ticker":"TSM","name":"TSMC (ADR)","pct":14.8},
        {"ticker":"AVGO","name":"Broadcom","pct":8.2},{"ticker":"ASML","name":"ASML Holding","pct":5.4},
        {"ticker":"QCOM","name":"Qualcomm","pct":4.9},{"ticker":"TXN","name":"Texas Instruments","pct":4.6},
        {"ticker":"AMD","name":"AMD","pct":4.2},{"ticker":"AMAT","name":"Applied Materials","pct":4.0},
        {"ticker":"LRCX","name":"Lam Research","pct":3.8},{"ticker":"MU","name":"Micron Technology","pct":3.5},
    ],
    "SOXX": [
        {"ticker":"NVDA","name":"NVIDIA","pct":10.4},{"ticker":"AVGO","name":"Broadcom","pct":9.8},
        {"ticker":"AMD","name":"AMD","pct":8.2},{"ticker":"QCOM","name":"Qualcomm","pct":7.4},
        {"ticker":"AMAT","name":"Applied Materials","pct":6.8},{"ticker":"LRCX","name":"Lam Research","pct":6.4},
        {"ticker":"TXN","name":"Texas Instruments","pct":6.1},{"ticker":"KLAC","name":"KLA Corp","pct":5.8},
        {"ticker":"INTC","name":"Intel Corp","pct":5.2},{"ticker":"MU","name":"Micron Technology","pct":4.9},
    ],
    "HACK": [
        {"ticker":"PANW","name":"Palo Alto Networks","pct":8.4},{"ticker":"CRWD","name":"CrowdStrike","pct":7.8},
        {"ticker":"FTNT","name":"Fortinet","pct":6.5},{"ticker":"ZS","name":"Zscaler","pct":5.9},
        {"ticker":"MSFT","name":"Microsoft","pct":5.2},{"ticker":"CHKP","name":"Check Point Sftwr","pct":4.8},
        {"ticker":"OKTA","name":"Okta Inc","pct":4.2},{"ticker":"CYBR","name":"CyberArk Software","pct":3.9},
        {"ticker":"S","name":"SentinelOne","pct":3.5},{"ticker":"TENB","name":"Tenable Holdings","pct":3.1},
    ],
    "CLOU": [
        {"ticker":"MSFT","name":"Microsoft","pct":7.8},{"ticker":"AMZN","name":"Amazon","pct":6.4},
        {"ticker":"GOOGL","name":"Alphabet","pct":5.9},{"ticker":"ORCL","name":"Oracle","pct":5.2},
        {"ticker":"CRM","name":"Salesforce","pct":4.8},{"ticker":"SNOW","name":"Snowflake","pct":4.4},
        {"ticker":"NET","name":"Cloudflare","pct":4.1},{"ticker":"DDOG","name":"Datadog","pct":3.8},
        {"ticker":"CFLT","name":"Confluent","pct":3.2},{"ticker":"TWLO","name":"Twilio Inc","pct":2.9},
    ],
    "AIQ": [
        {"ticker":"NVDA","name":"NVIDIA","pct":9.2},{"ticker":"MSFT","name":"Microsoft","pct":8.4},
        {"ticker":"GOOGL","name":"Alphabet","pct":6.8},{"ticker":"META","name":"Meta Platforms","pct":5.9},
        {"ticker":"AMZN","name":"Amazon","pct":5.4},{"ticker":"BIDU","name":"Baidu","pct":4.2},
        {"ticker":"BABA","name":"Alibaba","pct":3.8},{"ticker":"IBM","name":"IBM Corp","pct":3.5},
        {"ticker":"ORCL","name":"Oracle","pct":3.2},{"ticker":"SAP","name":"SAP SE","pct":2.9},
    ],
    "BOTZ": [
        {"ticker":"NVDA","name":"NVIDIA","pct":8.4},{"ticker":"ISRG","name":"Intuitive Surgical","pct":6.2},
        {"ticker":"ABB","name":"ABB Ltd","pct":5.8},{"ticker":"HON","name":"Honeywell Intl","pct":5.1},
        {"ticker":"ZBRA","name":"Zebra Technologies","pct":4.8},{"ticker":"OMCL","name":"Omnicell Inc","pct":4.2},
        {"ticker":"AMBA","name":"Ambarella Inc","pct":3.9},{"ticker":"BLDP","name":"Ballard Power","pct":3.5},
        {"ticker":"MASI","name":"Masimo Corp","pct":3.2},{"ticker":"IRBT","name":"iRobot Corp","pct":2.8},
    ],
    "XBI": [
        {"ticker":"MRNA","name":"Moderna Inc","pct":5.2},{"ticker":"BNTX","name":"BioNTech SE","pct":4.8},
        {"ticker":"ALNY","name":"Alnylam Pharma","pct":4.4},{"ticker":"VRTX","name":"Vertex Pharma","pct":4.1},
        {"ticker":"REGN","name":"Regeneron","pct":3.8},{"ticker":"BIIB","name":"Biogen Inc","pct":3.5},
        {"ticker":"GILD","name":"Gilead Sciences","pct":3.2},{"ticker":"BMRN","name":"BioMarin Pharma","pct":3.0},
        {"ticker":"SRPT","name":"Sarepta Therapeutics","pct":2.8},{"ticker":"RARE","name":"Ultragenyx","pct":2.6},
    ],
    "IBB": [
        {"ticker":"AMGN","name":"Amgen Inc","pct":9.8},{"ticker":"GILD","name":"Gilead Sciences","pct":8.2},
        {"ticker":"VRTX","name":"Vertex Pharma","pct":7.4},{"ticker":"REGN","name":"Regeneron","pct":6.8},
        {"ticker":"BIIB","name":"Biogen Inc","pct":6.2},{"ticker":"BNTX","name":"BioNTech SE","pct":5.8},
        {"ticker":"MRNA","name":"Moderna Inc","pct":5.4},{"ticker":"ABBV","name":"AbbVie","pct":4.9},
        {"ticker":"BMRN","name":"BioMarin Pharma","pct":4.2},{"ticker":"ALNY","name":"Alnylam Pharma","pct":3.8},
    ],
    "IHI": [
        {"ticker":"MDT","name":"Medtronic","pct":9.8},{"ticker":"ABT","name":"Abbott Labs","pct":9.2},
        {"ticker":"ISRG","name":"Intuitive Surgical","pct":8.4},{"ticker":"BSX","name":"Boston Scientific","pct":7.8},
        {"ticker":"EW","name":"Edwards Lifesciences","pct":5.4},{"ticker":"ZBH","name":"Zimmer Biomet","pct":4.8},
        {"ticker":"HOLX","name":"Hologic Inc","pct":4.2},{"ticker":"DXCM","name":"DexCom Inc","pct":3.9},
        {"ticker":"PODD","name":"Insulet Corp","pct":3.5},{"ticker":"IDXX","name":"IDEXX Labs","pct":3.2},
    ],
    "KRE": [
        {"ticker":"CFG","name":"Citizens Financial","pct":4.8},{"ticker":"HBAN","name":"Huntington Bancshares","pct":4.2},
        {"ticker":"RF","name":"Regions Financial","pct":3.9},{"ticker":"KEY","name":"KeyCorp","pct":3.7},
        {"ticker":"FITB","name":"Fifth Third Bancorp","pct":3.5},{"ticker":"ZION","name":"Zions Bancorporation","pct":3.2},
        {"ticker":"CMA","name":"Comerica Inc","pct":3.0},{"ticker":"MTB","name":"M&T Bank Corp","pct":2.9},
        {"ticker":"SNV","name":"Synovus Financial","pct":2.7},{"ticker":"BOKF","name":"BOK Financial","pct":2.5},
    ],
    "KBE": [
        {"ticker":"JPM","name":"JPMorgan Chase","pct":10.2},{"ticker":"BAC","name":"Bank of America","pct":8.8},
        {"ticker":"WFC","name":"Wells Fargo","pct":7.4},{"ticker":"GS","name":"Goldman Sachs","pct":6.2},
        {"ticker":"MS","name":"Morgan Stanley","pct":5.8},{"ticker":"C","name":"Citigroup","pct":5.4},
        {"ticker":"USB","name":"U.S. Bancorp","pct":4.9},{"ticker":"TFC","name":"Truist Financial","pct":4.5},
        {"ticker":"SCHW","name":"Charles Schwab","pct":4.2},{"ticker":"BK","name":"Bank of NY Mellon","pct":3.8},
    ],
    "KIE": [
        {"ticker":"BRK.B","name":"Berkshire Hathaway","pct":8.4},{"ticker":"MET","name":"MetLife Inc","pct":5.8},
        {"ticker":"PRU","name":"Prudential Financial","pct":5.4},{"ticker":"AIG","name":"AIG","pct":4.9},
        {"ticker":"AFL","name":"Aflac Inc","pct":4.5},{"ticker":"ALL","name":"Allstate Corp","pct":4.2},
        {"ticker":"CB","name":"Chubb Ltd","pct":3.9},{"ticker":"HIG","name":"Hartford Financial","pct":3.5},
        {"ticker":"LNC","name":"Lincoln National","pct":3.2},{"ticker":"UNM","name":"Unum Group","pct":2.9},
    ],
    "XOP": [
        {"ticker":"EOG","name":"EOG Resources","pct":8.4},{"ticker":"DVN","name":"Devon Energy","pct":7.2},
        {"ticker":"COP","name":"ConocoPhillips","pct":6.8},{"ticker":"MRO","name":"Marathon Oil","pct":5.8},
        {"ticker":"HES","name":"Hess Corp","pct":5.4},{"ticker":"APA","name":"APA Corp","pct":4.9},
        {"ticker":"FANG","name":"Diamondback Energy","pct":4.5},{"ticker":"XOM","name":"Exxon Mobil","pct":4.2},
        {"ticker":"CVX","name":"Chevron Corp","pct":3.8},{"ticker":"OVV","name":"Ovintiv Inc","pct":3.4},
    ],
    "OIH": [
        {"ticker":"HAL","name":"Halliburton","pct":14.8},{"ticker":"SLB","name":"SLB","pct":13.4},
        {"ticker":"BKR","name":"Baker Hughes","pct":11.2},{"ticker":"NOV","name":"NOV Inc","pct":6.8},
        {"ticker":"HP","name":"Helmerich & Payne","pct":5.4},{"ticker":"FTI","name":"TechnipFMC","pct":5.1},
        {"ticker":"RIG","name":"Transocean Ltd","pct":4.8},{"ticker":"WTTR","name":"Select Water Soln","pct":4.2},
        {"ticker":"PTEN","name":"Patterson-UTI","pct":3.9},{"ticker":"NR","name":"Newpark Resources","pct":3.5},
    ],
    "TAN": [
        {"ticker":"ENPH","name":"Enphase Energy","pct":12.4},{"ticker":"FSLR","name":"First Solar","pct":10.8},
        {"ticker":"SEDG","name":"SolarEdge Tech","pct":8.2},{"ticker":"SPWR","name":"SunPower Corp","pct":6.4},
        {"ticker":"RUN","name":"Sunrun Inc","pct":5.8},{"ticker":"CSIQ","name":"Canadian Solar","pct":5.4},
        {"ticker":"JKS","name":"JinkoSolar Holding","pct":5.1},{"ticker":"MAXN","name":"Maxeon Solar","pct":4.5},
        {"ticker":"NEE","name":"NextEra Energy","pct":4.2},{"ticker":"AES","name":"AES Corp","pct":3.8},
    ],
    "ICLN": [
        {"ticker":"PLUG","name":"Plug Power","pct":6.8},{"ticker":"FSLR","name":"First Solar","pct":6.4},
        {"ticker":"ENPH","name":"Enphase Energy","pct":5.9},{"ticker":"NEE","name":"NextEra Energy","pct":5.4},
        {"ticker":"CWEN","name":"Clearway Energy","pct":4.4},{"ticker":"RUN","name":"Sunrun Inc","pct":4.1},
        {"ticker":"BEP","name":"Brookfield Renew","pct":3.8},{"ticker":"SEDG","name":"SolarEdge Tech","pct":3.4},
        {"ticker":"NOVA","name":"Sunnova Energy","pct":3.1},{"ticker":"ORA","name":"Ormat Technologies","pct":2.8},
    ],
    "XAR": [
        {"ticker":"RTX","name":"RTX Corp","pct":14.8},{"ticker":"LMT","name":"Lockheed Martin","pct":13.4},
        {"ticker":"NOC","name":"Northrop Grumman","pct":10.8},{"ticker":"GD","name":"General Dynamics","pct":9.2},
        {"ticker":"TDG","name":"TransDigm Group","pct":6.4},{"ticker":"HEICO","name":"HEICO Corp","pct":5.8},
        {"ticker":"LDOS","name":"Leidos Holdings","pct":5.4},{"ticker":"SAIC","name":"Science Apps Intl","pct":4.9},
        {"ticker":"HII","name":"Huntington Ingalls","pct":4.5},{"ticker":"LHX","name":"L3Harris Tech","pct":4.2},
    ],
    "PAVE": [
        {"ticker":"BLDR","name":"Builders FirstSource","pct":5.8},{"ticker":"FAST","name":"Fastenal Co","pct":5.4},
        {"ticker":"MLM","name":"Martin Marietta","pct":5.1},{"ticker":"VMC","name":"Vulcan Materials","pct":4.8},
        {"ticker":"URI","name":"United Rentals","pct":4.5},{"ticker":"PWR","name":"Quanta Services","pct":4.2},
        {"ticker":"AECOM","name":"AECOM","pct":3.9},{"ticker":"MAS","name":"Masco Corp","pct":3.5},
        {"ticker":"NUE","name":"Nucor Corp","pct":3.2},{"ticker":"CARR","name":"Carrier Global","pct":2.9},
    ],
    "XRT": [
        {"ticker":"AMZN","name":"Amazon","pct":7.4},{"ticker":"WMT","name":"Walmart Inc","pct":6.8},
        {"ticker":"TGT","name":"Target Corp","pct":5.9},{"ticker":"COST","name":"Costco Wholesale","pct":5.4},
        {"ticker":"HD","name":"Home Depot","pct":4.9},{"ticker":"LOW","name":"Lowe's Companies","pct":4.5},
        {"ticker":"NKE","name":"Nike Inc","pct":4.1},{"ticker":"LULU","name":"Lululemon","pct":3.8},
        {"ticker":"DKNG","name":"DraftKings","pct":3.2},{"ticker":"FIVE","name":"Five Below","pct":2.9},
    ],
    "XHB": [
        {"ticker":"DHI","name":"D.R. Horton","pct":14.8},{"ticker":"LEN","name":"Lennar Corp","pct":13.2},
        {"ticker":"PHM","name":"PulteGroup","pct":10.8},{"ticker":"NVR","name":"NVR Inc","pct":9.4},
        {"ticker":"TOL","name":"Toll Brothers","pct":7.8},{"ticker":"MDC","name":"MDC Holdings","pct":5.4},
        {"ticker":"MHO","name":"M/I Homes","pct":4.8},{"ticker":"CCS","name":"Century Communities","pct":4.2},
        {"ticker":"IBP","name":"Installed Building","pct":3.8},{"ticker":"BLD","name":"TopBuild Corp","pct":3.5},
    ],
    "COPX": [
        {"ticker":"FCX","name":"Freeport-McMoRan","pct":14.8},{"ticker":"SCCO","name":"Southern Copper","pct":12.4},
        {"ticker":"HBM","name":"Hudbay Minerals","pct":6.8},{"ticker":"ERO","name":"Ero Copper","pct":5.4},
        {"ticker":"TECK","name":"Teck Resources","pct":4.9},{"ticker":"IVN","name":"Ivanhoe Mines","pct":4.5},
        {"ticker":"CMMC","name":"Capstone Copper","pct":4.2},{"ticker":"TRQ","name":"Turquoise Hill","pct":3.8},
        {"ticker":"GGB","name":"Gerdau SA","pct":3.4},{"ticker":"CS","name":"Capstone Mining","pct":3.1},
    ],
    "GDX": [
        {"ticker":"NEM","name":"Newmont Corp","pct":14.2},{"ticker":"GOLD","name":"Barrick Gold","pct":12.8},
        {"ticker":"AEM","name":"Agnico Eagle","pct":10.4},{"ticker":"WPM","name":"Wheaton Precious","pct":7.8},
        {"ticker":"KGC","name":"Kinross Gold","pct":5.4},{"ticker":"FNV","name":"Franco-Nevada","pct":5.1},
        {"ticker":"PAAS","name":"Pan American Silver","pct":4.8},{"ticker":"HMY","name":"Harmony Gold","pct":4.2},
        {"ticker":"AU","name":"AngloGold Ashanti","pct":3.9},{"ticker":"AGI","name":"Alamos Gold","pct":3.5},
    ],
    "SLX": [
        {"ticker":"NUE","name":"Nucor Corp","pct":18.4},{"ticker":"STLD","name":"Steel Dynamics","pct":14.8},
        {"ticker":"X","name":"U.S. Steel","pct":10.2},{"ticker":"CLF","name":"Cleveland-Cliffs","pct":8.4},
        {"ticker":"MT","name":"ArcelorMittal","pct":6.8},{"ticker":"TS","name":"Tenaris SA","pct":5.4},
        {"ticker":"WOR","name":"Worthington Inds","pct":4.9},{"ticker":"GGB","name":"Gerdau SA","pct":4.5},
        {"ticker":"CRS","name":"Carpenter Technology","pct":4.1},{"ticker":"ATI","name":"ATI Inc","pct":3.7},
    ],
    "ARKK": [
        {"ticker":"TSLA","name":"Tesla","pct":10.8},{"ticker":"CRSP","name":"CRISPR Therapeutics","pct":8.4},
        {"ticker":"COIN","name":"Coinbase","pct":7.2},{"ticker":"ROKU","name":"Roku Inc","pct":6.8},
        {"ticker":"RBLX","name":"Roblox Corp","pct":5.9},{"ticker":"SQ","name":"Block Inc","pct":5.4},
        {"ticker":"DKNG","name":"DraftKings","pct":4.8},{"ticker":"EXAS","name":"Exact Sciences","pct":4.2},
        {"ticker":"PATH","name":"UiPath Inc","pct":3.9},{"ticker":"RXRX","name":"Recursion Pharma","pct":3.5},
    ],
    "ARKG": [
        {"ticker":"CRSP","name":"CRISPR Therapeutics","pct":9.8},{"ticker":"NVTA","name":"Invitae Corp","pct":7.4},
        {"ticker":"TDOC","name":"Teladoc Health","pct":6.8},{"ticker":"FATE","name":"Fate Therapeutics","pct":6.2},
        {"ticker":"RXRX","name":"Recursion Pharma","pct":5.8},{"ticker":"BEAM","name":"Beam Therapeutics","pct":5.4},
        {"ticker":"TWST","name":"Twist Bioscience","pct":4.9},{"ticker":"VCYT","name":"Veracyte Inc","pct":4.5},
        {"ticker":"ACMR","name":"ACM Research","pct":4.1},{"ticker":"SEER","name":"Seer Inc","pct":3.7},
    ],
    "ARKW": [
        {"ticker":"TSLA","name":"Tesla","pct":12.4},{"ticker":"COIN","name":"Coinbase","pct":9.8},
        {"ticker":"SQ","name":"Block Inc","pct":8.4},{"ticker":"DKNG","name":"DraftKings","pct":6.8},
        {"ticker":"ROKU","name":"Roku Inc","pct":6.2},{"ticker":"HOOD","name":"Robinhood Markets","pct":5.4},
        {"ticker":"SPOT","name":"Spotify Technology","pct":4.8},{"ticker":"Z","name":"Zillow Group","pct":4.2},
        {"ticker":"OPEN","name":"Opendoor Tech","pct":3.8},{"ticker":"JD","name":"JD.com","pct":3.4},
    ],
}

BREADTH_TICKERS = {
    "^VIX": "VIX Fear Index",
    "HYG":  "High Yield Bonds",
    "LQD":  "Inv Grade Corp",
    "TLT":  "20Y T-Bond",
    "RSP":  "Equal Wt S&P500",
}


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
    """
    Fetch live market context via yfinance batch download.

    Uses dual-interval approach:
      - 5-min bars  (period="1d", interval="5m")  → current price, lower latency
      - Daily bars  (period="5d", interval="1d")   → previous close for accurate daily %
      → change = (5m_curr - daily_prev_close) / daily_prev_close × 100

    Note: TradingView screener was NOT used here because it filters out ETFs
    (it only returns equities), so SPY/QQQ/XLK would always return empty.
    """
    import yfinance as yf
    result = {}

    # ── Batch download: SPY/QQQ/IWM + all sector ETFs ────────────────────────
    etf_syms = MARKET_TICKERS + list(SECTOR_ETFS.keys())
    try:
        # Download 1: daily bars for previous close + SMA50
        raw_d = yf.download(
            etf_syms, period="5d", interval="1d",
            progress=False, auto_adjust=True, threads=True
        )
        # Download 2: 5-min bars for fresh current price
        raw_5m = yf.download(
            etf_syms, period="1d", interval="5m",
            progress=False, auto_adjust=True, threads=True
        )
        multi = len(etf_syms) > 1
        for sym in etf_syms:
            try:
                closes_d  = (raw_d["Close"][sym]  if multi else raw_d["Close"]).dropna()
                closes_5m = (raw_5m["Close"][sym] if multi else raw_5m["Close"]).dropna()
                if len(closes_d) < 2:
                    continue
                prev  = float(closes_d.iloc[-2])        # Previous session close
                # Use 5-min bar if available (fresher), else fall back to daily partial bar
                curr  = float(closes_5m.iloc[-1]) if not closes_5m.empty else float(closes_d.iloc[-1])
                sma50 = float(closes_d.tail(50).mean()) if len(closes_d) >= 50 else None
                chg   = (curr - prev) / prev * 100 if prev else 0.0
                result[sym] = {
                    "price":      round(curr, 2),
                    "change":     round(chg, 2),
                    "perf_1m":    0.0,
                    "above_50ma": (curr > sma50) if sma50 else None,
                    "label":      SECTOR_ETFS.get(sym, sym),
                }
            except Exception:
                pass
        print(f"  → yfinance 5m+1d dual: {len(result)} ETF/index tickers", file=sys.stderr)
    except Exception as e:
        print(f"  ETF/index batch download failed: {e}", file=sys.stderr)

    # ── VIX + commodity futures/crypto (fast_info is fine for these) ─────────
    for sym in ["^VIX"] + list(COMMODITY_TICKERS.keys()):
        key = sym.replace("^", "")
        try:
            fi    = yf.Ticker(sym).fast_info
            price = getattr(fi, "last_price", None)
            prev  = getattr(fi, "previous_close", None)
            chg   = ((price - prev) / prev * 100) if (price and prev) else 0.0
            result[key] = {
                "price":      round(float(price), 2) if price else None,
                "change":     round(chg, 2),
                "perf_1m":    0.0,
                "above_50ma": None,
                "label":      COMMODITY_TICKERS.get(sym, key),
            }
        except Exception:
            pass

    print(f"  → {len(result)} total market/sector tickers loaded", file=sys.stderr)
    return result


def fetch_market_highs_lows():
    """
    Query TradingView Screener for ALL US stocks to compute market-wide
    52-week high/low breadth.  Returns dict with hi_by_sec, lo_by_sec,
    hi_count, lo_count — or None on failure.
    """
    try:
        print("  Fetching market 52w high/low breadth (all US stocks)...", file=sys.stderr)
        _, df = (
            Query()
            .select("name", "close", "High.All", "Low.All", "sector")
            .set_markets("america")
            .limit(10000)
            .get_scanner_data()
        )
        if df.empty:
            return None
        highs_by_sec = {}
        lows_by_sec  = {}
        for _, row in df.iterrows():
            try:
                price = float(row.get("close") or 0)
                h52   = float(row.get("High.All") or 0)
                l52   = float(row.get("Low.All")  or 0)
                sec   = str(row.get("sector") or "Other").strip() or "Other"
                tkr   = str(row.get("name") or "")
                if not (price and tkr):
                    continue
                if h52 and price >= h52 * 0.97:
                    highs_by_sec.setdefault(sec, []).append(tkr)
                if l52 and price <= l52 * 1.03:
                    lows_by_sec.setdefault(sec, []).append(tkr)
            except Exception:
                pass
        hi_count = sum(len(v) for v in highs_by_sec.values())
        lo_count = sum(len(v) for v in lows_by_sec.values())
        print(f"    -> {hi_count} near highs, {lo_count} near lows across {len(df)} stocks",
              file=sys.stderr)
        return {
            "hi_by_sec": highs_by_sec,
            "lo_by_sec": lows_by_sec,
            "hi_count":  hi_count,
            "lo_count":  lo_count,
        }
    except Exception as e:
        print(f"  ⚠ fetch_market_highs_lows failed: {e}", file=sys.stderr)
        return None


def fetch_market_pulse_data():
    """
    Download 1-year daily data for all Market Pulse ETFs.
    Returns dict: sym -> {label, price, today, 1w, 1m, 3m, 6m, ytd}
    Trading-day approx: 5=1W, 21=1M, 63=3M, 126=6M.
    """
    import yfinance as yf
    import pandas as pd
    from datetime import datetime

    _etf_syms = list(dict.fromkeys(
        list(THEME_ETFS.keys()) + list(SECTOR_ETFS.keys()) + list(COUNTRY_ETFS.keys())
        + list(BREADTH_TICKERS.keys())
    ))
    label_map = {**THEME_ETFS, **SECTOR_ETFS, **COUNTRY_ETFS, **BREADTH_TICKERS}
    # Collect holding tickers for live sub-row data (Theme + Sector fallback dicts)
    _hold_tkrs = set()
    for _hl in list(THEME_ETF_FALLBACK_HOLDINGS.values()) + list(SECTOR_ETF_FALLBACK_HOLDINGS.values()):
        for _h in _hl:
            _hold_tkrs.add(_h["ticker"])
    _hold_tkrs -= set(_etf_syms)
    all_syms = list(dict.fromkeys(_etf_syms + sorted(_hold_tkrs)))
    result = {}
    try:
        print(f"  Market Pulse: downloading {len(all_syms)} syms ({len(_etf_syms)} ETFs + {len(_hold_tkrs)} holdings, 1Y daily)...", file=sys.stderr)
        raw = yf.download(all_syms, period="1y", interval="1d",
                          progress=False, auto_adjust=True, threads=True)
        multi  = len(all_syms) > 1
        ytd_ts = pd.Timestamp(datetime.now().year, 1, 1)
        for sym in _etf_syms:
            try:
                closes = (raw["Close"][sym] if multi else raw["Close"]).dropna()
                if len(closes) < 2:
                    continue
                curr = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                def _td(n, _c=closes, _curr=curr):
                    i = max(0, len(_c) - 1 - n)
                    p = float(_c.iloc[i])
                    return round((_curr - p) / p * 100, 2) if p else None
                ytd_s = closes[closes.index >= ytd_ts]
                ytd_p = float(ytd_s.iloc[0]) if not ytd_s.empty else None
                ytd   = round((curr - ytd_p) / ytd_p * 100, 2) if ytd_p else None
                _op = (raw["Open"][sym] if multi else raw["Open"]).dropna()
                _op_t = float(_op.iloc[-1]) if not _op.empty else None
                _chg_op = round((curr - _op_t) / _op_t * 100, 2) if _op_t else None
                result[sym] = {
                    "label": label_map.get(sym, sym),
                    "price": round(curr, 2),
                    "chg_open": _chg_op,
                    "today": round((curr - prev) / prev * 100, 2) if prev else 0.0,
                    "1w": _td(5), "1m": _td(21), "3m": _td(63), "6m": _td(126), "ytd": ytd,
                }
            except Exception:
                pass
        print(f"    -> {len(result)} ETFs loaded for Market Pulse", file=sys.stderr)
        # Compute live data for individual holding tickers (same raw download)
        _hl_live = {}
        for sym in _hold_tkrs:
            try:
                _hc = (raw["Close"][sym] if multi else raw["Close"]).dropna()
                _ho = (raw["Open"][sym]  if multi else raw["Open"]).dropna()
                if len(_hc) < 2:
                    continue
                _hcurr = float(_hc.iloc[-1])
                _hprev = float(_hc.iloc[-2])
                _hop_t = float(_ho.iloc[-1]) if not _ho.empty else None
                _hchg_op = round((_hcurr - _hop_t) / _hop_t * 100, 2) if _hop_t else None
                def _htd(n, _c=_hc, _cv=_hcurr):
                    _i = max(0, len(_c) - 1 - n)
                    _p = float(_c.iloc[_i])
                    return round((_cv - _p) / _p * 100, 2) if _p else None
                _hytd_s = _hc[_hc.index >= ytd_ts]
                _hytd_p = float(_hytd_s.iloc[0]) if not _hytd_s.empty else None
                _hytd   = round((_hcurr - _hytd_p) / _hytd_p * 100, 2) if _hytd_p else None
                _hl_live[sym] = {
                    "price":    round(_hcurr, 2),
                    "chg_open": _hchg_op,
                    "today":    round((_hcurr - _hprev) / _hprev * 100, 2) if _hprev else 0.0,
                    "1w": _htd(5), "1m": _htd(21), "3m": _htd(63), "6m": _htd(126), "ytd": _hytd,
                }
            except Exception:
                pass
        result["_holdings_live"] = _hl_live
        print(f"    -> {len(_hl_live)}/{len(_hold_tkrs)} holding tickers enriched", file=sys.stderr)
    except Exception as e:
        print(f"  Market Pulse download failed: {e}", file=sys.stderr)
    # Fetch ETF holdings for Country ETFs tab
    try:
        print("  Fetching ETF holdings for Country ETFs...", file=sys.stderr)
        result["_holdings"] = fetch_etf_holdings_batch(list(COUNTRY_ETFS.keys()))
        _n = len(result["_holdings"])
        print(f"    -> holdings for {_n} ETFs loaded", file=sys.stderr)
    except Exception as _he:
        print(f"  Holdings fetch skipped: {_he}", file=sys.stderr)
        result["_holdings"] = {}
    # Fetch market-wide 52w highs/lows breadth via TV Screener
    result["_market_hl"] = fetch_market_highs_lows()
    return result


def fetch_etf_holdings_batch(etf_syms):
    """
    Fetch top-10 holdings for a list of ETFs.
    Tries multiple yfinance approaches for robustness in CI environments.
    Returns: {sym: [{ticker, name, pct, price, today}, ...]}
    """
    import yfinance as yf
    import concurrent.futures
    result = {}

    def _one(sym):
        rows = []
        tk = yf.Ticker(sym)

        # Method 1: funds_data.top_holdings (yfinance 0.2.28+)
        try:
            fd = tk.funds_data
            if fd is not None:
                th = fd.top_holdings
                if th is not None and hasattr(th, "empty") and not th.empty:
                    for idx, row in th.head(10).iterrows():
                        name = str(row.get("Name") or row.get("name") or str(idx))[:38]
                        pct_raw = row.get("Holding Percent") or row.get("holdingPercent") or 0
                        rows.append({
                            "ticker": str(idx),
                            "name": name,
                            "pct": round(float(pct_raw or 0) * 100, 2),
                            "price": None, "today": None,
                        })
                    if rows:
                        return sym, rows
        except Exception:
            pass

        # Method 2: info["holdings"] (older yfinance / fallback)
        try:
            info = tk.info
            raw_h = info.get("holdings") or []
            if raw_h:
                for h in raw_h[:10]:
                    tkr  = h.get("symbol") or h.get("ticker") or ""
                    name = h.get("holdingName") or h.get("name") or tkr
                    pct  = h.get("holdingPercent") or h.get("pct") or 0
                    if tkr:
                        rows.append({
                            "ticker": str(tkr),
                            "name": str(name)[:38],
                            "pct": round(float(pct or 0) * 100, 2),
                            "price": None, "today": None,
                        })
                if rows:
                    return sym, rows
        except Exception:
            pass

        return sym, []

    print(f"  Fetching holdings for {len(etf_syms)} ETFs...", file=sys.stderr)
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_one, s): s for s in etf_syms}
        for fut in concurrent.futures.as_completed(futs):
            sym, rows = fut.result()
            if rows:
                result[sym] = rows
                ok += 1
    print(f"  Holdings loaded for {ok}/{len(etf_syms)} ETFs", file=sys.stderr)

    # Fallback: use static known holdings for ETFs that returned empty
    fallback_used = 0
    for sym in etf_syms:
        if sym not in result and sym in COUNTRY_ETF_FALLBACK_HOLDINGS:
            result[sym] = [dict(h) for h in COUNTRY_ETF_FALLBACK_HOLDINGS[sym]]
            # Mark as cached so UI can show a note
            for h in result[sym]:
                h.setdefault("cached", True)
            fallback_used += 1
    if fallback_used:
        print(f"  Holdings fallback used for {fallback_used} ETFs", file=sys.stderr)

    # Batch-fetch today's price & perf for all unique holding tickers
    all_tickers = list({h["ticker"] for rows in result.values() for h in rows})
    if all_tickers:
        try:
            hd = yf.download(all_tickers, period="2d", interval="1d",
                             progress=False, auto_adjust=True, threads=True)
            multi = len(all_tickers) > 1
            price_map = {}
            for tk in all_tickers:
                try:
                    col = hd["Close"][tk] if multi else hd["Close"]
                    closes = col.dropna()
                    if len(closes) >= 2:
                        c = float(closes.iloc[-1]); p = float(closes.iloc[-2])
                        price_map[tk] = {"price": round(c,2),
                                         "today": round((c-p)/p*100,2) if p else None}
                except Exception:
                    pass
            for rows in result.values():
                for h in rows:
                    h.update(price_map.get(h["ticker"], {}))
            print(f"  Holdings prices fetched for {len(price_map)} tickers", file=sys.stderr)
        except Exception as e:
            print(f"  Holdings price fetch failed: {e}", file=sys.stderr)

    return result


_SENT_POS = {
    "beat","beats","surge","surges","surged","rally","rallies","rallied","upgrade","upgraded",
    "outperform","buy","strong","record","profit","profits","growth","grew","rise","rises","rose",
    "gain","gains","gains","positive","bullish","boost","boosts","boosted","higher","upside",
    "exceed","exceeds","exceeded","raise","raises","raised","guidance","dividend","buyback",
    "breakout","momentum","accelerate","accelerates","accelerated","recovery","recovers","recovered",
}
_SENT_NEG = {
    "miss","misses","missed","drop","drops","dropped","fall","falls","fell","decline","declines",
    "declined","downgrade","downgraded","underperform","sell","weak","loss","losses","lower","downside",
    "cut","cuts","cutting","reduce","reduces","reduced","concern","concerns","risk","risks",
    "warn","warns","warned","warning","bearish","disappoint","disappoints","disappointed","negative",
    "layoff","layoffs","lawsuit","investigation","recall","fraud","crash","crashes","crashed",
}

def _news_sentiment(title):
    """Return 1 (positive) / -1 (negative) / 0 (neutral) based on keywords."""
    words = set(title.lower().replace(",","").replace(".","").replace("!","").split())
    pos = len(words & _SENT_POS)
    neg = len(words & _SENT_NEG)
    if pos > neg:   return 1
    if neg > pos:   return -1
    return 0

def _fetch_news_rss(sym):
    """Fetch up to 3 headlines. 3 methods: YF v1 JSON API, yfinance.Ticker.news, Google RSS."""
    import sys
    from datetime import datetime
    import urllib.request, xml.etree.ElementTree as ET

    # Method 1: Yahoo Finance v1 search JSON (same host yfinance uses for market data)
    try:
        import json
        url = ("https://query1.finance.yahoo.com/v1/finance/search"
               "?q=%s&quotesCount=0&newsCount=5&enableFuzzyQuery=false" % sym)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        raw = data.get("news") or []
        items = []
        for n in raw[:5]:
            title = (n.get("title") or "").strip()
            link  = n.get("link") or "#"
            pub   = n.get("providerPublishTime") or 0
            if isinstance(pub, (int, float)) and pub > 0:
                try: date = datetime.utcfromtimestamp(pub).strftime("%a, %d %b %Y")
                except Exception: date = ""
            else: date = ""
            if title:
                items.append({"t": title, "u": link, "d": date, "s": _news_sentiment(title)})
            if len(items) >= 3: break
        if items:
            print(f"  news {sym}: {len(items)} via YF-v1", file=sys.stderr)
            return items
        print(f"  news {sym}: YF-v1 empty ({len(raw)} raw)", file=sys.stderr)
    except Exception as e:
        print(f"  news {sym}: YF-v1 failed: {e}", file=sys.stderr)

    # Method 2: yfinance.Ticker.news
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
        if items:
            print(f"  news {sym}: {len(items)} via yfinance", file=sys.stderr)
            return items
        print(f"  news {sym}: yfinance empty ({len(raw)} raw)", file=sys.stderr)
    except Exception as e:
        print(f"  news {sym}: yfinance failed: {e}", file=sys.stderr)

    # Method 3: Google News RSS
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
        if items:
            print(f"  news {sym}: {len(items)} via Google RSS", file=sys.stderr)
            return items
        print(f"  news {sym}: Google RSS empty", file=sys.stderr)
    except Exception as e:
        print(f"  news {sym}: Google RSS failed: {e}", file=sys.stderr)

    print(f"  news {sym}: all methods failed", file=sys.stderr)
    return []

def _try_ibkr_prepost(tickers, host="127.0.0.1", ports=(7497, 7496, 4002, 4001)):
    """
    Optional: fetch pre/post-market prices from IBKR TWS or IB Gateway.
    Returns {sym: {pre_price, post_price, pre_chg, post_chg}} or {} if unavailable.
    Connects with readonly=True, timeout=3s per port — fails silently if TWS not running.
    Ports: 7497=TWS paper, 7496=TWS live, 4002=Gateway paper, 4001=Gateway live.
    """
    try:
        import ib_insync as _ib
        _ib.util.logToConsole("CRITICAL")
    except ImportError:
        return {}

    ib = _ib.IB()
    connected = False
    for port in ports:
        try:
            ib.connect(host, port, clientId=99, timeout=3, readonly=True)
            connected = True
            print(f"  🏦 IBKR connected on port {port}", file=sys.stderr)
            break
        except Exception:
            continue

    if not connected:
        return {}

    try:
        from datetime import datetime as _dt2, timezone as _tz
        # ET offset: UTC-4 summer, UTC-5 winter (DST approximation)
        import time as _time
        _utc_now = _dt2.now(_tz.utc)
        # DST: 2nd Sun Mar – 1st Sun Nov (approx month check)
        _dst = 3 <= _utc_now.month <= 10
        _et_offset = -4 if _dst else -5
        from datetime import timedelta as _td
        _et_now = _utc_now + _td(hours=_et_offset)
        _mkt_open  = _et_now.replace(hour=9,  minute=30, second=0, microsecond=0)
        _mkt_close = _et_now.replace(hour=16, minute=0,  second=0, microsecond=0)
        is_pre  = _et_now < _mkt_open
        is_post = _et_now >= _mkt_close

        if not is_pre and not is_post:
            print("  🏦 IBKR: regular session — no extended-hours data needed", file=sys.stderr)
            ib.disconnect()
            return {}

        # Batch request — all tickers at once
        contracts   = [_ib.Stock(sym, "SMART", "USD") for sym in tickers]
        ibk_tickers = [ib.reqMktData(c, "233", False, False) for c in contracts]
        ib.sleep(4)          # wait for tick stream

        out = {}
        for sym, tkr in zip(tickers, ibk_tickers):
            try:
                close = tkr.close if tkr.close and tkr.close > 0 else None
                last  = tkr.last  if tkr.last  and tkr.last  > 0 else None
                if not close or not last or abs(last - close) < 0.001:
                    continue
                chg = round((last - close) / close * 100, 2)
                if is_pre:
                    out[sym] = {"pre_price": last,  "pre_chg": chg,
                                "post_price": None,  "post_chg": None}
                else:
                    out[sym] = {"pre_price": None,  "pre_chg": None,
                                "post_price": last,  "post_chg": chg}
            except Exception:
                pass

        print(f"  🏦 IBKR extended-hours: {len(out)}/{len(tickers)} tickers", file=sys.stderr)
        return out

    except Exception as _e:
        print(f"  ⚠ IBKR error: {_e}", file=sys.stderr)
        return {}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


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

    # ── Optional IBKR pre/post override (falls back silently if TWS not running) ──
    print("🏦 Trying IBKR extended-hours data...", file=sys.stderr)
    _ibkr = _try_ibkr_prepost(list(tickers))

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

            # ── IBKR source (priority 1) ─────────────────────────────────
            _ibkr_sym = _ibkr.get(sym, {})
            pre_price  = _ibkr_sym.get("pre_price")
            post_price = _ibkr_sym.get("post_price")
            pre_chg    = _ibkr_sym.get("pre_chg")
            post_chg   = _ibkr_sym.get("post_chg")

            # ── yfinance fallback if IBKR didn't supply data ─────────────
            if pre_price is None and post_price is None:
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

            # Use IBKR chg if already computed, else calculate from yfinance prices
            if pre_chg is None:
                pre_chg  = pct(pre_price,  reg_close)
            if post_chg is None:
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
                "pre_post_source": "ibkr" if (_ibkr_sym.get("pre_price") or _ibkr_sym.get("post_price")) else "yahoo",
            }
        except Exception as e:
            print(f"  ⚠ yfinance {sym}: {e}", file=sys.stderr)
    return result


def _pct(s):
    """Parse Finviz percent string like '38.50%' or '-5.2%' → float, or None."""
    if not s or s in ("-", "N/A", ""):
        return None
    try:
        return float(str(s).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


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
    """Score stock against CANSLIM technical + fundamental requirements."""
    price    = safe(row.get("close"), 0)
    sma50    = safe(row.get("SMA50"), 0)
    sma200   = safe(row.get("SMA200"), 0)
    high52   = safe(row.get("High.All"), 0)
    rsi      = safe(row.get("RSI"), 50)
    rel_vol  = safe(row.get("relative_volume_10d_calc"), 1)
    perf_3m  = safe(row.get("Perf.3M"), 0)
    perf_6m  = safe(row.get("Perf.6M"), 0)
    # Finviz fundamental fields (passed via fv_meta, from v=152 Financial view)
    fv        = row.get("fv_meta") or {}
    eps_qoq   = _pct(fv.get("eps_qoq", ""))
    sales_qoq = _pct(fv.get("sales_qoq", ""))
    eps_yoy   = _pct(fv.get("eps_this_y", ""))

    pct_from_high = ((high52 - price) / high52 * 100) if high52 and price else None

    criteria = {}
    # ── Fundamental criteria (C = Current earnings, A = Annual) ──────────────
    if eps_qoq is not None:
        criteria["EPS Q/Q (C=Current Earnings)"] = {
            "value":    "%.1f%%" % eps_qoq,
            "required": "≥ 25%",
            "pass":     eps_qoq >= 25,
            "warn":     15 <= eps_qoq < 25,
        }
    if sales_qoq is not None:
        criteria["Sales Q/Q (C=Current Sales)"] = {
            "value":    "%.1f%%" % sales_qoq,
            "required": "≥ 20%",
            "pass":     sales_qoq >= 20,
            "warn":     10 <= sales_qoq < 20,
        }
    if eps_yoy is not None:
        criteria["EPS Y/Y (A=Annual Growth)"] = {
            "value":    "%.1f%%" % eps_yoy,
            "required": "≥ 25%",
            "pass":     eps_yoy >= 25,
            "warn":     15 <= eps_yoy < 25,
        }
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
    """Calculate entry, stop, T1, T2, and R/R for the trade.

    Entry logic:
      Minervini  — breakout above 52w high (within 5%) or VCP base top
      CANSLIM    — pivot breakout +1% above current price
      Reversion  — buy the bounce +0.5% above close

    Stop logic (ATR-based, clamped to strategy risk limits):
      ATR estimated from daily high-low range × 1.5 (approximates True Range).
      Minervini: entry − 2.0×ATR, clamped −3% to −8%
      CANSLIM:   entry − 1.5×ATR, clamped −3% to −7%
      Reversion: below day low OR entry − 1.5×ATR, clamped −2% to −5%

    T1/T2 logic:
      Minervini/CANSLIM: fixed R/R multiples (3:1 and 5:1 / 2.5:1 and 4:1)
      Reversion: T1 = SMA50 (mean reversion target), T2 = SMA200
    """
    price    = safe(row.get("close"), 0)
    sma50    = safe(row.get("SMA50"), 0)
    sma200   = safe(row.get("SMA200"), 0)
    high52   = safe(row.get("High.All"), 0)
    day_high = safe(row.get("high"), 0)
    day_low  = safe(row.get("low"), 0)

    if not price:
        return {}

    # ── ATR estimate from daily range ────────────────────────────────────────
    # True Range proxy: high-low of current session × 1.5 (accounts for gaps)
    # Minimum fallback: 1.5% of price (avoids near-zero ATR on illiquid days)
    daily_range = (day_high - day_low) if (day_high and day_low and day_high > day_low) else 0
    atr = max(daily_range * 1.5, price * 0.015)

    def clamp_stop(raw_stop, entry, min_pct, max_pct):
        """Keep stop within [entry*(1-max_pct), entry*(1-min_pct)]."""
        floor = entry * (1.0 - max_pct)   # furthest allowed stop
        ceil_ = entry * (1.0 - min_pct)   # closest allowed stop
        return round(max(floor, min(raw_stop, ceil_)), 2)

    if strategy == "minervini":
        # Breakout mode: price within 5% of 52w high → entry just above resistance
        if high52 and price >= high52 * 0.95:
            entry = round(high52 * 1.001, 2)
        else:
            # Consolidation/VCP base: entry slightly above current price
            entry = round(price * 1.005, 2)
        stop  = clamp_stop(entry - 2.0 * atr, entry, 0.03, 0.08)
        risk  = entry - stop
        t1    = round(entry + risk * 3.0, 2)   # 3:1 R/R
        t2    = round(entry + risk * 5.0, 2)   # 5:1 R/R (trim point)

    elif strategy == "canslim":
        entry = round(price * 1.01, 2)
        stop  = clamp_stop(entry - 1.5 * atr, entry, 0.03, 0.07)
        risk  = entry - stop
        t1    = round(entry + risk * 2.5, 2)   # 2.5:1 R/R
        t2    = round(entry + risk * 4.0, 2)   # 4:1 R/R

    else:  # reversion
        entry = round(price * 1.005, 2)
        # Stop: just below the day low OR 1.5×ATR — whichever is tighter
        stop_low = round(day_low * 0.99, 2) if (day_low and day_low < entry) else None
        stop_atr = round(entry - 1.5 * atr, 2)
        raw_stop = max(stop_low, stop_atr) if stop_low else stop_atr
        stop = clamp_stop(raw_stop, entry, 0.02, 0.05)
        # T1 = SMA50 (mean reversion to the mean), T2 = SMA200
        t1   = round(sma50,  2) if (sma50  and sma50  > entry) else round(entry * 1.08, 2)
        t2   = round(sma200, 2) if (sma200 and sma200 > t1)    else round(t1 * 1.05, 2)

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


def enrich_tickers(tickers, strategy, global_markets=False, fv_meta=None):
    """Fetch TV data and score each ticker. fv_meta: dict of ticker→Finviz row."""
    df = fetch_global_tv_data(tickers) if global_markets else fetch_tv_data(tickers)

    if df.empty:
        print("❌ No data returned from TradingView", file=sys.stderr)
        return []

    results = []
    for idx, tv_row in df.iterrows():
        row = tv_row.to_dict()
        raw    = row.get("ticker") or row.get("name") or ""
        ticker = str(raw).split(":")[-1].strip().upper()
        # Inject Finviz fundamentals if available
        if fv_meta and ticker in fv_meta:
            row["fv_meta"] = fv_meta[ticker]

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
            "mkt_cap":     safe(row.get("market_cap_basic")),
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
            "strategy":   strategy,
        })

    results.sort(key=lambda x: (not x["valid_setup"], -x["score"]["score_pct"]))
    return results




def fetch_daily_breadth_data(history_file="breadth_history.json"):
    """
    Fetch market-wide breadth metrics via TradingView screener.
    Counts US stocks meeting each criterion, stores daily in history_file.
    Returns (today_data_dict, history_list_sorted_desc).
    """
    try:
        from tradingview_screener import Query, col as Col
        import json as _json, os as _os
        from datetime import date as _date

        today_str = _date.today().isoformat()

        # Load accumulated history {date: metrics}
        history = {}
        if _os.path.exists(history_file):
            try:
                history = _json.loads(open(history_file, encoding="utf-8").read())
            except Exception:
                history = {}

        # ── One broad query: US common stocks, price > 1, with 45-sec timeout ──
        import concurrent.futures as _cf
        print("  Daily Breadth: querying TV screener (all US stocks, 45s timeout)...", file=__import__("sys").stderr)
        def _run_query():
            return (
                Query()
                .set_markets("america")
                .select("name", "close", "change", "Perf.W", "Perf.1M", "Perf.3M",
                        "SMA50", "SMA200", "exchange")
                .where(Col("close") > 1, Col("type").isin(["stock", "dr"]))
                .limit(5000)
                .get_scanner_data()
            )
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(_run_query)
            try:
                _, df = _fut.result(timeout=45)
            except _cf.TimeoutError:
                print("  Daily Breadth: TV screener timed out (45s) — skipping", file=__import__("sys").stderr)
                return {}, []

        total = len(df)
        if total == 0:
            print("  Daily Breadth: no data returned", file=__import__("sys").stderr)
            return {}, list(history.values())

        chg   = df["change"].fillna(0)
        p1m   = df["Perf.1M"].fillna(0)
        p3m   = df["Perf.3M"].fillna(0)
        close = df["close"].fillna(0)
        sma50 = df["SMA50"].fillna(0)

        up4   = int((chg >= 4).sum())
        dn4   = int((chg <= -4).sum())
        adv   = int((chg > 0).sum())
        dec   = int((chg < 0).sum())

        today_data = {
            "date":    today_str,
            "up4":     up4,
            "dn4":     dn4,
            "adv":     adv,
            "dec":     dec,
            "up25q":   int((p3m >= 25).sum()),
            "dn25q":   int((p3m <= -25).sum()),
            "up25m":   int((p1m >= 25).sum()),
            "dn25m":   int((p1m <= -25).sum()),
            "up50m":   int((p1m >= 50).sum()),
            "dn50m":   int((p1m <= -50).sum()),
            "above50": int(((close > 0) & (sma50 > 0) & (close > sma50)).sum()),
            "universe": total,
        }

        # Accumulate history (keep last 60 trading days)
        history[today_str] = today_data
        sorted_keys = sorted(history.keys(), reverse=True)[:60]
        history = {k: history[k] for k in sorted_keys}
        try:
            open(history_file, "w", encoding="utf-8").write(_json.dumps(history, indent=2))
        except Exception as e:
            print(f"  Daily Breadth: could not write {history_file}: {e}", file=__import__("sys").stderr)

        # Compute rolling A/D ratios from history
        hist_list = [history[k] for k in sorted(history.keys(), reverse=True)]
        adv_vals  = [d.get("adv", 0) for d in hist_list]
        dec_vals  = [d.get("dec", 1) for d in hist_list]
        for i, d in enumerate(hist_list):
            a5  = sum(adv_vals[i:i+5]);  d5  = sum(dec_vals[i:i+5])
            a10 = sum(adv_vals[i:i+10]); d10 = sum(dec_vals[i:i+10])
            d["ratio5"]  = round(a5  / d5,  2) if d5  else 0
            d["ratio10"] = round(a10 / d10, 2) if d10 else 0
            pct50 = round(d.get("above50", 0) / d.get("universe", 1) * 100, 1)
            d["pct50"] = pct50

        print(f"  Daily Breadth: {total} stocks | up4={up4} dn4={dn4} above50={today_data['above50']}", file=__import__("sys").stderr)
        return today_data, hist_list

    except Exception as e:
        print(f"  Daily Breadth fetch failed: {e}", file=__import__("sys").stderr)
        return {}, []


def build_pulse_panel_html(pulse_data, all_results=None, daily_breadth=None):
    """
    Collapsible Market Pulse panel:
      Tabs -- Theme Tracker | S&P Sectors | Country ETFs | Highs / Lows | Daily Breadth
    pulse_data    : dict from fetch_market_pulse_data()
    all_results   : enriched scanner rows (Highs/Lows tab)
    daily_breadth : (today_dict, hist_list) from fetch_daily_breadth_data()
    """
    if not pulse_data:
        return ""

    # Extract pre-fetched ETF holdings (country tab) without mutating caller dict
    _holdings = (pulse_data or {}).get("_holdings", {})
    _holdings_live = (pulse_data or {}).get("_holdings_live", {})

    SQ = "'"   # single-quote helper to avoid heredoc quoting issues

    def _fmt(v):
        if v is None:
            return '<span style="color:#475569">&#8212;</span>'
        c = "#10b981" if v >= 0 else "#ef4444"
        s = "+" if v >= 0 else ""
        return '<span style="color:%s;font-weight:600">%s%.2f%%</span>' % (c, s, v)

    def _build_rows(sym_dict):
        rows = []
        for sym, _lbl in sym_dict.items():
            d = pulse_data.get(sym)
            if not d:
                continue
            rows.append((d.get("today") or 0.0, sym, d))
        rows.sort(key=lambda x: x[0], reverse=True)
        html = ""
        for today_v, sym, d in rows:
            bg  = "rgba(16,185,129,.07)" if today_v >= 0 else "rgba(239,68,68,.07)"
            p   = d.get("price", 0)
            lbl = d.get("label", sym)
            fv_url = "https://finviz.com/quote.ashx?t=" + sym
            html += (
                "<tr style=\"border-bottom:1px solid #1e293b;cursor:pointer\""
                " onclick=\"window.open(%s'%s',%s'_blank'%s)\"" % (SQ, fv_url, SQ, SQ)
                + " onmouseover=\"this.style.background=%s#1e293b%s\"" % (SQ, SQ)
                + " onmouseout=\"this.style.background=%stransparent%s\">" % (SQ, SQ)
                + "<td style=\"padding:5px 10px;font-weight:700;color:#94a3b8;font-size:12px;white-space:nowrap\">%s</td>" % sym
                + "<td style=\"padding:5px 10px;color:#64748b;font-size:12px;white-space:nowrap\">%s</td>" % lbl
                + "<td style=\"padding:5px 10px;text-align:right;color:#94a3b8;font-size:12px\">$%.2f</td>" % p
                + "<td style=\"padding:5px 10px;text-align:right;font-size:12px\">%s</td>" % _fmt(d.get("chg_open"))
                + "<td style=\"padding:5px 10px;text-align:right;background:%s;font-size:12px\">%s</td>" % (bg, _fmt(today_v))
                + "<td style=\"padding:5px 10px;text-align:right;font-size:12px\">%s</td>" % _fmt(d.get("1w"))
                + "<td style=\"padding:5px 10px;text-align:right;font-size:12px\">%s</td>" % _fmt(d.get("1m"))
                + "<td style=\"padding:5px 10px;text-align:right;font-size:12px\">%s</td>" % _fmt(d.get("3m"))
                + "<td style=\"padding:5px 10px;text-align:right;font-size:12px\">%s</td>" % _fmt(d.get("6m"))
                + "<td style=\"padding:5px 10px;text-align:right;font-size:12px\">%s</td>" % _fmt(d.get("ytd"))
                + "</tr>"
            )
        return html

    def _th(label, pane_id, col_i, active=False):
        clr = "color:#f59e0b;" if active else "color:#64748b;"
        return (
            "<th style=\"padding:6px 10px;text-align:right;%sfont-size:11px;"
            "cursor:pointer;user-select:none;white-space:nowrap\""
            " onclick=\"mpSort(%s'%s',%s%d%s)\">%s</th>"
        ) % (clr, SQ, pane_id, SQ, col_i, SQ[0:0], label)

    def _build_pane(sym_dict, pane_id, visible=False):
        rows = _build_rows(sym_dict)
        disp = "block" if visible else "none"
        thead = (
            "<thead style=\"position:sticky;top:0;background:#151e2d;z-index:2\">"
            "<tr style=\"border-bottom:2px solid #334155\">"
            "<th style=\"padding:6px 10px;text-align:left;color:#64748b;font-size:11px;"
            "cursor:pointer;user-select:none\" onclick=\"mpSort(%s'%s',0)\">Ticker</th>" % (SQ, pane_id)
            + "<th style=\"padding:6px 10px;text-align:left;color:#64748b;font-size:11px\">Group</th>"
            + "<th style=\"padding:6px 10px;text-align:right;color:#64748b;font-size:11px\">Price</th>"
            + _th("Today &#9660;", pane_id, 3, active=True)
            + _th("1W", pane_id, 4) + _th("1M", pane_id, 5)
            + _th("3M", pane_id, 6) + _th("6M", pane_id, 7) + _th("YTD", pane_id, 8)
            + "</tr></thead>"
        )
        return (
            "<div id=\"mpp-%s\" class=\"mpp-pane\" style=\"display:%s\">"
            "<div style=\"overflow-x:auto;max-height:760px;overflow-y:auto\">"
            "<table style=\"width:100%%;border-collapse:collapse\">"
            "%s"
            "<tbody id=\"mpp-body-%s\">%s</tbody>"
            "</table></div></div>"
        ) % (pane_id, disp, thead, pane_id, rows)

    def _highs_lows_pane():
        # Prefer full-market breadth data (from TV Screener all-stocks query)
        mhl = (pulse_data or {}).get("_market_hl") if pulse_data else None
        if mhl:
            highs_by_sec = mhl["hi_by_sec"]
            lows_by_sec  = mhl["lo_by_sec"]
            total_hi     = mhl["hi_count"]
            total_lo     = mhl["lo_count"]
        elif all_results:
            # Fallback: use scan universe (growth stocks only — lows will be rare)
            highs_by_sec = {}
            lows_by_sec  = {}
            total_hi = total_lo = 0
            for r in all_results:
                try:
                    price = float(r.get("price") or r.get("close") or 0)
                    h52   = float(str(r.get("high52") or r.get("high_52w") or "0").replace(",","") or 0)
                    l52   = float(str(r.get("low52")  or r.get("low_52w")  or "0").replace(",","") or 0)
                    sec   = (r.get("sector") or "Other").strip() or "Other"
                    tkr   = r.get("ticker", "")
                    if not (price and tkr):
                        continue
                    if h52 and price >= h52 * 0.97:
                        highs_by_sec.setdefault(sec, []).append(tkr); total_hi += 1
                    if l52 and price <= l52 * 1.03:
                        lows_by_sec.setdefault(sec, []).append(tkr); total_lo += 1
                except Exception:
                    pass
        else:
            return (
                "<div id=\"mpp-highs\" class=\"mpp-pane\" style=\"display:none;padding:20px;"
                "color:#64748b;text-align:center\">&#9888; No data available</div>"
            )
        total  = total_hi + total_lo or 1
        hi_pct = round(total_hi / total * 100)
        lo_pct = 100 - hi_pct
        html = (
            "<div id=\"mpp-highs\" class=\"mpp-pane\" style=\"display:none;padding:14px 16px\">"
            "<div style=\"display:flex;align-items:center;gap:12px;margin-bottom:14px\">"
            "<span style=\"color:#10b981;font-size:13px;font-weight:700\">&#8679; Near Highs %d%% (%d)</span>"
            "<div style=\"flex:1;height:7px;border-radius:4px;background:#ef4444\">"
            "<div style=\"width:%d%%;height:100%%;background:#10b981;border-radius:4px\"></div></div>"
            "<span style=\"color:#ef4444;font-size:13px;font-weight:700\">&#8681; Near Lows %d%% (%d)</span>"
            "</div><div style=\"display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px\">"
        ) % (hi_pct, total_hi, hi_pct, lo_pct, total_lo)
        for sec, tkrs in sorted(highs_by_sec.items(), key=lambda x: -len(x[1]))[:12]:
            chips = "".join(
                "<span style=\"background:#064e3b;color:#34d399;padding:2px 7px;border-radius:5px;"
                "font-size:11px;font-weight:700;cursor:pointer;display:inline-block\""
                " onclick=\"window.open(%s'https://finviz.com/quote.ashx?t=%s',%s'_blank'%s)\">%s</span>" % (SQ, t, SQ, SQ, t)
                for t in tkrs[:8]
            )
            html += (
                "<div style=\"background:#0f172a;border:1px solid #1a3a2a;border-radius:8px;padding:8px 10px\">"
                "<div style=\"color:#10b981;font-size:11px;font-weight:700;margin-bottom:6px\">&#9650; %s (%d)</div>"
                "<div style=\"display:flex;flex-wrap:wrap;gap:4px\">%s</div></div>"
            ) % (sec, len(tkrs), chips)
        for sec, tkrs in sorted(lows_by_sec.items(), key=lambda x: -len(x[1]))[:4]:
            chips = "".join(
                "<span style=\"background:#450a0a;color:#fca5a5;padding:2px 7px;border-radius:5px;"
                "font-size:11px;font-weight:700;cursor:pointer;display:inline-block\""
                " onclick=\"window.open(%s'https://finviz.com/quote.ashx?t=%s',%s'_blank'%s)\">%s</span>" % (SQ, t, SQ, SQ, t)
                for t in tkrs[:6]
            )
            html += (
                "<div style=\"background:#0f172a;border:1px solid #3a1a1a;border-radius:8px;padding:8px 10px\">"
                "<div style=\"color:#ef4444;font-size:11px;font-weight:700;margin-bottom:6px\">&#9660; %s (%d)</div>"
                "<div style=\"display:flex;flex-wrap:wrap;gap:4px\">%s</div></div>"
            ) % (sec, len(tkrs), chips)
        return html + "</div></div>"

    def _breadth_pane():
        PERIODS = [("today","Today"),("1w","1W"),("1m","1M"),
                   ("3m","3M"),("6m","6M"),("ytd","YTD")]

        def _br(sym_dict, period):
            vals = [pulse_data.get(s, {}).get(period) for s in sym_dict]
            vals = [v for v in vals if v is not None]
            if not vals:
                return 0, len(sym_dict), 0
            pos = sum(1 for v in vals if v > 0)
            tot = len(vals)
            return pos, tot, round(pos / tot * 100)

        def _brow(lbl, pos, tot, pct):
            bc = "#10b981" if pct >= 60 else "#f59e0b" if pct >= 40 else "#ef4444"
            return (
                "<tr style=\"border-bottom:1px solid #0f172a\">"
                "<td style=\"padding:4px 10px;color:#94a3b8;font-size:12px;width:55px\">%s</td>"
                "<td style=\"padding:4px 8px;width:90px\">"
                "<div style=\"background:#0f172a;border-radius:3px;height:6px\">"
                "<div style=\"width:%d%%;height:100%%;background:%s;border-radius:3px\"></div>"
                "</div></td>"
                "<td style=\"padding:4px 8px;color:%s;font-size:12px;font-weight:700;width:40px\">%d%%</td>"
                "<td style=\"padding:4px 8px;color:#64748b;font-size:11px\">%d/%d</td>"
                "</tr>"
            ) % (lbl, pct, bc, bc, pct, pos, tot)

        def _btable(title, sym_dict):
            rows = "".join(_brow(lbl, *_br(sym_dict, p)) for p, lbl in PERIODS)
            return (
                "<div>"
                "<div style=\"color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;"
                "letter-spacing:.5px;margin-bottom:8px\">%s</div>"
                "<table style=\"width:100%%;border-collapse:collapse\"><tbody>%s</tbody></table>"
                "</div>"
            ) % (title, rows)

        def _risk_card(sym, fallback):
            d = pulse_data.get(sym, {})
            if not d:
                return ""
            price = d.get("price", 0)
            today = d.get("today", 0)
            lbl   = d.get("label", fallback)
            c     = "#10b981" if today >= 0 else "#ef4444"
            arr   = "+" if today >= 0 else ""
            # VIX: invert colour (high VIX = red = bad)
            if sym == "^VIX":
                c = "#ef4444" if price > 20 else "#f59e0b" if price > 15 else "#10b981"
            return (
                "<div style=\"background:#0f172a;border:1px solid #1e293b;border-radius:8px;"
                "padding:10px 14px;min-width:120px\">"
                "<div style=\"color:#64748b;font-size:10px;text-transform:uppercase;"
                "letter-spacing:.5px;margin-bottom:4px\">%s</div>"
                "<div style=\"color:#e2e8f0;font-size:17px;font-weight:700\">%.2f</div>"
                "<div style=\"color:%s;font-size:11px;margin-top:2px\">%s%.2f%%</div>"
                "</div>"
            ) % (lbl, price, c, arr, today)

        risk_syms = [("^VIX","VIX"),("HYG","High Yield"),("LQD","IG Corp"),
                     ("TLT","20Y T-Bond"),("RSP","Equal Wt S&P")]
        risk_html = "".join(_risk_card(s, lb) for s, lb in risk_syms)

        s_table = _btable("Sector Participation  (11 ETFs)", SECTOR_ETFS)
        t_table = _btable("Theme Participation  (26 ETFs)",  THEME_ETFS)
        c_table = _btable("Country Participation  (20 ETFs)", COUNTRY_ETFS)

        return (
            "<div id=\"mpp-breadth\" class=\"mpp-pane\" style=\"display:none;padding:16px\">"
            "<div style=\"display:grid;grid-template-columns:repeat(3,1fr);gap:20px;"
            "margin-bottom:20px\">"
            + s_table + t_table + c_table
            + "</div>"
            "<div style=\"color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;"
            "letter-spacing:.5px;margin-bottom:10px\">&#9888; Risk Sentiment</div>"
            "<div style=\"display:flex;flex-wrap:wrap;gap:10px\">"
            + risk_html
            + "</div></div>"
        )

    def _build_country_pane(sym_dict, pane_id, holdings, fallback_dict=None, group_label="Country / ETF", visible=False, holdings_live=None):
        # Country/Theme/Sector ETF pane: perf table + expandable top-10 holdings sub-rows
        rows_html = _build_rows(sym_dict)
        hold_rows = {}
        # Merge live holdings with static fallback — live data takes priority,
        # fallback_dict checked first, then COUNTRY_ETF_FALLBACK_HOLDINGS
        merged_holdings = {}
        for sym in sym_dict:
            live = (holdings or {}).get(sym)
            if live:
                merged_holdings[sym] = live
            elif fallback_dict and sym in fallback_dict:
                fb = [dict(h) for h in fallback_dict[sym]]
                for h in fb:
                    h.setdefault("cached", True)
                merged_holdings[sym] = fb
            elif sym in COUNTRY_ETF_FALLBACK_HOLDINGS:
                fb = [dict(h) for h in COUNTRY_ETF_FALLBACK_HOLDINGS[sym]]
                for h in fb:
                    h.setdefault("cached", True)
                merged_holdings[sym] = fb
        for sym, hlist in merged_holdings.items():
            if not hlist:
                continue
            h_inner = (
                "<tr id='hold-" + sym + "' style='display:none;background:#0a1628'>"
                "<td colspan='10' style='padding:0'>"
                "<div style='padding:10px 14px 14px'>"
                "<div style='font-size:10px;color:#475569;font-weight:700;"
                "text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px'>Top Holdings"
                + (" <span style='font-weight:400;color:#374151;font-size:9px'>(static)</span>"
                   if any(h.get("cached") for h in hlist) else " <span style='font-weight:400;color:#22c55e;font-size:9px'>(live)</span>")
                + "</div>"
                "<table style='width:100%;border-collapse:collapse'>"
                "<thead><tr style='border-bottom:1px solid #1e293b'>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:left'>Ticker</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:left'>Company</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>Price</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>Chg/Open</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>Day</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>1W</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>1M</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>3M</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>6M</th>"
                "<th style='padding:3px 8px;color:#475569;font-size:10px;text-align:right'>YTD</th>"
                "</tr></thead><tbody>"
            )
            def _hfmt(v):
                if v is None:
                    return "<span style='color:#374151'>&#8212;</span>"
                c = "#10b981" if v >= 0 else "#ef4444"
                s = "+" if v >= 0 else ""
                return "<span style='color:%s'>%s%.2f%%</span>" % (c, s, v)
            for h in hlist:
                tkr  = h.get("ticker", "")
                name = h.get("name", tkr)
                pct  = h.get("pct", 0)
                ld   = (holdings_live or {}).get(tkr, {})
                px   = ld.get("price") or h.get("price")
                px_s = ("$%.2f" % px) if px else "&#8212;"
                fv_url = "https://finviz.com/quote.ashx?t=" + tkr
                h_inner += (
                    "<tr style='border-bottom:1px solid #1e2d42'>"
                    "<td style='padding:4px 8px;font-weight:700;font-size:11px'>"
                    "<a href='%s' target='_blank' style='color:#60a5fa;text-decoration:none'>%s</a>"
                    " <span style='color:#f59e0b;font-size:10px'>%.1f%%</span></td>"
                    "<td style='padding:4px 8px;color:#94a3b8;font-size:11px'>%s</td>"
                    "<td style='padding:4px 8px;color:#94a3b8;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "<td style='padding:4px 8px;font-size:11px;text-align:right'>%s</td>"
                    "</tr>"
                ) % (fv_url, tkr, pct, name, px_s,
                     _hfmt(ld.get("chg_open")), _hfmt(ld.get("today")),
                     _hfmt(ld.get("1w")), _hfmt(ld.get("1m")),
                     _hfmt(ld.get("3m")), _hfmt(ld.get("6m")),
                     _hfmt(ld.get("ytd")))
            h_inner += "</tbody></table></div></td></tr>"
            hold_rows[sym] = h_inner

        final_rows = ""
        for sym in sym_dict:
            row_marker = (
                '<tr style="border-bottom:1px solid #1e293b;cursor:pointer"'
                ' onclick="window.open(%s\'https://finviz.com/quote.ashx?t=%s\',%s\'_blank\'%s)"'
                % (SQ, sym, SQ, SQ)
            )
            row_start = rows_html.find(row_marker)
            if row_start == -1:
                continue
            row_end = rows_html.find("</tr>", row_start) + 5
            orig_row = rows_html[row_start:row_end]
            if sym in hold_rows:
                toggled = orig_row.replace(
                    'onclick="window.open(%s\'https://finviz.com/quote.ashx?t=%s\',%s\'_blank\'%s)"'
                    % (SQ, sym, SQ, SQ),
                    "onclick=\"toggleHold('%s')\"" % sym
                )
                arrow_td = (
                    '<td style="padding:5px 10px;font-weight:700;color:#94a3b8;font-size:12px;white-space:nowrap">'
                    "<span id='arr-%s' style='display:inline-block;margin-right:5px;"
                    "transition:transform .2s;color:#64748b'>&#9658;</span>%s</td>"
                ) % (sym, sym)
                orig_td = (
                    '<td style="padding:5px 10px;font-weight:700;color:#94a3b8;font-size:12px;white-space:nowrap">%s</td>'
                    % sym
                )
                toggled = toggled.replace(orig_td, arrow_td)
                final_rows += toggled + hold_rows[sym]
            else:
                final_rows += orig_row

        thead = (
            "<thead style='position:sticky;top:0;background:#151e2d;z-index:2'>"
            "<tr style='border-bottom:2px solid #334155'>"
            "<th style='padding:6px 10px;text-align:left;color:#64748b;font-size:11px'>Ticker</th>"
            "<th style='padding:6px 10px;text-align:left;color:#64748b;font-size:11px'>" + group_label + "</th>"
            "<th style='padding:6px 10px;text-align:right;color:#64748b;font-size:11px'>Price</th>"
            + _th("Chg/Open", pane_id, 3)
            + _th("Day &#9660;", pane_id, 4, active=True)
            + _th("1W", pane_id, 5) + _th("1M", pane_id, 6)
            + _th("3M", pane_id, 7) + _th("6M", pane_id, 8) + _th("YTD", pane_id, 9)
            + "</tr></thead>"
        )
        disp = "block" if visible else "none"
        return (
            "<div id='mpp-%s' class='mpp-pane' style='display:%s'>"
            "<div style='overflow-x:auto;max-height:760px;overflow-y:auto'>"
            "<table style='width:100%%;border-collapse:collapse'>"
            "%s<tbody id='mpp-body-%s'>%s</tbody>"
            "</table></div></div>"
        ) % (pane_id, disp, thead, pane_id, final_rows)

    themes_pane  = _build_country_pane(THEME_ETFS,  "themes",  None, THEME_ETF_FALLBACK_HOLDINGS,  "Theme / ETF",  visible=True,  holdings_live=_holdings_live)
    sectors_pane = _build_country_pane(SECTOR_ETFS, "sectors", None, SECTOR_ETF_FALLBACK_HOLDINGS, "Sector / ETF", holdings_live=_holdings_live)
    countries_pane = _build_country_pane(COUNTRY_ETFS, "countries", _holdings, holdings_live=_holdings_live)
    highs_pane     = _highs_lows_pane()
    breadth_pane   = _breadth_pane()

    js = (
        "<script>(function(){"
        "var _d={};"
        "window.mpSort=function(id,col){"
        "var tb=document.getElementById(\"mpp-body-\"+id);if(!tb)return;"
        "var rows=Array.from(tb.querySelectorAll(\"tr:not([id^='hold-'])\"));"
        "var k=id+\"_\"+col;_d[k]=(_d[k]===\"desc\")?\"asc\":\"desc\";"
        "rows.sort(function(a,b){"
        "var av=((a.cells[col]||{}).textContent||\"\").replace(/[+%$,—\\s]+/g,\"\").trim();"
        "var bv=((b.cells[col]||{}).textContent||\"\").replace(/[+%$,—\\s]+/g,\"\").trim();"
        "var an=parseFloat(av),bn=parseFloat(bv);"
        "if(isNaN(an)&&isNaN(bn))return _d[k]===\"asc\"?av.localeCompare(bv):bv.localeCompare(av);"
        "if(isNaN(an))return 1;if(isNaN(bn))return -1;"
        "return _d[k]===\"asc\"?an-bn:bn-an;});"
        "rows.forEach(function(r){"
        "tb.appendChild(r);"
        "var sym=(r.cells[0]?r.cells[0].textContent:'').replace(/[^A-Z0-9]/g,'').trim();"
        "if(sym){var hr=document.getElementById('hold-'+sym);if(hr)tb.appendChild(hr);}"
        "});};"
        "window.toggleHold=function(sym){"
        "var r=document.getElementById('hold-'+sym);"
        "var a=document.getElementById('arr-'+sym);"
        "if(!r)return;"
        "var open=r.style.display==='table-row';"
        "r.style.display=open?'none':'table-row';"
        "if(a)a.style.transform=open?'rotate(90deg)':'';"
        "};"
        "window.mppSwitch=function(btn,pane){"
        "document.querySelectorAll(\".mpp-tab\").forEach(function(b){"
        "b.style.color=\"#64748b\";b.style.borderBottom=\"2px solid transparent\";b.style.fontWeight=\"400\";});"
        "btn.style.color=\"#e2e8f0\";btn.style.borderBottom=\"2px solid #f59e0b\";btn.style.fontWeight=\"600\";"
        "document.querySelectorAll(\".mpp-pane\").forEach(function(p){p.style.display=\"none\";});"
        "var el=document.getElementById(\"mpp-\"+pane);if(el)el.style.display=\"block\";};"
        "window.mppToggle=function(){"
        "var b=document.getElementById(\"mpp-body-panel\");"
        "var i=document.getElementById(\"mpp-toggle-icon\");if(!b)return;"
        "var v=b.style.display!==\"none\";"
        "b.style.display=v?\"none\":\"block\";"
        "if(i)i.textContent=v?\"▼ expand\":\"▲ collapse\";};"
        "})();</script>"
    )

    def _tab_btn(pane, label, active=False):
        bb = "#f59e0b" if active else "transparent"
        cc = "#e2e8f0" if active else "#64748b"
        fw = "600" if active else "400"
        return (
            "      <button class=\"mpp-tab\" onclick=\"mppSwitch(this,'%s')\""
            " style=\"background:none;border:none;border-bottom:2px solid %s;color:%s;font-weight:%s;"
            "padding:9px 14px;cursor:pointer;font-size:12px;white-space:nowrap\">%s</button>\n"
        ) % (pane, bb, cc, fw, label)

    def _daily_breadth_pane():
        """Historical daily breadth table (like RealSimpleAriel Breadth indicator)."""
        today_d, hist_list = (daily_breadth if daily_breadth else ({}, []))

        def _ratio_cell(v):
            if v is None or v == 0:
                return "<td style='padding:5px 8px;text-align:center;color:#64748b;font-size:12px'>—</td>"
            c = "#10b981" if v >= 1.0 else "#ef4444"
            return "<td style='padding:5px 8px;text-align:center;font-weight:700;font-size:12px;color:%s'>%.2f</td>" % (c, v)

        def _int_cell(v, hi_thr=None, lo_thr=None, invert=False):
            if v is None:
                return "<td style='padding:5px 8px;text-align:center;color:#64748b;font-size:12px'>—</td>"
            c = "#e2e8f0"
            if hi_thr is not None and v >= hi_thr:
                c = "#ef4444" if invert else "#10b981"
            elif lo_thr is not None and v <= lo_thr:
                c = "#10b981" if invert else "#ef4444"
            return "<td style='padding:5px 8px;text-align:center;font-size:12px;color:%s'>%d</td>" % (c, v)

        def _pct_cell(v):
            if v is None:
                return "<td style='padding:5px 8px;text-align:center;color:#64748b;font-size:12px'>—</td>"
            c = "#10b981" if v >= 60 else "#f59e0b" if v >= 40 else "#ef4444"
            return "<td style='padding:5px 8px;text-align:center;font-weight:700;font-size:12px;color:%s'>%.1f%%</td>" % (c, v)

        def _date_cell(d):
            return "<td style='padding:5px 8px;color:#64748b;font-size:11px;white-space:nowrap'>%s</td>" % d

        rows_html = ""
        for d in hist_list[:30]:
            rows_html += (
                "<tr style='border-bottom:1px solid #0f172a'>"
                + _date_cell(d.get("date",""))
                + _int_cell(d.get("up4"),   hi_thr=200,  lo_thr=50)
                + _int_cell(d.get("dn4"),   hi_thr=200,  lo_thr=50, invert=True)
                + _ratio_cell(d.get("ratio5"))
                + _ratio_cell(d.get("ratio10"))
                + _int_cell(d.get("up25q"),  hi_thr=400,  lo_thr=100)
                + _int_cell(d.get("dn25q"),  hi_thr=100,  lo_thr=20, invert=True)
                + _int_cell(d.get("up25m"),  hi_thr=150,  lo_thr=30)
                + _int_cell(d.get("dn25m"),  hi_thr=100,  lo_thr=20, invert=True)
                + _int_cell(d.get("up50m"),  hi_thr=50,   lo_thr=5)
                + _int_cell(d.get("dn50m"),  hi_thr=30,   lo_thr=5,  invert=True)
                + _pct_cell(d.get("pct50"))
                + _int_cell(d.get("universe"))
                + "</tr>"
            )

        if not rows_html:
            rows_html = ("<tr><td colspan='13' style='padding:20px;text-align:center;"
                         "color:#475569;font-size:12px'>No breadth history yet — "
                         "data will accumulate with each dashboard build</td></tr>")

        thead = (
            "<thead style='position:sticky;top:0;background:#151e2d;z-index:2'>"
            "<tr style='border-bottom:2px solid #334155'>"
            "<th style='padding:6px 8px;color:#94a3b8;font-size:10px;text-align:left'>Date</th>"
            "<th style='padding:6px 6px;color:#10b981;font-size:10px;text-align:center' title='Stocks up 4%+ today'>Up 4%+<br>Today</th>"
            "<th style='padding:6px 6px;color:#ef4444;font-size:10px;text-align:center' title='Stocks down 4%+ today'>Dn 4%+<br>Today</th>"
            "<th style='padding:6px 6px;color:#a78bfa;font-size:10px;text-align:center' title='5-day advance/decline ratio'>5D<br>Ratio</th>"
            "<th style='padding:6px 6px;color:#a78bfa;font-size:10px;text-align:center' title='10-day advance/decline ratio'>10D<br>Ratio</th>"
            "<th style='padding:6px 6px;color:#10b981;font-size:10px;text-align:center' title='Stocks up 25%+ this quarter'>Up 25%+<br>Qtr</th>"
            "<th style='padding:6px 6px;color:#ef4444;font-size:10px;text-align:center' title='Stocks down 25%+ this quarter'>Dn 25%+<br>Qtr</th>"
            "<th style='padding:6px 6px;color:#10b981;font-size:10px;text-align:center' title='Stocks up 25%+ this month'>Up 25%+<br>Mo</th>"
            "<th style='padding:6px 6px;color:#ef4444;font-size:10px;text-align:center' title='Stocks down 25%+ this month'>Dn 25%+<br>Mo</th>"
            "<th style='padding:6px 6px;color:#10b981;font-size:10px;text-align:center' title='Stocks up 50%+ this month'>Up 50%+<br>Mo</th>"
            "<th style='padding:6px 6px;color:#ef4444;font-size:10px;text-align:center' title='Stocks down 50%+ this month'>Dn 50%+<br>Mo</th>"
            "<th style='padding:6px 6px;color:#f59e0b;font-size:10px;text-align:center' title='% of stocks above 50-day MA'>&gt;50dma<br>%</th>"
            "<th style='padding:6px 6px;color:#64748b;font-size:10px;text-align:center'>Universe</th>"
            "</tr></thead>"
        )

        return (
            "<div id='mpp-dbreadth' class='mpp-pane' style='display:none'>"
            "<div style='overflow-x:auto;max-height:760px;overflow-y:auto'>"
            "<table style='width:100%;border-collapse:collapse'>"
            + thead
            + "<tbody>" + rows_html + "</tbody>"
            "</table></div></div>"
        )

    daily_breadth_pane = _daily_breadth_pane()


    return (
        "\n<!-- Market Pulse Panel -->\n"
        "<div style=\"background:#1e293b;border:1px solid #334155;border-radius:12px;margin-bottom:14px;overflow:hidden\">\n"
        "  <div onclick=\"mppToggle()\" style=\"display:flex;align-items:center;justify-content:space-between;"
        "padding:10px 16px;cursor:pointer;user-select:none;border-bottom:1px solid #334155\">\n"
        "    <span style=\"font-size:13px;font-weight:700;color:#e2e8f0\">&#128202; Market Pulse</span>\n"
        "    <span id=\"mpp-toggle-icon\" style=\"color:#64748b;font-size:11px\">&#9660; expand</span>\n"
        "  </div>\n"
        "  <div id=\"mpp-body-panel\" style=\"display:none\">\n"
        "    <div style=\"display:flex;border-bottom:1px solid #334155;padding:0 8px;overflow-x:auto\">\n"
        + _tab_btn("themes",    "&#128200; Theme Tracker", active=True)
        + _tab_btn("sectors",   "&#127959; S&amp;P Sectors")
        + _tab_btn("countries", "&#127758; Country ETFs")
        + _tab_btn("highs",     "&#128202; Highs / Lows")
        + _tab_btn("breadth",   "&#128268; Breadth")
        + _tab_btn("dbreadth",  "&#128202; Daily Breadth")
        + "    </div>\n"
        + themes_pane + "\n"
        + sectors_pane + "\n"
        + countries_pane + "\n"
        + highs_pane + "\n"
        + breadth_pane + "\n"
        + daily_breadth_pane + "\n"
        + "  </div>\n</div>\n"
        + js
    )



def build_html_dashboard(results, strategy, market_ctx=None, yahoo=None, tabs_mode=False, new_tickers=None, market_pulse=None, **kwargs):
    """Generate self-contained HTML dashboard."""
    market_ctx = market_ctx or {}
    yahoo      = yahoo or {}
    new_tickers = set(new_tickers or [])
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
        _hdrill = bool(SECTOR_INDUSTRY_MAP.get(etf))
        _hcur   = "cursor:pointer;" if _hdrill else ""
        _hclk   = "onclick=\"toggleSectorDrill('%s')\" " % etf if _hdrill else ""
        sector_cells.append(
            '<div %sstyle="%sbackground:%s;border-radius:6px;padding:8px 10px;'
            'text-align:center;min-width:90px;flex:1" id="sec-tile-%s">'
            '<div style="font-size:11px;font-weight:700;color:%s">%s%.2f%%</div>'
            '<div style="font-size:10px;color:#94a3b8;margin-top:2px">%s</div>'
            '<div style="font-size:10px;color:#64748b">%s</div>'
            '</div>' % (_hclk, _hcur, bg, etf, tc, chg_arrow(chg), abs(chg), name, etf)
        )

    # Embed sub-industry performance data for JS drill-down
    import json as _json
    _mp = market_pulse or {}
    _ddata = {}
    for _etf, _inds in SECTOR_INDUSTRY_MAP.items():
        _ddata[_etf] = []
        for _sym, _lbl in _inds:
            _d = _mp.get(_sym, {})
            _ddata[_etf].append({
                "sym": _sym, "lbl": _lbl,
                "day": _d.get("today"), "w1": _d.get("1w"),
                "m1":  _d.get("1m"),   "m3": _d.get("3m"),
            })
    _djson = _json.dumps(_ddata)

    _dpanel = (
        '<div id="sector-drill-panel" style="display:none;margin-top:8px;'
        'background:#0f172a;border:1px solid #334155;border-radius:8px;padding:10px 14px">'
        '<div id="sector-drill-title" style="font-size:12px;font-weight:700;color:#94a3b8;'
        'text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px"></div>'
        '<div id="sector-drill-body"></div>'
        '</div>'
    )

    # JS uses single-quote string delimiters (safe inside double-quoted Python strings)
    _djs = (
        "<script>var _sdi=" + _djson + ";"
        "var _sdActive=null;"
        "function _pct(v){"
        "if(v===null||v===undefined)return '<span style=\"color:#64748b\">n/a</span>';"
        "var c=v>=0?'#10b981':'#ef4444';"
        "var s=v>=0?'▲':'▼';"
        "return '<span style=\"color:'+c+';font-weight:600\">'+s+Math.abs(v).toFixed(2)+'%</span>';}"
        "function toggleSectorDrill(etf){"
        "var panel=document.getElementById('sector-drill-panel');"
        "var title=document.getElementById('sector-drill-title');"
        "var body=document.getElementById('sector-drill-body');"
        "if(_sdActive===etf){panel.style.display='none';_sdActive=null;return;}"
        "_sdActive=etf;panel.style.display='block';"
        "title.textContent=etf+' - Industries';"
        "var rows=_sdi[etf]||[];"
        "var h='<table style=\"width:100%;border-collapse:collapse\">';"
        "h+='<tr style=\"border-bottom:1px solid #1e293b\">"
        "<th style=\"text-align:left;padding:4px 8px;font-size:10px;color:#475569\">Industry</th>"
        "<th style=\"text-align:right;padding:4px 8px;font-size:10px;color:#475569\">ETF</th>"
        "<th style=\"text-align:right;padding:4px 8px;font-size:10px;color:#475569\">Day</th>"
        "<th style=\"text-align:right;padding:4px 8px;font-size:10px;color:#475569\">1W</th>"
        "<th style=\"text-align:right;padding:4px 8px;font-size:10px;color:#475569\">1M</th>"
        "<th style=\"text-align:right;padding:4px 8px;font-size:10px;color:#475569\">3M</th>"
        "</tr>';"
        "rows.forEach(function(r){"
        "h+='<tr style=\"border-bottom:1px solid #0f172a\">"
        "<td style=\"padding:5px 8px;font-size:11px;color:#cbd5e1\">'+r.lbl+'</td>"
        "<td style=\"padding:5px 8px;font-size:11px;color:#64748b;text-align:right\">'+r.sym+'</td>"
        "<td style=\"padding:5px 8px;font-size:11px;text-align:right\">'+_pct(r.day)+'</td>"
        "<td style=\"padding:5px 8px;font-size:11px;text-align:right\">'+_pct(r.w1)+'</td>"
        "<td style=\"padding:5px 8px;font-size:11px;text-align:right\">'+_pct(r.m1)+'</td>"
        "<td style=\"padding:5px 8px;font-size:11px;text-align:right\">'+_pct(r.m3)+'</td>"
        "</tr>';"
        "});h+='</table>';body.innerHTML=h;}"
        "</script>"
    )

    # Use plain concatenation — avoids "%" in _djson/_djs being misinterpreted
    # as format specifiers if we used % operator on sector_html.
    sector_html = (
        '<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;'
        'padding:12px 16px;margin-bottom:12px">'
        '<div style="font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;'
        'letter-spacing:.5px;margin-bottom:10px">Sector Heatmap &#8212; today</div>'
        '<div style="display:flex;gap:6px;flex-wrap:wrap">'
        + "".join(sector_cells)
        + '</div>'
        + _dpanel
        + _djs
        + '</div>'
    )

    # Market Pulse panel (Theme Tracker / S&P Sectors / Country ETFs / Highs-Lows)
    pulse_panel_html = build_pulse_panel_html(market_pulse, results, daily_breadth=kwargs.get("daily_breadth")) if market_pulse else ""

    # ── Table rows and detail cards ───────────────────────────────────────────
    rows_html  = ""
    cards_html = ""

    _strat_labels = {
        "minervini": "Minervini SEPA",
        "canslim":   "O'Neil CANSLIM",
        "reversion": "Mean Reversion",
    }
    for r in results:
        ticker = r["ticker"]
        setup  = r.get("setup", {})
        score  = r.get("score", {})
        valid  = r["valid_setup"]
        row_strat_label = _strat_labels.get(r.get("strategy", strategy), strategy_label)

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
            ' data-sector="%(sec)s" data-chg="%(chg_raw)s"'
            ' onclick="showDetail(\'%(t)s\')">'
            '<td style="padding:8px 12px;font-weight:600;color:#e2e8f0">%(t)s%(new_badge)s%(sdot)s</td>'
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
            "new_badge": ('<span style="background:#dc2626;color:#fff;border-radius:3px;padding:1px 5px;font-size:9px;font-weight:700;margin-left:4px;vertical-align:middle;letter-spacing:.5px">NEW</span>' if ticker in new_tickers else ""),
            "p": price_val, "e": entry_val, "s": stop_val, "t1": t1_val,
            "strat": r.get("strategy", strategy), "sec": sector_val, "chg_raw": chg_pct,
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
            row_strat_label,
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
            strat=r.get("strategy", strategy), sec=r.get('sector','').replace("'",'')
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
            ticker, row_strat_label,
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
/* ── Table sorting ─────────────────────────────────────────────── */
var _sortCol=null,_sortAsc=true;
function sortTable(col,type){
  var tbl=document.getElementById('scanner-table');
  if(!tbl)return;
  var tbody=tbl.tBodies[0];
  var rows=Array.from(tbody.rows);
  if(_sortCol===col){_sortAsc=!_sortAsc;}else{_sortCol=col;_sortAsc=true;}
  rows.sort(function(a,b){
    var av,bv;
    if(col==='chg'){
      av=parseFloat(a.getAttribute('data-chg')||0);
      bv=parseFloat(b.getAttribute('data-chg')||0);
    }else{
      var ac=a.cells[col],bc=b.cells[col];
      if(!ac||!bc)return 0;
      av=ac.textContent.trim().replace(/[$%+,]/g,'');
      bv=bc.textContent.trim().replace(/[$%+,]/g,'');
      if(type==='num'){av=parseFloat(av)||0;bv=parseFloat(bv)||0;}
    }
    if(av<bv)return _sortAsc?-1:1;
    if(av>bv)return _sortAsc?1:-1;
    return 0;
  });
  rows.forEach(function(r){tbody.appendChild(r);});
  tbl.querySelectorAll('th .sh').forEach(function(sp){sp.textContent='';});
  tbl.querySelectorAll('th[onclick]').forEach(function(th){
    var m=(th.getAttribute('onclick')||'').match(/sortTable\(([^,)]+)/);
    if(!m)return;
    var c=m[1].replace(/['"]/g,'');
    var match=isNaN(c)?c===String(_sortCol):parseInt(c)===_sortCol;
    if(match){var sp=th.querySelector('.sh');if(sp)sp.textContent=_sortAsc?' \u25b2':' \u25bc';}
  });
}
var WL_KEY='swingtrader_watchlist';
var _curStrat='all';
var _curSect='all';
function _applyFilters(){
  var s=_curStrat,sec=_curSect,shown=0;
  var rows=document.querySelectorAll('#scanner-table tbody tr');
  rows.forEach(function(r){
    var ok=(s==='all'||r.getAttribute('data-strategy')===s)&&(sec==='all'||r.getAttribute('data-sector')===sec);
    r.style.display=ok?'':'none';
    if(ok)shown++;
  });
  var lbl=document.getElementById('scan-count');
  if(lbl)lbl.textContent=shown;
  document.querySelectorAll('.tab-btn').forEach(function(btn){
    var bs=btn.getAttribute('data-s'),cnt=0;
    rows.forEach(function(r){
      if((bs==='all'||r.getAttribute('data-strategy')===bs)&&(sec==='all'||r.getAttribute('data-sector')===sec))cnt++;
    });
    btn.innerHTML=btn.innerHTML.replace(/\(\d+\)/,'('+cnt+')');
  });
}
function filterStrategy(s){
  _curStrat=s;
  _applyFilters();
  document.querySelectorAll('.tab-btn').forEach(function(b){
    b.classList.toggle('act',b.getAttribute('data-s')===s);
  });
}
function filterSector(sec){
  _curSect=sec;
  _applyFilters();
  document.querySelectorAll('.sec-btn').forEach(function(b){
    b.classList.toggle('act',b.getAttribute('data-sec')===sec);
  });
}
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
    if(row){
      var lp=parseFloat(row.getAttribute('data-price')); if(lp) item.price=lp;
      var le=parseFloat(row.getAttribute('data-entry')); if(le) item.entry=le;
      var ls=parseFloat(row.getAttribute('data-stop'));  if(ls) item.stop=ls;
      var lt=parseFloat(row.getAttribute('data-t1'));    if(lt) item.t1=lt;
    }
    return item;
  });
  if(!wl.length){panel.innerHTML='';return;}
  var rows=wl.map(function(item){
    var st=alertStatus(item),bar=distBar(item);
    return '<tr style="background:'+st.bg+';cursor:pointer" onclick="showDetail(this.cells[0].textContent.trim())">'
      +'<td style="padding:8px 12px;font-weight:600;color:#e2e8f0">'+item.ticker+'</td>'
      +'<td style="padding:8px 12px;color:#94a3b8;font-size:11px">'+(item.sector||'')+'</td>'
      +'<td style="padding:8px 10px"><span style="background:#1e3a5f;color:#60a5fa;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:600;text-transform:uppercase">'+(item.strategy||'--')+'</span></td>'
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
    +'<th style="padding:6px 12px;text-align:left;color:#64748b;font-size:11px">Strategy</th>'
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
  var panel=document.getElementById('detail-panel');
  if(panel){Array.from(panel.querySelectorAll('.detail-card')).forEach(function(el){
    el.style.display='none';panel.parentNode.insertBefore(el,panel.nextSibling);
  });}
  document.querySelectorAll('.detail-card').forEach(function(el){el.style.display='none';});
  var card=document.getElementById('detail-'+ticker);
  if(card&&panel){
    panel.innerHTML='';card.style.display='block';panel.appendChild(card);
    panel.scrollIntoView({behavior:'smooth'});updateWatchBtn(ticker);
  } else if(panel){
    var wl=getWL(),item=wl.find(function(x){return x.ticker===ticker;});
    if(!item)return;
    var rr=(item.t1&&item.entry&&item.stop&&(item.entry-item.stop)!==0)?((item.t1-item.entry)/(item.entry-item.stop)).toFixed(1):'--';
    panel.innerHTML='<div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px;max-width:700px">'
      +'<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">'
      +'<span style="font-size:20px;font-weight:700;color:#e2e8f0">'+item.ticker+'</span>'
      +'<span style="color:#94a3b8;font-size:12px">'+(item.sector||'')+'</span>'
      +'<span style="color:#a78bfa;font-size:11px;background:#1e1b4b;padding:2px 8px;border-radius:4px;text-transform:uppercase">'+(item.strategy||'')+'</span>'
      +'</div>'
      +'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px">'
      +'<div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center"><div style="color:#64748b;font-size:10px;margin-bottom:4px">PRICE</div><div style="color:#e2e8f0;font-size:15px;font-weight:700">$'+(item.price||'--')+'</div></div>'
      +'<div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center"><div style="color:#64748b;font-size:10px;margin-bottom:4px">ENTRY</div><div style="color:#4ade80;font-size:15px;font-weight:700">$'+(item.entry||'--')+'</div></div>'
      +'<div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center"><div style="color:#64748b;font-size:10px;margin-bottom:4px">STOP</div><div style="color:#f87171;font-size:15px;font-weight:700">$'+(item.stop||'--')+'</div></div>'
      +'<div style="background:#0f172a;border-radius:8px;padding:10px;text-align:center"><div style="color:#64748b;font-size:10px;margin-bottom:4px">TARGET</div><div style="color:#38bdf8;font-size:15px;font-weight:700">$'+(item.t1||'--')+'</div></div>'
      +'</div>'
      +'<div style="color:#94a3b8;font-size:12px">R/R: <b>'+rr+'</b> &nbsp;|&nbsp; Added: '+(item.added||'--')+'</div>'
      +'<div style="margin-top:8px;color:#475569;font-size:11px">&#9432; Not in current scan - showing saved data</div>'
      +'</div>';
    panel.scrollIntoView({behavior:'smooth'});
    updateWatchBtn(ticker);
  }
}
function copyJournal(ticker){
  var el=document.getElementById('journal-'+ticker);
  if(el){navigator.clipboard.writeText(el.value);}
}
document.addEventListener('DOMContentLoaded',function(){renderWatchlist();});
"""

    # ── Tabs bar HTML ─────────────────────────────────────────────────────
    if tabs_mode:
        _sc = {}
        for _r in results:
            _s = _r.get('strategy', strategy)
            _sc[_s] = _sc.get(_s, 0) + 1
        _tl = [('minervini','&#9889; Minervini'),('canslim','&#128200; CANSLIM'),
               ('reversion','&#8635; Reversion')]
        _tabs_html = '<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">'
        _tabs_html += ('<button class="tab-btn act" data-s="all"'
                       ' onclick="filterStrategy(&quot;all&quot;)">All (%d)</button>' % len(results))
        for _ts, _tlabel in _tl:
            _cnt = _sc.get(_ts, 0)
            if _cnt:
                _tabs_html += ('<button class="tab-btn" data-s="%s"'
                               ' onclick="filterStrategy(&quot;%s&quot;)">%s (%d)</button>'
                               % (_ts, _ts, _tlabel, _cnt))
        _tabs_html += '</div>'
    else:
        _tabs_html = ''

    # ── Sector filter bar ─────────────────────────────────────────────────────
    _sec_counts = {}
    for _r in results:
        _sn = (_r.get('sector') or '').strip() or 'Other'
        _sec_counts[_sn] = _sec_counts.get(_sn, 0) + 1
    _sec_sorted = sorted(_sec_counts.items(), key=lambda x: -x[1])
    if len(_sec_sorted) > 1:
        _sectors_html = ('<div style="display:flex;gap:6px;margin-bottom:14px;'
                         'flex-wrap:wrap;align-items:center">'
                         '<span style="color:#64748b;font-size:12px;margin-right:4px">'
                         'Sector:</span>'
                         '<button class="sec-btn act" data-sec="all"'
                         ' onclick="filterSector(this.getAttribute(\'data-sec\'))">All</button>')
        for _sn, _sc in _sec_sorted[:10]:
            _sectors_html += ('<button class="sec-btn" data-sec="%s"'
                              ' onclick="filterSector(this.getAttribute(\'data-sec\'))">'
                              '%s&nbsp;(%d)</button>' % (_sn, _sn, _sc))
        _sectors_html += '</div>'
    else:
        _sectors_html = ''

    # ── Assemble final HTML ───────────────────────────────────────────────────
    header_btns = (
        '<button onclick="exportCSV()" style="background:#1e3a5f;color:#60a5fa;border:none;'
        'border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px">&#8595; CSV</button>'
        '<button onclick="exportTV()" style="background:#312e81;color:#a78bfa;border:none;'
        'border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px">&#8631; TradingView</button>'
    )

    # Pulse panel with auto-expanded body for dedicated page
    pulse_page_html = ""
    if pulse_panel_html:
        pulse_page_html = (pulse_panel_html
            .replace('id="mpp-body-panel" style="display:none"',
                     'id="mpp-body-panel" style="display:block"')
            .replace('>&#9660; expand<', '>&#9650; collapse<')
        )

    # ── Pre / Post Market page builder ──────────────────────────────────────
    def _prepost_page(mode):
        chg_key   = "pre_chg"   if mode == "pre" else "post_chg"
        price_key = "pre_price" if mode == "pre" else "post_price"
        label     = "Pre-Market" if mode == "pre" else "Post-Market (AH)"
        icon      = "&#9728;&#65039;" if mode == "pre" else "&#9790;&#65039;"
        pid       = "premarket"  if mode == "pre" else "postmarket"
        tlbl      = "Pre" if mode == "pre" else "AH"
        movers = []; src_used = set(); ydata = yahoo or {}
        for r in results:
            sym = r.get("ticker", "")
            y   = ydata.get(sym, {})
            chg = y.get(chg_key); px = y.get(price_key)
            if chg is None or px is None:
                continue
            if abs(chg) < 1.0:   # skip tickers with < 1% pre/post-market move
                continue
            src_v = y.get("pre_post_source", "yahoo")
            movers.append({
                "ticker": sym, "sector": r.get("sector",""),
                "reg_close": r.get("price",0) or 0, "pm_price": px,
                "pm_chg": chg, "src": src_v,
                "dir": "up" if chg>0 else "down",
                "mkt_cap": r.get("mkt_cap") or 0,
                "rvol":    r.get("rel_volume") or 0,
            })
            src_used.add(src_v)
        movers.sort(key=lambda x: x["pm_chg"])
        src_label = ("IBKR + Yahoo" if "ibkr" in src_used and "yahoo" in src_used
                     else "IBKR" if "ibkr" in src_used else "Yahoo Finance")
        src_col   = "#10b981" if "ibkr" in src_used else "#64748b"
        if not movers:
            note = ("Данные доступны только в 16:00–20:00 ET (после закрытия рынка). "
                    "Последнее обновление дашборда могло быть в рабочие часы.")
            return ('<div class="swt-page" id="pg-' + pid
                    + '" style="display:none;padding:40px;text-align:center">'
                    '<div style="color:#64748b;font-size:14px;margin-bottom:8px">'
                    + icon + ' ' + label + ' — нет данных</div>'
                    '<div style="color:#475569;font-size:12px;max-width:480px;margin:0 auto">'
                    + note + '</div></div>\n')
        rows = ""
        for m in movers:
            cv  = m["pm_chg"]; cc = "#4ade80" if cv>0 else "#f87171"
            sgn = "+" if cv>0 else ""
            pmx = ("$%.2f" % m["pm_price"]) if m["pm_price"] else "—"
            rgx = ("$%.2f" % m["reg_close"]) if m["reg_close"] else "—"
            mc  = m["mkt_cap"] or 0
            mc_b = round(mc / 1e9, 2) if mc else 0
            rvol_v = round(m["rvol"] or 0, 2)
            sb = ('<span style="background:#0d3d2a;color:#10b981;border:1px solid #065f46;'
                  'font-size:9px;padding:1px 5px;border-radius:4px">IBKR</span>'
                  if m["src"] == "ibkr" else
                  '<span style="background:#1e293b;color:#64748b;border:1px solid #334155;'
                  'font-size:9px;padding:1px 5px;border-radius:4px">Yahoo</span>')
            mc_str = ("%.1fB" % mc_b if mc_b >= 1 else "%.0fM" % (mc/1e6)) if mc else "—"
            rvol_str = ("%.1fx" % rvol_v) if rvol_v else "—"
            rows += ('<tr data-dir="' + m["dir"]
                     + '" data-mktcap="' + str(mc_b)
                     + '" data-rvol="' + str(rvol_v)
                     + '" data-ticker="' + m["ticker"].lower()
                     + '" data-chg="' + str(round(m["pm_chg"], 4))
                     + '" style="border-bottom:1px solid #1e293b">'
                     '<td style="padding:8px 12px;font-weight:700;color:#e2e8f0">' + m["ticker"] + '</td>'
                     '<td style="padding:8px 12px;color:#64748b;font-size:12px">' + m["sector"][:22] + '</td>'
                     '<td style="padding:8px 12px;color:#94a3b8">' + rgx + '</td>'
                     '<td style="padding:8px 12px;color:#a78bfa;font-weight:600">' + pmx + '</td>'
                     '<td style="padding:8px 12px;font-weight:700;color:' + cc + '">'
                     + sgn + ("%.2f%%" % cv) + '</td>'
                     '<td style="padding:8px 12px;color:#94a3b8;font-size:12px">' + mc_str + '</td>'
                     '<td style="padding:8px 12px;color:#94a3b8;font-size:12px">' + rvol_str + '</td>'
                     '<td style="padding:8px 12px">' + sb + '</td></tr>\n')
        ups = sum(1 for m in movers if m["dir"] == "up"); dns = len(movers) - ups
        sel_s = ('background:#1e293b;color:#94a3b8;border:1px solid #334155;'
                 'border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer')
        inp_s = ('background:#1e293b;color:#e2e8f0;border:1px solid #334155;'
                 'border-radius:6px;padding:4px 10px;font-size:12px;width:140px;outline:none')
        toolbar = (
            '<div style="display:flex;align-items:center;justify-content:space-between;'
            'flex-wrap:wrap;gap:10px;margin-bottom:16px">\n'
            '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">\n'
            '<div>\n'
            '<h2 style="font-size:16px;font-weight:700;color:#e2e8f0">' + icon + ' ' + label + '</h2>\n'
            + '<div id="cnt-' + pid + '" style="font-size:11px;color:#64748b;margin-top:2px">'
            + str(len(movers)) + ' stocks'
            + ' &nbsp;·&nbsp;<span style="color:#4ade80">&#8679; ' + str(ups) + '</span>'
            + ' &nbsp;<span style="color:#f87171">&#8681; ' + str(dns) + '</span>'
            + '</div></div>\n'
            + '<div style="display:flex;gap:6px">\n'
            + '<button class="sec-btn act" id="dir-all-' + pid + '" data-dir="all" onclick="setPMDir(\'' + pid + '\',\'all\',this)">All</button>\n'
            + '<button class="sec-btn" id="dir-up-' + pid + '" data-dir="up" onclick="setPMDir(\'' + pid + '\',\'up\',this)">&#8679; Up</button>\n'
            + '<button class="sec-btn" id="dir-dn-' + pid + '" data-dir="down" onclick="setPMDir(\'' + pid + '\',\'down\',this)">&#8681; Down</button>\n'
            + '</div>\n</div>\n'
            + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">\n'
            + '<label style="color:#64748b;font-size:11px">SVol (10d) ≥</label>'
            + '<select id="svol-' + pid + '" style="' + sel_s + '" onchange="filterPMAll(\'' + pid + '\')">'
            + '<option value="0">Off</option><option value="1">1x</option>'
            + '<option value="1.5">1.5x</option><option value="2">2x</option>'
            + '<option value="3">3x</option><option value="5">5x</option></select>\n'
            + '<label style="color:#64748b;font-size:11px">Mkt Cap ≥</label>'
            + '<select id="mktcap-' + pid + '" style="' + sel_s + '" onchange="filterPMAll(\'' + pid + '\')">'
            + '<option value="0">Off</option><option value="0.3">Micro $300M+</option>'
            + '<option value="2">Small $2B+</option><option value="10">Mid $10B+</option>'
            + '<option value="50">Large $50B+</option></select>\n'
            + '<input id="search-' + pid + '" type="text" placeholder="Search..." style="' + inp_s + '"'
            + ' oninput="filterPMAll(\'' + pid + '\')"/>\n'
            + '<span style="background:#1a2332;color:' + src_col
            + ';border:1px solid #334155;font-size:11px;padding:3px 10px;border-radius:6px">'
            + src_label + '</span>\n'
            + '</div>\n</div>\n'
        )
        return ('<div class="swt-page" id="pg-' + pid + '" style="display:none">\n'
                + toolbar
                + '<div style="background:#1e293b;border:1px solid #334155;border-radius:12px;overflow-x:auto">\n'
                + '<table id="tbl-' + pid + '">\n<thead><tr>\n'
                + '<th>Ticker</th><th>Sector</th><th>Close</th>'
                + '<th style="color:#a78bfa">' + tlbl + ' Price</th>'
                + '<th style="color:#a78bfa;cursor:pointer;user-select:none" onclick="sortPMChg(\'' + pid + '\')" title="Sort by change">' + tlbl + ' Chg% &#8597;</th>'
                + '<th>Mkt Cap</th><th>SVol (10d)</th><th>Source</th>\n</tr></thead><tbody>\n'
                + rows + '</tbody></table></div>\n</div>\n')

    _pm_page_html   = _prepost_page("pre")
    _post_page_html = _prepost_page("post")

    # ── Page-switching JS ───────────────────────────────────────────────────
    _page_js = (
        'function showPage(pid,el){'
        'document.querySelectorAll(".swt-page").forEach(function(p){p.style.display="none";});'
        'document.querySelectorAll(".nav-item").forEach(function(n){n.classList.remove("active");});'
        'var pg=document.getElementById("pg-"+pid);if(pg)pg.style.display="block";'
        'if(el){el.classList.add("active");}'
        'else{var nv=document.getElementById("nav-"+pid);if(nv)nv.classList.add("active");}'
        'try{localStorage.setItem("swt_page",pid);}catch(e){}}'
        '\nfunction goToPulsePane(pane,el){'
        'showPage("pulse");'
        'if(el){document.querySelectorAll(".nav-item").forEach(function(n){n.classList.remove("active");});el.classList.add("active");}'
        'setTimeout(function(){'
        'var btn=null;'
        'document.querySelectorAll(".mpp-tab").forEach(function(b){'
        'var oc=b.getAttribute("onclick")||"";'
        'if(oc.indexOf("\'"+pane+"\'")>-1)btn=b;'
        '});'
        'if(btn&&typeof mppSwitch==="function")mppSwitch(btn,pane);'
        'var b=document.getElementById("mpp-body-panel");'
        'var ic=document.getElementById("mpp-toggle-icon");'
        'if(b&&b.style.display==="none"){b.style.display="block";if(ic)ic.textContent="▲ collapse";}'
        '},0);}'
        '\nfunction setPMDir(pid,dir,btn){'
        'document.querySelectorAll("#dir-all-"+pid+",#dir-up-"+pid+",#dir-dn-"+pid)'
        '.forEach(function(b){b.classList.remove("act");});'
        'btn.classList.add("act");btn.setAttribute("data-dir",dir);filterPMAll(pid);}'
        '\nfunction filterPMAll(pid){'
        'var tbl=document.getElementById("tbl-"+pid);if(!tbl)return;'
        'var dirBtn=document.querySelector("#dir-all-"+pid+".act,#dir-up-"+pid+".act,#dir-dn-"+pid+".act");'
        'var dir=dirBtn?dirBtn.getAttribute("data-dir"):"all";'
        'var svolEl=document.getElementById("svol-"+pid);'
        'var mcEl=document.getElementById("mktcap-"+pid);'
        'var srchEl=document.getElementById("search-"+pid);'
        'var svolMin=svolEl?parseFloat(svolEl.value)||0:0;'
        'var mcMin=mcEl?parseFloat(mcEl.value)||0:0;'
        'var q=srchEl?srchEl.value.toLowerCase().trim():"";'
        'var vis=0,tot=0;'
        'tbl.querySelectorAll("tbody tr").forEach(function(r){'
        'tot++;var ok=true;'
        'if(dir!=="all"&&r.getAttribute("data-dir")!==dir)ok=false;'
        'if(ok&&svolMin>0){var rv=parseFloat(r.getAttribute("data-rvol"))||0;if(rv<svolMin)ok=false;}'
        'if(ok&&mcMin>0){var mc=parseFloat(r.getAttribute("data-mktcap"))||0;if(mc<mcMin)ok=false;}'
        'if(ok&&q){var tk=r.getAttribute("data-ticker")||"";if(tk.indexOf(q)===-1)ok=false;}'
        'r.style.display=ok?"":"none";if(ok)vis++;});'
        'var cnt=document.getElementById("cnt-"+pid);'
        'if(cnt)cnt.textContent=vis+" / "+tot+" stocks";}'
        '\nfunction filterPM(pid,dir,btn){setPMDir(pid,dir,btn);}'
        '\nfunction sortPMChg(pid){''var tbl=document.getElementById("tbl-"+pid);if(!tbl)return;''var tbody=tbl.querySelector("tbody");''var rows=Array.from(tbody.querySelectorAll("tr"));''var cur=tbl.getAttribute("data-chg-sort")||"none";''var asc=cur!=="asc";''tbl.setAttribute("data-chg-sort",asc?"asc":"desc");''rows.sort(function(a,b){''var av=parseFloat(a.getAttribute("data-chg")||"0");''var bv=parseFloat(b.getAttribute("data-chg")||"0");''return asc?av-bv:bv-av;});''rows.forEach(function(r){tbody.appendChild(r);});''var th=tbl.querySelector("th[onclick*=\'sortPMChg\']");''if(th){var b=th.innerHTML.replace(/[\u25b2\u25bc\u2195]/g,"").trim();''th.innerHTML=b+" "+(asc?"&#9650;":"&#9660;");}}'
        '\ndocument.addEventListener("DOMContentLoaded",function(){'
        'try{var s=localStorage.getItem("swt_page");'
        'if(s&&document.getElementById("pg-"+s)){showPage(s);return;}'
        '}catch(e){}showPage("scanner");});\n'
    )

    html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>Swing Trader Dashboard &mdash; ' + now + '</title>\n'
        '<style>\n'
        '* { box-sizing:border-box; margin:0; padding:0; }\n'
        'body { background:#0f172a; color:#e2e8f0;'
        ' font-family:system-ui,-apple-system,sans-serif; display:flex; min-height:100vh; }\n'
        '#sidebar { width:200px; min-width:200px; background:#131e2e;'
        ' border-right:1px solid #334155;'
        ' position:fixed; top:0; left:0; height:100vh; overflow-y:auto;'
        ' z-index:200; display:flex; flex-direction:column; }\n'
        '#main-content { margin-left:200px; flex:1; min-width:0;'
        ' display:flex; flex-direction:column; }\n'
        '#topbar { padding:14px 24px 10px; border-bottom:1px solid #334155;'
        ' position:sticky; top:0; background:#0f172a; z-index:100; }\n'
        '.swt-page { padding:20px 24px; }\n'
        '.nav-item { display:flex; align-items:center; gap:10px; padding:9px 16px;'
        ' color:#94a3b8; text-decoration:none; font-size:13px; cursor:pointer;'
        ' transition:all .15s; border-left:3px solid transparent; }\n'
        '.nav-item:hover { background:#0c1520; color:#e2e8f0; }\n'
        '.nav-item.active { color:#60a5fa; background:rgba(59,130,246,.12);'
        ' border-left-color:#3b82f6; }\n'
        '.nav-badge { margin-left:auto; background:#1e293b; color:#94a3b8;'
        ' font-size:10px; padding:1px 6px; border-radius:10px;'
        ' min-width:22px; text-align:center; }\n'
        '.nav-section { padding:14px 16px 5px; font-size:10px; font-weight:700;'
        ' color:#3d5166; text-transform:uppercase; letter-spacing:.5px; }\n'
        'table { width:100%; border-collapse:collapse; }\n'
        'tr:hover { background:#1e293b !important; }\n'
        'th { color:#64748b; font-size:11px; font-weight:500; text-align:left;'
        ' padding:8px 12px; border-bottom:1px solid #334155; }\n'
        'th[onclick] { cursor:pointer; user-select:none; }\n'
        'th[onclick]:hover { color:#cbd5e1 !important; }\n'
        '.sh { font-size:10px; color:#94a3b8; }\n'
        '.tab-btn{background:#1e293b;color:#94a3b8;border:1px solid #334155;'
        'border-radius:8px;padding:5px 16px;cursor:pointer;font-size:13px;'
        'font-weight:500;transition:all .15s;white-space:nowrap}\n'
        '.tab-btn.act{background:#1e3a5f;color:#60a5fa;border-color:#3b82f6}\n'
        '.tab-btn:hover{border-color:#475569;color:#e2e8f0}\n'
        '.sec-btn{background:#1e293b;color:#94a3b8;border:1px solid #334155;'
        'border-radius:6px;padding:3px 10px;cursor:pointer;font-size:12px;'
        'transition:all .15s;white-space:nowrap}\n'
        '.sec-btn.act{background:#162416;color:#4ade80;border-color:#22c55e}\n'
        '.sec-btn:hover{border-color:#475569;color:#e2e8f0}\n'
        'details summary { padding:6px 0; cursor:pointer; }\n'
        '.detail-card { background:#1e293b; border:1px solid #334155;'
        ' border-radius:12px; padding:20px; margin-top:16px; }\n'
        '</style>\n</head>\n<body>\n'

        # ── Sidebar ──────────────────────────────────────────────────────────
        '<nav id="sidebar">\n'
        '  <div style="padding:14px 16px 14px;border-bottom:1px solid #334155">\n'
        '    <div style="font-size:14px;font-weight:700;color:#e2e8f0">'
        '&#9889; SwingTrader</div>\n'
        '    <div style="font-size:10px;color:#4d6a82;margin-top:2px">' + now + '</div>\n'
        '  </div>\n'
        '  <div class="nav-section">Trading</div>\n'
        '  <a class="nav-item active" id="nav-scanner" href="#"'
        ' onclick="showPage(\'scanner\',this);return false">'
        '<span>&#9889;</span><span>Scanner</span>'
        '<span class="nav-badge">' + str(len(results)) + '</span>'
        '</a>\n'
        '  <a class="nav-item" id="nav-watchlist" href="#"'
        ' onclick="showPage(\'watchlist\',this);return false">'
        '<span>&#11088;</span><span>Watchlist</span>'
        '</a>\n'
        '  <div class="nav-section">Market Data</div>\n'
        '  <a class="nav-item" id="nav-pulse" href="#"'
        ' onclick="showPage(\'pulse\',this);return false">'
        '<span>&#128200;</span><span>Market Pulse</span>'
        '</a>\n'
        '  <a class="nav-item" id="nav-breadth" href="#"'
        ' onclick="goToPulsePane(\'breadth\',this);return false">'
        '<span>&#128268;</span><span>Breadth</span>'
        '</a>\n'
        '  <a class="nav-item" id="nav-highs" href="#"'
        ' onclick="goToPulsePane(\'highs\',this);return false">'
        '<span>&#128202;</span><span>Highs / Lows</span>'
        '</a>\n'
        '  <a class="nav-item" id="nav-sectors" href="#"'
        ' onclick="showPage(\'sectors\',this);return false">'
        '<span>&#127759;</span><span>Sectors</span>'
        '</a>\n'
        '  <div class="nav-section">Extended Hours</div>\n'
        '  <a class="nav-item" id="nav-premarket" href="#"'
        ' onclick="showPage(\'premarket\',this);return false">'
        '<span>&#9728;&#65039;</span><span>Pre-Market</span>'
        '</a>\n'
        '  <a class="nav-item" id="nav-postmarket" href="#"'
        ' onclick="showPage(\'postmarket\',this);return false">'
        '<span>&#9790;&#65039;</span><span>Post-Market</span>'
        '</a>\n'
        '  <div style="margin-top:auto;padding:10px 16px;border-top:1px solid #1e293b">\n'
        '    <div style="font-size:11px;color:#3d5166">'
        'Valid: <span style="color:#10b981">' + str(valid_count) + '</span>'
        '&nbsp;&nbsp;'
        'Scanned: <span style="color:#64748b">' + str(len(results)) + '</span>'
        '</div>\n'
        '  </div>\n'
        '</nav>\n\n'

        # ── Main content area ─────────────────────────────────────────────────
        '<div id="main-content">\n'

        # Top bar (always visible: title + market stats line)
        '<div id="topbar">\n'
        '  <div style="display:flex;align-items:center;justify-content:space-between;'
        'margin-bottom:10px">\n'
        '    <div>\n'
        '      <h1 style="font-size:18px;font-weight:600;color:#e2e8f0">'
        '&#9889; Swing Trader Dashboard</h1>\n'
        '      <div style="color:#64748b;font-size:12px;margin-top:1px">'
        + ('All Strategies' if tabs_mode else strategy_label) + '</div>\n'
        '    </div>\n'
        '    <div style="display:flex;gap:10px;align-items:center">\n'
        '      ' + header_btns + '\n'
        '    </div>\n'
        '  </div>\n'
        + market_pulse_html + '\n'
        '</div>\n\n'  # end topbar

        # ── Page: Scanner (default, shown) ───────────────────────────────────
        '<div class="swt-page" id="pg-scanner" style="display:block">\n'
        + (_tabs_html + '\n' if _tabs_html else '')
        + (_sectors_html + '\n' if _sectors_html else '')
        + '<div style="background:#1e293b;border:1px solid #334155;'
        'border-radius:12px;overflow-x:auto;margin-bottom:20px">\n'
        '<table id="scanner-table">\n<thead>\n<tr>\n'
        '<th onclick="sortTable(0,\'str\')">Ticker <span class="sh"></span></th>'
        '<th onclick="sortTable(1,\'str\')">Sector <span class="sh"></span></th>'
        '<th onclick="sortTable(2,\'num\')">Price <span class="sh"></span></th>'
        '<th onclick="sortTable(\'chg\',\'num\')">Day% <span class="sh"></span></th>\n'
        '<th style="color:#a78bfa">Pre</th><th style="color:#818cf8">Post</th>\n'
        '<th style="color:#fb923c">Earnings</th>\n'
        '<th>Entry</th><th>Stop</th><th>T1</th>'
        '<th onclick="sortTable(10,\'num\')">R/R <span class="sh"></span></th>'
        '<th>Conv.</th><th>&#10003;</th>\n'
        '<th title="Chart pattern (VCP/Cup/Flat)" style="color:#34d399">Pattern</th>\n'
        '<th title="Insider activity (30d)" style="color:#a78bfa">Insider</th>\n'
        '<th title="Top institutional holder" style="color:#818cf8">Inst. Top</th>\n'
        '<th title="Watchlist"'
        ' style="position:sticky;right:0;background:#1e293b;z-index:2">&#9734;</th>\n'
        '</tr>\n</thead>\n<tbody>\n'
        + rows_html
        + '</tbody>\n</table>\n</div>\n\n'
        '<div id="detail-panel">'
        '<div style="color:#64748b;font-size:13px;text-align:center;padding:20px">'
        '&#8593; Click a row to see full trade setup</div></div>\n'
        + cards_html + '\n\n'
        '</div>\n'  # end pg-scanner

        # ── Page: Watchlist ───────────────────────────────────────────────────
        '<div class="swt-page" id="pg-watchlist" style="display:none">\n'
        '<div id="watchlist-panel"></div>\n'
        '</div>\n'

        # ── Page: Market Pulse (auto-expanded) ────────────────────────────────
        + ('<div class="swt-page" id="pg-pulse" style="display:none">\n'
           + pulse_page_html + '\n</div>\n'
           if pulse_page_html else
           '<div class="swt-page" id="pg-pulse" style="display:none">'
           '<p style="color:#64748b;padding:40px;text-align:center">'
           'Market Pulse data unavailable</p></div>\n')

        # ── Page: Sectors heatmap ──────────────────────────────────────────────
        + '<div class="swt-page" id="pg-sectors" style="display:none">\n'
        + sector_html + '\n'
        + '</div>\n'

        + _pm_page_html
        + _post_page_html

        + '</div>\n'  # end #main-content
        '<script>' + js + _page_js + '</script>\n'
        '</body></html>'
    )

    return html


def main():
    parser = argparse.ArgumentParser(description="TradingView data enricher + trade setup generator")
    parser.add_argument("--tickers", help="Comma-separated ticker list (NVDA,TSLA,...)")
    parser.add_argument("--file",    help="Text file with tickers (one per line)")
    parser.add_argument("--strategy", choices=["minervini", "canslim", "reversion"],
                        default="minervini")
    parser.add_argument("--scan", action="append", metavar="FILE",
                        help="JSON scan file (repeat for --tabs mode)")
    parser.add_argument("--tabs", action="store_true",
                        help="Multi-strategy tabbed dashboard")
    parser.add_argument("--global", dest="global_markets", action="store_true",
                        help="Search global exchanges (slower)")
    parser.add_argument("--html",   action="store_true", help="Output HTML dashboard")
    parser.add_argument("--output", help="Output file path (for --html)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    parser.add_argument("--new-tickers", dest="new_tickers_file", metavar="FILE",
                        help="JSON file with list of new ticker symbols (for NEW badge)")
    args = parser.parse_args()

    tickers = []
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    elif args.file:
        tickers = [l.strip().upper() for l in Path(args.file).read_text().splitlines() if l.strip()]
    elif getattr(args, "tabs", False) and args.scan:
        pass  # tabs mode uses --scan files, not stdin
    elif not sys.stdin.isatty():
        data = json.load(sys.stdin)
        tickers = [t["ticker"] for t in data.get("tickers", [])]
        if not args.strategy and data.get("strategy"):
            args.strategy = data["strategy"]
        print(f"✅ Received {len(tickers)} tickers from pipe (strategy: {args.strategy})", file=sys.stderr)

    # ── Multi-strategy tabs mode ─────────────────────────────────────────
    if getattr(args, "tabs", False) and args.scan:
        if not args.html:
            print("❌ --tabs requires --html", file=sys.stderr); sys.exit(1)
        all_results = []
        for _sf in args.scan:
            with open(_sf, encoding="utf-8") as _f:
                try:
                    _sd = json.load(_f)
                except (json.JSONDecodeError, ValueError) as _e:
                    print(f"⚠️  Invalid JSON in {_sf}: {_e}", file=sys.stderr)
                    _sd = {"strategy": "minervini", "tickers": []}
            _strat = _sd.get("strategy", "minervini")
            _ticker_meta = {
                (t["ticker"] if isinstance(t, dict) else str(t)): t
                for t in _sd.get("tickers", []) if isinstance(t, dict)
            }
            _tickers = list(_ticker_meta.keys())
            if not _tickers:
                print(f"⚠️  No tickers in {_sf}", file=sys.stderr); continue
            print(f"📊 Enriching {len(_tickers)} [{_strat}]...", file=sys.stderr)
            _enriched = enrich_tickers(_tickers, _strat, args.global_markets, fv_meta=_ticker_meta)
            all_results.extend(_enriched)
        print("📊 Fetching market context...", file=sys.stderr)
        _mctx = fetch_market_context()
        _ytk = [r["ticker"] for r in all_results[:150]]
        print(f"🌙 Pre/post + earnings for {len(_ytk)} tickers...", file=sys.stderr)
        _yahoo = fetch_yahoo_data(_ytk)
        print("🌐 Fetching Market Pulse data...", file=sys.stderr)
        _mpulse = fetch_market_pulse_data()
        print("📊 Fetching daily breadth data...", file=sys.stderr)
        _dbreadth = fetch_daily_breadth_data()
        _new_tickers = []
        if getattr(args, 'new_tickers_file', None) and Path(args.new_tickers_file).exists():
            try:
                _new_tickers = json.loads(Path(args.new_tickers_file).read_text(encoding='utf-8'))
            except Exception:
                pass
        html = build_html_dashboard(all_results, "all", _mctx, _yahoo, tabs_mode=True, new_tickers=_new_tickers, market_pulse=_mpulse, daily_breadth=_dbreadth)
        out_path = args.output or ("watchlist_tabs_%s.html" % datetime.now().strftime("%Y-%m-%d"))
        Path(out_path).write_text(html, encoding="utf-8")
        print(f"✅ Tabs dashboard saved: {out_path}", file=sys.stderr)
        return

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
        print("🌐 Fetching Market Pulse data...", file=sys.stderr)
        market_pulse = fetch_market_pulse_data()
        print("📊 Fetching daily breadth data...", file=sys.stderr)
        _dbr = fetch_daily_breadth_data()
        _new_tickers = []
        if getattr(args, 'new_tickers_file', None) and Path(args.new_tickers_file).exists():
            try:
                _new_tickers = json.loads(Path(args.new_tickers_file).read_text(encoding='utf-8'))
            except Exception:
                pass
        html = build_html_dashboard(results, args.strategy, market_ctx, yahoo, new_tickers=_new_tickers, market_pulse=market_pulse, daily_breadth=_dbr)
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
