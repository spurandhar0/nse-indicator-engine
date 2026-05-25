"""
NSE Indicator Signal Generator — v2
=====================================
Reads directly from indicator_data/{date}/*.csv folders.
Counts Bullish / Bearish / Neutral per symbol per day.

SIGNAL CRITERIA:
  - total_count > 25
  - bullish_pct > 90%  (bullish_count / total_count * 100)

Symbol resolution: mc_nse_mapping.csv (MC_Code → NSE_Symbol)
Close price: bhav_data/{month}/sec_bhavdata_full_{ddmmyyyy}.csv

Output: signal_data/signals.json
"""

import os, json, glob, re, ast
import pandas as pd
from datetime import datetime
from pathlib import Path

INDICATOR_ROOT = Path("indicator_data")
BHAV_ROOT      = Path("bhav_data")
MAPPING_PATH   = Path("src/mc_nse_mapping.csv")
SIGNAL_DIR     = Path("signal_data")
SIGNALS_FILE   = SIGNAL_DIR / "signals.json"
SIGNAL_DIR.mkdir(exist_ok=True)

# ── File classification ───────────────────────────────────────────────────────
BULLISH_FILES = {
    "bullish", "bullish_breakout", "candlestick_bullish",
    "moving_average_bullish", "range_breakout_bullish",
    "supertrend_bullish", "volume_delivery_bullish",
    "stock_ideas", "technical_picks_active", "analysts_choice", "gainers"
}
BEARISH_FILES = {
    "bearish", "bearish_breakout", "candlestick_bearish",
    "moving_average_bearish", "range_breakout_bearish",
    "supertrend_bearish", "volume_delivery_bearish",
    "technical_picks_inactive", "chart_patterns_inactive", "losers"
}
# everything else = NEUTRAL

def classify_file(fname):
    stem = Path(fname).stem.lower()
    if stem in BULLISH_FILES:
        return "bullish"
    if stem in BEARISH_FILES:
        return "bearish"
    return "neutral"

# ── Load MC → NSE mapping ─────────────────────────────────────────────────────
mc_to_nse = {}
if MAPPING_PATH.exists():
    try:
        mdf = pd.read_csv(MAPPING_PATH, low_memory=False)
        mdf.columns = mdf.columns.str.strip()
        for _, row in mdf.iterrows():
            mc  = str(row.get("MC_Code", "")).strip()
            nse = str(row.get("NSE_Symbol", "")).strip()
            if mc and nse and nse.lower() not in ("nan", "none", ""):
                mc_to_nse[mc.upper()] = nse.upper()
        print(f"MC mapping loaded: {len(mc_to_nse)} entries")
    except Exception as e:
        print(f"Warning: could not load mapping: {e}")

# ── Build bhavcopy lookup: date_str → {SYMBOL: close_price} ──────────────────
print("Building bhavcopy price cache...")
bhavcopy_cache = {}  # {date_str: {NSE_SYMBOL: close_price}}

MONTH_NUM = {
    "jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
    "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"
}

for bhav_file in sorted(BHAV_ROOT.glob("**/sec_bhavdata_full_*.csv")):
    try:
        df = pd.read_csv(bhav_file, low_memory=False)
        df.columns = df.columns.str.strip()
        sym_col   = next((c for c in df.columns if c.strip().upper() == "SYMBOL"), None)
        ser_col   = next((c for c in df.columns if c.strip().upper() == "SERIES"), None)
        date_col  = next((c for c in df.columns if c.strip().upper() in ("DATE1","DATE","TIMESTAMP")), None)
        close_col = next((c for c in df.columns if c.strip().upper() in ("CLOSE_PRICE","CLOSE")), None)
        if not all([sym_col, date_col, close_col]):
            continue
        if ser_col:
            df = df[df[ser_col].fillna("").str.strip() == "EQ"]
        if df.empty:
            continue

        raw_date = str(df[date_col].dropna().iloc[0]).strip()
        for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_str = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        else:
            # Parse from filename: sec_bhavdata_full_22052026.csv
            m = re.search(r'(\d{2})(\d{2})(\d{4})', bhav_file.name)
            if m:
                date_str = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            else:
                continue

        prices = {}
        for _, row in df.iterrows():
            sym = str(row[sym_col]).strip().upper()
            try:
                cl = float(str(row[close_col]).replace(",", ""))
                if cl > 0:
                    prices[sym] = cl
            except (ValueError, TypeError):
                pass
        bhavcopy_cache[date_str] = prices
    except Exception as e:
        print(f"  Bhav error {bhav_file}: {e}")

print(f"Bhavcopy dates loaded: {len(bhavcopy_cache)}")

# ── Load existing signals ─────────────────────────────────────────────────────
if SIGNALS_FILE.exists():
    with open(SIGNALS_FILE) as f:
        existing = json.load(f)
else:
    existing = []

existing_keys = {(s["symbol"], s["signal_date"]) for s in existing}
print(f"Existing signals: {len(existing)}")

# ── Symbol extraction helpers ─────────────────────────────────────────────────
def extract_mc_codes(df, stem):
    """Extract MC codes from a dataframe based on file type."""
    codes = set()
    cols_upper = {c.upper(): c for c in df.columns}

    if stem in ("bullish", "bearish"):
        # scId column is MC code
        for col in ("scId", "SCID", "scid"):
            if col in df.columns or col.upper() in cols_upper:
                real_col = cols_upper.get(col.upper(), col)
                for v in df[real_col].dropna():
                    codes.add(str(v).strip().upper())
        return codes

    if stem in ("analysts_choice", "stock_ideas"):
        for col in ("scid", "scId", "SCID"):
            if col in df.columns or col.upper() in cols_upper:
                real_col = cols_upper.get(col.upper(), col)
                for v in df[real_col].dropna():
                    codes.add(str(v).strip().upper())
        return codes

    if stem == "52wk":
        for col in ("MC_Code", "MC_CODE"):
            if col in df.columns or col.upper() in cols_upper:
                real_col = cols_upper.get(col.upper(), col)
                for v in df[real_col].dropna():
                    codes.add(str(v).strip().upper())
        return codes

    # Scanner files: parse scannerDetails JSON
    for col in ("scannerDetails", "SCANNERDETAILS"):
        if col in df.columns or col.upper() in cols_upper:
            real_col = cols_upper.get(col.upper(), col)
            for raw in df[real_col].dropna():
                try:
                    raw_s = str(raw).strip()
                    # Replace single quotes with double for JSON, or use ast
                    try:
                        d = ast.literal_eval(raw_s)
                        stk_id = d.get("stkId") or d.get("stk_id", "")
                    except Exception:
                        m = re.search(r"'stkId'\s*:\s*'([^']+)'", raw_s)
                        if not m:
                            m = re.search(r'"stkId"\s*:\s*"([^"]+)"', raw_s)
                        stk_id = m.group(1) if m else ""
                    if stk_id:
                        codes.add(str(stk_id).strip().upper())
                except Exception:
                    pass
            return codes

    # Fallback: try scId
    for col in ("scId", "SCID", "scid"):
        if col in df.columns or col.upper() in cols_upper:
            real_col = cols_upper.get(col.upper(), col)
            for v in df[real_col].dropna():
                codes.add(str(v).strip().upper())

    return codes

# ── Process indicator_data folders ────────────────────────────────────────────
all_date_folders = sorted([
    d for d in INDICATOR_ROOT.iterdir()
    if d.is_dir() and not d.name.startswith(".")
])
print(f"Found {len(all_date_folders)} date folders")

new_signals = []

for date_folder in all_date_folders:
    folder_name = date_folder.name  # e.g. "22-May-2026"

    # Parse folder date
    try:
        folder_date = datetime.strptime(folder_name, "%d-%b-%Y")
        date_str = folder_date.strftime("%Y-%m-%d")
    except ValueError:
        print(f"  SKIP folder {folder_name} — can't parse date")
        continue

    # Get prices for this date from bhavcopy
    prices_today = bhavcopy_cache.get(date_str, {})

    # Per MC code: count bullish/bearish/neutral
    symbol_counts = {}  # {mc_code: {bullish:0, bearish:0, neutral:0}}

    csv_files = list(date_folder.glob("*.csv"))
    if not csv_files:
        print(f"  SKIP {folder_name} — no CSVs")
        continue

    for csv_file in csv_files:
        stem = csv_file.stem.lower()
        category = classify_file(csv_file)
        try:
            df = pd.read_csv(csv_file, low_memory=False)
            if df.empty:
                continue
            mc_codes = extract_mc_codes(df, stem)
            for mc in mc_codes:
                if not mc or mc.lower() in ("nan", "none", ""):
                    continue
                if mc not in symbol_counts:
                    symbol_counts[mc] = {"bullish": 0, "bearish": 0, "neutral": 0}
                symbol_counts[mc][category] += 1
        except Exception as e:
            pass  # skip bad files silently

    # Filter and generate signals
    date_new = 0
    for mc_code, counts in symbol_counts.items():
        bull = counts["bullish"]
        bear = counts["bearish"]
        neut = counts["neutral"]
        total = bull + bear + neut
        if total == 0:
            continue
        bull_pct = round(bull / total * 100, 1)

        if total > 25 and bull_pct > 90:
            # Resolve NSE symbol
            nse_sym = mc_to_nse.get(mc_code.upper(), "")
            if not nse_sym:
                continue

            key = (nse_sym, date_str)
            if key in existing_keys:
                continue

            # Get close price
            close = prices_today.get(nse_sym, 0)
            if close <= 0:
                continue  # skip if no price data

            new_signals.append({
                "symbol":        nse_sym,
                "signal_date":   date_str,
                "close":         round(close, 2),
                "bullish_count": bull,
                "bearish_count": bear,
                "neutral_count": neut,
                "total_count":   total,
                "bullish_pct":   bull_pct
            })
            existing_keys.add(key)
            date_new += 1

    print(f"  {folder_name}: {len(symbol_counts)} symbols checked, {date_new} new signals")

# ── Save combined signals ─────────────────────────────────────────────────────
all_signals = existing + new_signals
all_signals.sort(key=lambda s: s["signal_date"])

with open(SIGNALS_FILE, "w") as f:
    json.dump(all_signals, f, indent=2)

print(f"\n✅ Total signals: {len(all_signals)} ({len(new_signals)} new)")
