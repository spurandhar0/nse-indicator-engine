"""
NSE Indices Fetch — v1
=======================
Fetches major NSE/BSE index data from Yahoo Finance.
Saves to signal_data/indices.json for the dashboard.
"""

import json, time, requests
from datetime import datetime
from pathlib import Path
import pytz

SIGNAL_DIR   = Path("signal_data")
INDICES_FILE = SIGNAL_DIR / "indices.json"
SIGNAL_DIR.mkdir(exist_ok=True)

IST = pytz.timezone("Asia/Kolkata")

INDICES = [
    {"symbol": "^NSEI",          "name": "Nifty 50",       "color": "#4fc3f7"},
    {"symbol": "^BSESN",         "name": "Sensex",         "color": "#81c784"},
    {"symbol": "^NSEBANK",       "name": "Nifty Bank",     "color": "#ffb74d"},
    {"symbol": "^CNXIT",         "name": "Nifty IT",       "color": "#ce93d8"},
    {"symbol": "NIFMDCP100.NS",  "name": "Nifty Midcap",   "color": "#f48fb1"},
    {"symbol": "^NSMIDCP",       "name": "Nifty Midcap50", "color": "#80cbc4"},
]

def fetch_yahoo(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        res  = data.get("chart", {}).get("result", [None])[0]
        if not res:
            return None
        meta    = res.get("meta", {})
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose") or 0
        curr       = meta.get("regularMarketPrice") or 0
        chg        = round(curr - prev_close, 2) if prev_close else 0
        chg_pct    = round(chg / prev_close * 100, 2) if prev_close else 0
        ts         = meta.get("regularMarketTime", 0)
        time_str   = datetime.fromtimestamp(ts, tz=IST).strftime("%d %b %Y %H:%M") if ts else "—"
        return {
            "price":     round(curr, 2),
            "prev":      round(prev_close, 2),
            "change":    chg,
            "change_pct": chg_pct,
            "time":      time_str,
        }
    except Exception as e:
        print(f"  Error fetching {ticker}: {e}")
        return None

results = []
for idx in INDICES:
    d = fetch_yahoo(idx["symbol"])
    time.sleep(0.5)  # polite delay
    if d:
        results.append({
            "name":       idx["name"],
            "symbol":     idx["symbol"],
            "color":      idx["color"],
            **d
        })
        arrow = "▲" if d["change_pct"] >= 0 else "▼"
        print(f"  {idx['name']}: {d['price']:,.2f} {arrow}{d['change_pct']:+.2f}%")
    else:
        print(f"  {idx['name']}: FAILED")

out = {
    "updated": datetime.now(IST).strftime("%d %b %Y %H:%M IST"),
    "indices": results
}

with open(INDICES_FILE, "w") as f:
    json.dump(out, f, indent=2)

print(f"\n✅ Saved {len(results)} indices to {INDICES_FILE}")
