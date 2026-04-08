"""
NSE Indicator Engine
====================
Matches all indicator CSV files (from MoneyControl) with the NSE Bhavcopy
for the same trading day. Produces a consolidated output CSV and sends
it to Telegram.

Folder layout expected in GitHub repo:
  indicator_data/
    DD-Mon-YYYY/          ← one folder per trading date
      bullish_breakout.csv
      candlestick_bearish.csv
      moving_average_bearish.csv
      range_breakout_bearish.csv
      ... (up to 27 files)
  bhav_data/
    Mon-YYYY/             ← one folder per month  e.g. Apr-2026
      sec_bhavdata_full_DDMMYYYY.csv

Output: FINAL_OUTPUT_DD-Mon-YYYY.csv  → sent to Telegram
"""

import os
import ast
import glob
import re
import pandas as pd
import requests
from datetime import datetime
import pytz

# ─── CONFIG ─────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["TG_BOT_TOKEN"]
CHAT_ID    = os.environ["TG_CHAT_ID"]

INDICATOR_ROOT = "indicator_data"
BHAV_ROOT      = "bhav_data"
TOLERANCE      = 0.5       # max price diff for "Near" match

IST = pytz.timezone("Asia/Kolkata")

# ─── TELEGRAM HELPERS ────────────────────────────────────────────────────────
def tg_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=30,
        )
    except Exception as e:
        print(f"⚠️  tg_message error: {e}")


def tg_file(filepath, caption):
    try:
        with open(filepath, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
                files={"document": f},
                timeout=120,
            )
        if not r.json().get("ok"):
            print(f"⚠️  Telegram send error: {r.json()}")
        else:
            print("✅ Sent to Telegram")
    except Exception as e:
        print(f"❌ tg_file error: {e}")


# ─── PARSE ONE INDICATOR ROW ─────────────────────────────────────────────────
def parse_scanner_row(row):
    """Return (stkId, stkname, ltp, scanner_info) or None if unparseable."""
    txt = str(row.get("scannerDetails", ""))
    try:
        d = ast.literal_eval(txt)
    except Exception:
        return None

    ltp_raw = None
    for col in d.get("columns", []):
        if col.get("name") == "LTP":
            ltp_raw = col.get("value")
            break

    if ltp_raw is None:
        return None

    try:
        ltp = float(ltp_raw.replace(",", "").replace(" ", ""))
    except ValueError:
        return None

    return {
        "stkId":   d.get("stkId", "").strip(),
        "stkname": d.get("stkname", "").strip(),
        "ltp":     ltp,
    }


# ─── LOAD BHAV ───────────────────────────────────────────────────────────────
def load_bhav(trade_date_str):
    """
    trade_date_str: e.g. '07-Apr-2026'
    Looks in bhav_data/Apr-2026/sec_bhavdata_full_07042026.csv
    """
    try:
        dt = datetime.strptime(trade_date_str, "%d-%b-%Y")
    except ValueError:
        print(f"  ⚠️  Could not parse trade date: {trade_date_str}")
        return None

    month_folder = dt.strftime("%b-%Y")           # Apr-2026
    ddmmyyyy     = dt.strftime("%d%m%Y")          # 07042026
    fname        = f"sec_bhavdata_full_{ddmmyyyy}.csv"
    path         = os.path.join(BHAV_ROOT, month_folder, fname)

    if not os.path.isfile(path):
        print(f"  ⚠️  Bhav file not found: {path}")
        return None

    bhav = pd.read_csv(path)
    bhav.columns = bhav.columns.str.strip()
    bhav["SYMBOL"]      = bhav["SYMBOL"].str.strip()
    bhav["SERIES"]      = bhav["SERIES"].str.strip()
    bhav["CLOSE_PRICE"] = pd.to_numeric(bhav["CLOSE_PRICE"], errors="coerce")
    return bhav[bhav["SERIES"] == "EQ"].copy()


# ─── MATCH ONE STOCK ─────────────────────────────────────────────────────────
def match_to_bhav(ltp, bhav_eq):
    """Return (best_row, match_type) or (None, None)."""
    exact = bhav_eq[bhav_eq["CLOSE_PRICE"].round(2) == round(ltp, 2)]
    if not exact.empty:
        return exact.iloc[0], "Exact"

    bhav_eq = bhav_eq.copy()
    bhav_eq["_diff"] = (bhav_eq["CLOSE_PRICE"] - ltp).abs()
    near = bhav_eq[bhav_eq["_diff"] <= TOLERANCE]
    if near.empty:
        return None, None

    best = near.loc[near["_diff"].idxmin()]
    return best, "Near"


# ─── PROCESS ONE DATE FOLDER ─────────────────────────────────────────────────
def process_date(date_folder):
    trade_date = os.path.basename(date_folder)   # e.g. 07-Apr-2026
    print(f"\n📅 Processing: {trade_date}")

    bhav_eq = load_bhav(trade_date)
    if bhav_eq is None:
        return []

    print(f"  📊 Bhav loaded: {len(bhav_eq):,} EQ rows")

    results = []
    ind_files = sorted(glob.glob(os.path.join(date_folder, "*.csv")))
    print(f"  📂 Indicator files found: {len(ind_files)}")

    for ind_file in ind_files:
        fname = os.path.basename(ind_file).lower()
        trend = (
            "Bullish" if "bull" in fname
            else "Bearish" if "bear" in fname
            else "Unknown"
        )

        try:
            df = pd.read_csv(ind_file)
        except Exception as e:
            print(f"  ⚠️  Could not read {fname}: {e}")
            continue

        matched = skipped = 0
        for _, row in df.iterrows():
            parsed = parse_scanner_row(row)
            if parsed is None:
                skipped += 1
                continue

            best, mtype = match_to_bhav(parsed["ltp"], bhav_eq)
            if best is None:
                skipped += 1
                continue

            results.append({
                "Date":           trade_date,
                "Trend":          trend,
                "ScannerName":    row.get("scannerName", ""),
                "ScannerCode":    row.get("scannerCode", ""),
                "Category":       row.get("catName", ""),
                "MC_StkId":       parsed["stkId"],
                "MC_StkName":     parsed["stkname"],
                "LTP_MC":         parsed["ltp"],
                # Bhav data
                "SYMBOL":         best["SYMBOL"],
                "SERIES":         best["SERIES"],
                "OPEN":           best["OPEN_PRICE"],
                "HIGH":           best["HIGH_PRICE"],
                "LOW":            best["LOW_PRICE"],
                "CLOSE":          best["CLOSE_PRICE"],
                "AVG_PRICE":      best["AVG_PRICE"],
                "VOLUME":         best["TTL_TRD_QNTY"],
                "TURNOVER_LACS":  best["TURNOVER_LACS"],
                "NO_OF_TRADES":   best["NO_OF_TRADES"],
                "DELIV_QTY":      best["DELIV_QTY"],
                "DELIV_PER":      best["DELIV_PER"],
                "IndicatorFile":  os.path.basename(ind_file),
                "MatchType":      mtype,
            })
            matched += 1

        print(f"    {os.path.basename(ind_file)}: {matched} matched | {skipped} skipped")

    return results


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST)
    print(f"🕐 NSE Engine started: {now_ist.strftime('%d-%b-%Y %H:%M IST')}")

    # Find all date folders, sorted newest first
    date_folders = sorted(
        glob.glob(os.path.join(INDICATOR_ROOT, "*")),
        reverse=True,
    )

    if not date_folders:
        msg = f"❌ NSE Engine — No indicator folders found in `{INDICATOR_ROOT}/`"
        print(msg)
        tg_message(msg)
        return

    all_results = []
    for folder in date_folders:
        all_results.extend(process_date(folder))

    if not all_results:
        msg = (
            f"⚠️ NSE Engine — {now_ist.strftime('%d-%b-%Y')}\n"
            f"No matches found. Check indicator files and bhav data."
        )
        print(msg)
        tg_message(msg)
        return

    df = pd.DataFrame(all_results)

    # ── Summary stats
    total      = len(df)
    bullish_n  = len(df[df["Trend"] == "Bullish"])
    bearish_n  = len(df[df["Trend"] == "Bearish"])
    dates_done = df["Date"].nunique()
    symbols    = df["SYMBOL"].nunique()

    print(f"\n✅ Total matched rows: {total:,}")
    print(f"   Bullish: {bullish_n:,} | Bearish: {bearish_n:,}")
    print(f"   Unique symbols: {symbols:,} | Dates: {dates_done}")

    # ── Save output
    latest_date = df["Date"].max()
    output_file = f"/tmp/NSE_Indicator_Report_{latest_date.replace(' ','_')}.csv"
    df.to_csv(output_file, index=False)
    print(f"💾 Saved: {output_file}")

    # ── Per-scanner summary for caption
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

    caption = (
        f"📊 *NSE Indicator Report*\n"
        f"📅 Date: *{latest_date}*\n"
        f"📈 Total Matches: *{total:,}*\n"
        f"🟢 Bullish: {bullish_n:,} | 🔴 Bearish: {bearish_n:,}\n"
        f"🏷️ Unique Symbols: {symbols:,}\n"
        f"🕐 Generated: {now_ist.strftime('%H:%M IST')}"
        + "\n".join(lines)
    )[:1024]

    tg_file(output_file, caption)
    print("🏁 NSE Engine complete.")


if __name__ == "__main__":
    main()
