"""
NSE Indicator Engine  — v2  (Symbol-First Matching)
=====================================================
Matches all 27 indicator CSV files (from MoneyControl) with the NSE Bhavcopy
for the same trading day.  Priority matching order:

  1. nsCode column (direct NSE symbol from MC API)  ← PRIMARY — 100% accurate
  2. stkId column  (if it looks like an NSE symbol)  ← SECONDARY
  3. stkname column fuzzy → bhav SYMBOL               ← TERTIARY (fallback)
  4. LTP price match within ±0.5                       ← LAST RESORT (flagged)

Any row that reaches method 4 is flagged MatchType=PriceFallback so you
know it needs manual verification.  This replaces the old "Near" logic
which was matching wrong companies.

Folder layout in GitHub repo:
  indicator_data/DD-Mon-YYYY/   ← 27 CSVs saved by mc_data_fetch.py
  bhav_data/Mon-YYYY/           ← sec_bhavdata_full_DDMMYYYY.csv
  output_data/                  ← consolidated output CSVs committed here

Output: NSE_Indicator_Report_DD-Mon-YYYY.csv → committed to repo + sent to Telegram
"""

import os, glob, re, json
import pandas as pd
import requests
from datetime import datetime
import pytz

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID   = os.environ["TG_CHAT_ID"]

INDICATOR_ROOT = "indicator_data"
BHAV_ROOT      = "bhav_data"
OUTPUT_ROOT    = "output_data"
os.makedirs(OUTPUT_ROOT, exist_ok=True)

PRICE_TOLERANCE = 0.5   # used ONLY as last-resort fallback
IST = pytz.timezone("Asia/Kolkata")

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def tg_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
                      timeout=30)
    except Exception as e:
        print(f"⚠️ tg_message error: {e}")

def tg_file(filepath, caption):
    try:
        with open(filepath, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                              data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
                              files={"document": f}, timeout=120)
        if not r.json().get("ok"):
            print(f"⚠️ Telegram send error: {r.json()}")
        else:
            print("✅ Sent to Telegram")
    except Exception as e:
        print(f"❌ tg_file error: {e}")

# ─── LOAD BHAV ────────────────────────────────────────────────────────────────
def load_bhav(trade_date_str):
    """Load NSE bhavcopy EQ series for a given date string like '08-Apr-2026'."""
    try:
        dt = datetime.strptime(trade_date_str, "%d-%b-%Y")
    except ValueError:
        print(f"  ⚠️ Could not parse trade date: {trade_date_str}")
        return None

    month_folder = dt.strftime("%b-%Y")          # Apr-2026
    ddmmyyyy     = dt.strftime("%d%m%Y")          # 08042026
    fname        = f"sec_bhavdata_full_{ddmmyyyy}.csv"
    path         = os.path.join(BHAV_ROOT, month_folder, fname)

    if not os.path.isfile(path):
        print(f"  ⚠️ Bhav file not found: {path}")
        return None

    bhav = pd.read_csv(path)
    bhav.columns        = bhav.columns.str.strip()
    bhav["SYMBOL"]      = bhav["SYMBOL"].str.strip().str.upper()
    bhav["SERIES"]      = bhav["SERIES"].str.strip()
    bhav["CLOSE_PRICE"] = pd.to_numeric(bhav["CLOSE_PRICE"], errors="coerce")

    eq = bhav[bhav["SERIES"] == "EQ"].copy()
    eq.set_index("SYMBOL", inplace=True, drop=False)   # keep SYMBOL column too
    return eq

# ─── EXTRACT nsCode + LTP FROM A ROW ─────────────────────────────────────────
def parse_row(row):
    """
    Return dict with keys: nsCode, stkId, stkname, ltp
    Tries every column that might carry NSE symbol or price.
    """
    # ── nsCode: try direct column first ──
    nscode = str(row.get("nsCode", row.get("nseid", row.get("nseCode", "")))).strip().upper()
    nscode = nscode if nscode not in ("", "NAN", "NONE") else ""

    # ── stkId ──
    stk_id = str(row.get("stkId", row.get("MC_StkId", ""))).strip()
    stk_id = stk_id if stk_id not in ("", "NAN", "NONE") else ""

    # ── stkname ──
    stkname = str(row.get("stkname", row.get("StockName", row.get("MC_StkName", "")))).strip()
    stkname = stkname if stkname not in ("", "NAN", "NONE") else ""

    # ── LTP: try direct column, then scannerDetails JSON ──
    ltp = None
    for col in ("LTP", "ltp", "LTP_MC", "currPrice", "CMP"):
        val = row.get(col)
        if val is not None and str(val) not in ("", "nan", "None"):
            try:
                ltp = float(str(val).replace(",", "").strip())
                break
            except ValueError:
                pass

    if ltp is None:
        # try to parse from scannerDetails JSON string
        sd_raw = row.get("scannerDetails", "")
        if sd_raw and str(sd_raw) not in ("", "nan"):
            try:
                sd = json.loads(str(sd_raw).replace("'", "\""))
            except Exception:
                sd = {}
            for col in sd.get("columns", []):
                if col.get("name") == "LTP":
                    try:
                        ltp = float(str(col.get("value", "")).replace(",", ""))
                    except ValueError:
                        pass
                    break
            if ltp is None:
                nscode = nscode or str(sd.get("nsCode", sd.get("nseid", ""))).strip().upper()
                stk_id = stk_id or str(sd.get("stkId", "")).strip()
                stkname = stkname or str(sd.get("stkname", "")).strip()

    return {"nsCode": nscode, "stkId": stk_id, "stkname": stkname, "ltp": ltp}

# ─── SYMBOL LOOKUP HELPERS ────────────────────────────────────────────────────
# Pre-compiled regex: NSE symbols are 1-20 uppercase letters/digits
_NSE_RE = re.compile(r'^[A-Z0-9&]{1,20}$')

def looks_like_nse(s):
    return bool(s and _NSE_RE.match(s.upper()))

def match_symbol_in_bhav(symbol, bhav_eq):
    """Direct symbol lookup. Returns bhav row or None."""
    s = symbol.strip().upper()
    if s in bhav_eq.index:
        return bhav_eq.loc[s]
    return None

def price_fallback(ltp, bhav_eq):
    """Last-resort: match by price ± PRICE_TOLERANCE. Returns (row, diff) or (None, None)."""
    if ltp is None:
        return None, None
    bhav_eq = bhav_eq.copy()
    bhav_eq["_diff"] = (bhav_eq["CLOSE_PRICE"] - ltp).abs()
    near = bhav_eq[bhav_eq["_diff"] <= PRICE_TOLERANCE]
    if near.empty:
        return None, None
    best = near.loc[near["_diff"].idxmin()]
    return best, float(best["_diff"])

# ─── MATCH ONE PARSED ROW ────────────────────────────────────────────────────
def match_row(parsed, bhav_eq):
    """
    Returns (bhav_row, match_type_str) or (None, None).
    Match type priority:
      nsCode_Exact  → nsCode column matched directly in bhav
      stkId_Exact   → stkId column matched as NSE symbol
      PriceFallback → last resort (unreliable, flag for review)
    """
    # ── Priority 1: nsCode direct match ──
    if parsed["nsCode"]:
        row = match_symbol_in_bhav(parsed["nsCode"], bhav_eq)
        if row is not None:
            return row, "nsCode_Exact"

    # ── Priority 2: stkId as NSE symbol ──
    if parsed["stkId"] and looks_like_nse(parsed["stkId"]):
        row = match_symbol_in_bhav(parsed["stkId"], bhav_eq)
        if row is not None:
            return row, "stkId_Exact"

    # ── Priority 3: stkname stripped → NSE symbol (sometimes MC uses NSE name directly) ──
    if parsed["stkname"]:
        clean = re.sub(r'[^A-Z0-9]', '', parsed["stkname"].upper())
        if clean and looks_like_nse(clean):
            row = match_symbol_in_bhav(clean, bhav_eq)
            if row is not None:
                return row, "Name_Exact"

    # ── Priority 4: price fallback (LAST RESORT) ──
    row, diff = price_fallback(parsed["ltp"], bhav_eq)
    if row is not None:
        return row, f"PriceFallback(±{diff:.2f})"

    return None, None

# ─── PROCESS ONE DATE FOLDER ─────────────────────────────────────────────────
def process_date(date_folder):
    trade_date = os.path.basename(date_folder)
    print(f"\n📅 Processing: {trade_date}")

    bhav_eq = load_bhav(trade_date)
    if bhav_eq is None:
        return []

    print(f"  📊 Bhav loaded: {len(bhav_eq):,} EQ rows | {bhav_eq.index.nunique():,} unique symbols")

    results   = []
    ind_files = sorted(glob.glob(os.path.join(date_folder, "*.csv")))
    print(f"  📂 Indicator files found: {len(ind_files)}")

    for ind_file in ind_files:
        fname = os.path.basename(ind_file)
        trend = ("Bullish" if "bull" in fname.lower()
                 else "Bearish" if "bear" in fname.lower()
                 else "Unknown")

        try:
            df = pd.read_csv(ind_file, low_memory=False)
        except Exception as e:
            print(f"  ⚠️ Could not read {fname}: {e}")
            continue

        df.columns = df.columns.str.strip()

        matched = skipped = 0
        match_types = {}

        for _, row in df.iterrows():
            parsed = parse_row(row)

            # Skip rows where we have nothing to match with
            if not parsed["nsCode"] and not parsed["stkId"] and parsed["ltp"] is None:
                skipped += 1
                continue

            best, mtype = match_row(parsed, bhav_eq)
            if best is None:
                skipped += 1
                continue

            match_types[mtype] = match_types.get(mtype, 0) + 1

            results.append({
                "Date":         trade_date,
                "Trend":        trend,
                "ScannerName":  str(row.get("scannerName", row.get("ScannerName", ""))).strip(),
                "ScannerCode":  str(row.get("scannerCode", row.get("ScannerCode", row.get("scanId", "")))).strip(),
                "Category":     str(row.get("catName",     row.get("Category", ""))).strip(),
                # MC identifiers
                "MC_nsCode":    parsed["nsCode"],
                "MC_StkId":     parsed["stkId"],
                "MC_StkName":   parsed["stkname"],
                "LTP_MC":       parsed["ltp"],
                # NSE Bhav data  — these are now ACCURATE
                "NSE_SYMBOL":   best["SYMBOL"],
                "SERIES":       best["SERIES"],
                "PREV_CLOSE":   best.get("PREV_CLOSE", ""),
                "OPEN":         best["OPEN_PRICE"],
                "HIGH":         best["HIGH_PRICE"],
                "LOW":          best["LOW_PRICE"],
                "CLOSE":        best["CLOSE_PRICE"],
                "AVG_PRICE":    best["AVG_PRICE"],
                "VOLUME":       best["TTL_TRD_QNTY"],
                "TURNOVER_LACS":best["TURNOVER_LACS"],
                "NO_OF_TRADES": best["NO_OF_TRADES"],
                "DELIV_QTY":    best["DELIV_QTY"],
                "DELIV_PER":    best["DELIV_PER"],
                "IndicatorFile":fname,
                "MatchType":    mtype,
            })
            matched += 1

        mt_str = " | ".join(f"{k}:{v}" for k, v in sorted(match_types.items()))
        print(f"  {fname}: {matched} matched | {skipped} skipped  [{mt_str}]")

    return results

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST)
    print(f"🕐 NSE Engine v2 started: {now_ist.strftime('%d-%b-%Y %H:%M IST')}")

    date_folders = sorted(glob.glob(os.path.join(INDICATOR_ROOT, "*")), reverse=True)
    if not date_folders:
        msg = f"❌ NSE Engine — No indicator folders found in `{INDICATOR_ROOT}/`"
        print(msg); tg_message(msg)
        return

    all_results = []
    for folder in date_folders:
        all_results.extend(process_date(folder))

    if not all_results:
        msg = (f"⚠️ NSE Engine — {now_ist.strftime('%d-%b-%Y')}\n"
               f"No matches found. Check indicator files and bhav data.")
        print(msg); tg_message(msg)
        return

    df = pd.DataFrame(all_results)

    # ── Match quality report ──
    print("\n📊 Match Type Distribution:")
    for mt, cnt in df["MatchType"].value_counts().items():
        flag = "  ⚠️ REVIEW RECOMMENDED" if "PriceFallback" in str(mt) else ""
        print(f"  {mt}: {cnt:,}{flag}")

    # ── Summary stats ──
    total      = len(df)
    bullish_n  = len(df[df["Trend"] == "Bullish"])
    bearish_n  = len(df[df["Trend"] == "Bearish"])
    accurate_n = len(df[~df["MatchType"].str.startswith("PriceFallback", na=False)])
    dates_done = df["Date"].nunique()
    symbols    = df["NSE_SYMBOL"].nunique()

    print(f"\n✅ Total matched: {total:,}")
    print(f"   Accurate (symbol-matched): {accurate_n:,} ({accurate_n/total*100:.1f}%)")
    print(f"   Bullish: {bullish_n:,} | Bearish: {bearish_n:,}")
    print(f"   Unique NSE symbols: {symbols:,} | Dates: {dates_done}")

    # ── Save output ──
    latest_date = df["Date"].max()
    fname       = f"NSE_Indicator_Report_{latest_date.replace(' ','_')}.csv"
    out_path    = os.path.join(OUTPUT_ROOT, fname)
    tmp_path    = f"/tmp/{fname}"
    df.to_csv(out_path, index=False)     # → repo (auto-committed by workflow)
    df.to_csv(tmp_path, index=False)     # → /tmp for Telegram
    print(f"💾 Saved: {out_path}")

    # ── Telegram caption ──
    scanner_summary = (
        df.groupby(["Trend", "ScannerName"])
          .size()
          .reset_index(name="Count")
          .sort_values(["Trend", "Count"], ascending=[True, False])
    )
    lines = []
    for trend, grp in scanner_summary.groupby("Trend"):
        emoji = "🟢" if trend == "Bullish" else "🔴"
        lines.append(f"\n{emoji} *{trend}*")
        for _, r in grp.iterrows():
            lines.append(f"  • {r['ScannerName']}: {r['Count']}")

    price_fb = len(df[df["MatchType"].str.startswith("PriceFallback", na=False)])
    caption = (
        f"📊 *NSE Indicator Report v2*\n"
        f"📅 Date: *{latest_date}*\n"
        f"📈 Total Matches: *{total:,}*\n"
        f"🟢 Bullish: {bullish_n:,} | 🔴 Bearish: {bearish_n:,}\n"
        f"✅ Symbol-Matched: {accurate_n:,} | ⚠️ PriceFallback: {price_fb}\n"
        f"🏷️ Unique NSE Symbols: {symbols:,}\n"
        f"🕐 Generated: {now_ist.strftime('%H:%M IST')}"
        + "\n".join(lines)
    )[:1024]

    tg_file(tmp_path, caption)
    print("🏁 NSE Engine v2 complete.")


if __name__ == "__main__":
    main()
