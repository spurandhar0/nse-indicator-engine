"""
NSE Indicator Signal Generator — v1
=====================================
Reads all output_data/NSE_Indicator_Report_*.csv files.
For each date + symbol, counts Bullish/Bearish/Neutral entries.

SIGNAL CRITERIA:
  - total_count > 25
  - bullish_pct > 90%  (bullish / total * 100)

Output: signal_data/signals.json
"""

import os, json, glob, re
import pandas as pd
from datetime import datetime
from pathlib import Path

OUTPUT_DIR   = Path("output_data")
SIGNAL_DIR   = Path("signal_data")
SIGNALS_FILE = SIGNAL_DIR / "signals.json"
SIGNAL_DIR.mkdir(exist_ok=True)

# ── Load existing signals (to avoid re-processing) ──────────────────────────
if SIGNALS_FILE.exists():
    with open(SIGNALS_FILE) as f:
        existing = json.load(f)
else:
    existing = []

existing_keys = {(s["symbol"], s["signal_date"]) for s in existing}
print(f"Existing signals loaded: {len(existing)}")

new_signals = []

# ── Process each output CSV ──────────────────────────────────────────────────
csv_files = sorted(OUTPUT_DIR.glob("NSE_Indicator_Report_*.csv"))
print(f"Found {len(csv_files)} output CSVs")

for csv_file in csv_files:
    try:
        df = pd.read_csv(csv_file, low_memory=False)
        if df.empty:
            print(f"  SKIP {csv_file.name} — empty")
            continue
        required = {"NSE_SYMBOL", "Trend", "CLOSE_PRICE"}
        # Accept either CLOSE_PRICE or CLOSE column
        close_col = "CLOSE_PRICE" if "CLOSE_PRICE" in df.columns else "CLOSE" if "CLOSE" in df.columns else None
        if "NSE_SYMBOL" not in df.columns or "Trend" not in df.columns or not close_col:
            print(f"  SKIP {csv_file.name} — missing columns (have: {list(df.columns)[:10]})")
            continue

        # Get date from filename: NSE_Indicator_Report_15-May-2026.csv
        fname = csv_file.stem  # NSE_Indicator_Report_15-May-2026
        date_part = fname.replace("NSE_Indicator_Report_", "")
        try:
            date_str = datetime.strptime(date_part, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            # Try Date column
            if "Date" in df.columns:
                date_str = str(df["Date"].dropna().iloc[0])[:10]
            else:
                print(f"  SKIP {csv_file.name} — can't parse date")
                continue

        # Filter EQ series only
        if "SERIES" in df.columns:
            df = df[df["SERIES"].fillna("").str.strip() == "EQ"]
        if df.empty:
            continue

        # Group by symbol
        grp = df.groupby("NSE_SYMBOL")
        for symbol, sdf in grp:
            symbol = str(symbol).strip()
            if not symbol or symbol.lower() in ("nan", "none", ""):
                continue

            total    = len(sdf)
            bullish  = int((sdf["Trend"] == "Bullish").sum())
            bearish  = int((sdf["Trend"] == "Bearish").sum())
            neutral  = int((sdf["Trend"] == "Neutral").sum())
            bull_pct = round(bullish / total * 100, 1) if total > 0 else 0

            if total > 25 and bull_pct > 90:
                # Get most-common close price for this symbol
                prices = pd.to_numeric(sdf[close_col], errors="coerce").dropna()
                if prices.empty:
                    continue
                close = round(float(prices.mode().iloc[0] if len(prices) > 1 else prices.iloc[0]), 2)

                key = (symbol, date_str)
                if key not in existing_keys:
                    new_signals.append({
                        "symbol":        symbol,
                        "signal_date":   date_str,
                        "close":         close,
                        "bullish_count": bullish,
                        "bearish_count": bearish,
                        "neutral_count": neutral,
                        "total_count":   total,
                        "bullish_pct":   bull_pct
                    })
                    existing_keys.add(key)

        print(f"  Processed {csv_file.name} ({date_str})")

    except Exception as e:
        print(f"  ERROR {csv_file.name}: {e}")
        import traceback; traceback.print_exc()

# ── Save ─────────────────────────────────────────────────────────────────────
if new_signals:
    all_signals = existing + new_signals
    all_signals.sort(key=lambda x: x["signal_date"])
    with open(SIGNALS_FILE, "w") as f:
        json.dump(all_signals, f, indent=2)
    print(f"\n✅ Added {len(new_signals)} new signals. Total: {len(all_signals)}")
else:
    # Always write file (even if empty list) so downstream scripts can load it
    if not SIGNALS_FILE.exists():
        with open(SIGNALS_FILE, "w") as f:
            json.dump([], f)
    print(f"\nℹ️  No new signals found. Total stays at {len(existing)}")
