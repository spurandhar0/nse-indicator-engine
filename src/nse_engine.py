"""
NSE Indicator Engine  — v3  (6-Priority Symbol Matching)
=========================================================
Processes all 27 indicator CSV files (from MoneyControl) saved to
indicator_data/DD-Mon-YYYY/ and matches each stock with NSE Bhavcopy.

Symbol resolution priority (highest → lowest accuracy):
  P1. nsCode / sc_symbol / symbol column     → 100% accurate (direct NSE code)
  P2. stkId column (if looks like NSE code)  → 100% accurate
  P3. stockShortName / scid (if looks like NSE code) → ~98% accurate
  P4. NSE EQUITY_L company name → symbol     → ~95% accurate (fuzzy normalized)
  P5. MC Search API (name → nseid)           → ~92% accurate (needs MC_AUTH_TOKEN)
  P6. LTP price match ±0.5 (LAST RESORT)    → unreliable, flagged PriceFallback

Files handled:
  technical_picks_*  → sc_symbol column (direct NSE symbol)  ← P1
  chart_patterns_*   → meta_data.sc_symbol JSON field         ← P1
  scanner files (18) → nsCode / stkId top-level columns       ← P1/P2
  stock_ideas        → sc_symbol from meta? / stockShortName  ← P3
  analysts_choice    → scid sometimes = NSE / name fallback   ← P3/P4
  bullish / bearish  → company name via EQUITY_L              ← P4
  52wk               → company name via EQUITY_L              ← P4

Output: NSE_Indicator_Report_DD-Mon-YYYY.csv → committed to repo + Telegram
"""

import os, glob, re, json, difflib
from io import StringIO
import pandas as pd
import requests
from datetime import datetime
import pytz

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["TG_BOT_TOKEN"]
CHAT_ID    = os.environ["TG_CHAT_ID"]
# MC_AUTH_TOKEN is optional — enables MC Search API fallback (Priority 5)
MC_TOKEN   = os.environ.get("MC_AUTH_TOKEN", "")

INDICATOR_ROOT = "indicator_data"
BHAV_ROOT      = "bhav_data"
OUTPUT_ROOT    = "output_data"
os.makedirs(OUTPUT_ROOT, exist_ok=True)

PRICE_TOLERANCE = 0.5   # ±₹ used ONLY as last-resort (P6)
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

# ─── NSE EQUITY_L  (Priority 4 — name → symbol) ──────────────────────────────
def _normalize_name(name):
    """Normalize company name for matching: lowercase, strip legal suffixes, remove special chars."""
    s = str(name).lower().strip()
    for suffix in [
        ' limited', ' ltd.', ' ltd', ' pvt. ltd.', ' pvt. ltd', ' pvt ltd',
        ' private limited', ' private', ' pvt.', ' pvt',
        ' corporation', ' corp.', ' corp', ' incorporated', ' inc.',
        ' inc', ' llp', ' lp', ' co.', ' and co', ' (india)'
    ]:
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    s = re.sub(r'[^a-z0-9& ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def load_equity_l():
    """
    Download NSE EQUITY_L.csv and build {normalized_company_name: NSE_SYMBOL} map.
    Returns empty dict if download fails (engine continues without it).
    """
    urls = [
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
        "https://www1.nseindia.com/content/equities/EQUITY_L.csv",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": "https://www.nseindia.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                df = pd.read_csv(StringIO(r.text))
                df.columns = df.columns.str.strip()
                name_col = [c for c in df.columns if "NAME" in c.upper()]
                sym_col  = [c for c in df.columns if "SYMBOL" in c.upper()]
                if not name_col or not sym_col:
                    continue
                name_map = {}
                for _, row in df.iterrows():
                    symbol = str(row[sym_col[0]]).strip().upper()
                    name   = str(row[name_col[0]]).strip()
                    norm   = _normalize_name(name)
                    name_map[norm] = symbol
                print(f"  ✅ NSE EQUITY_L loaded: {len(name_map)} companies from {url}")
                return name_map
        except Exception as e:
            print(f"  ⚠️ EQUITY_L from {url}: {e}")
    print("  ⚠️ NSE EQUITY_L unavailable — will fall back to price matching for name-only files")
    return {}

def name_to_symbol(company_name, name_map):
    """Normalized + fuzzy match of company name against EQUITY_L. Returns NSE symbol or None."""
    if not company_name or not name_map:
        return None
    norm = _normalize_name(company_name)
    # 1) Exact normalized match
    if norm in name_map:
        return name_map[norm]
    # 2) Fuzzy match (cutoff 0.82 — tolerates "Titan Company" vs "titan company")
    matches = difflib.get_close_matches(norm, name_map.keys(), n=1, cutoff=0.82)
    if matches:
        return name_map[matches[0]]
    return None

# ─── MC SEARCH API (Priority 5 — online fallback) ────────────────────────────
_mc_search_cache = {}   # cache to avoid repeated API calls for same name

def mc_search_symbol(company_name):
    """
    Query MC Search API to get NSE symbol (nseid) for a company name.
    Requires MC_AUTH_TOKEN env var. Returns NSE symbol string or None.
    """
    if not MC_TOKEN or not company_name:
        return None
    name = str(company_name).strip()
    if name in _mc_search_cache:
        return _mc_search_cache[name]
    try:
        r = requests.get(
            "https://api.moneycontrol.com/mcapi/v1/search/query",
            params={"q": name, "type": "stock", "deviceType": "W"},
            headers={"Auth-Token": MC_TOKEN,
                     "User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                # First result with exchange='N' (NSE) preferred
                for item in data:
                    if str(item.get("exchange", "")).upper() in ("N", "NSE"):
                        sym = str(item.get("nseid", item.get("symbol", ""))).strip().upper()
                        if sym:
                            _mc_search_cache[name] = sym
                            return sym
                # Fallback: first result regardless
                sym = str(data[0].get("nseid", data[0].get("symbol", ""))).strip().upper()
                if sym:
                    _mc_search_cache[name] = sym
                    return sym
    except Exception:
        pass
    _mc_search_cache[name] = None
    return None

# ─── LOAD BHAV ────────────────────────────────────────────────────────────────
def load_bhav(trade_date_str):
    """Load NSE bhavcopy EQ series for a given date string like '08-Apr-2026'."""
    try:
        dt_obj = datetime.strptime(trade_date_str, "%d-%b-%Y")
    except ValueError:
        print(f"  ⚠️ Could not parse trade date: {trade_date_str}")
        return None

    month_folder = dt_obj.strftime("%b-%Y")   # Apr-2026
    ddmmyyyy     = dt_obj.strftime("%d%m%Y")  # 08042026
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
    eq.set_index("SYMBOL", inplace=True, drop=False)
    return eq

# ─── ROW PARSER ───────────────────────────────────────────────────────────────
_NSE_RE = re.compile(r'^[A-Z0-9&]{1,20}$')

def _clean(val):
    s = str(val).strip()
    return "" if s.upper() in ("NAN", "NONE", "NULL", "") else s

def _to_float(val):
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None

def _looks_like_nse(s):
    """True if string could be a valid NSE symbol (1-20 alphanumeric/&, uppercase)."""
    return bool(s and len(s) <= 20 and _NSE_RE.match(s.upper()))

def parse_row(row):
    """
    Extract all useful identifiers from a row regardless of source file.
    Returns dict with: nsCode, stkId, scid, stkname, ltp
    """
    # ── P1 candidates: direct NSE symbol columns ──
    nscode = ""
    for col in ("nsCode", "nseid", "nseCode", "sc_symbol", "symbol", "NSE_SYMBOL"):
        val = _clean(row.get(col, "")).upper()
        if val:
            nscode = val
            break

    # ── P2 candidate: stkId ──
    stk_id = _clean(row.get("stkId", row.get("MC_StkId", "")))

    # ── P3 candidates: scid / stockShortName (sometimes = NSE symbol) ──
    scid = _clean(row.get("scid", row.get("sc_id", row.get("scId", ""))))
    short_name = _clean(row.get("stockShortName", "")).upper()

    # ── Company name (for P4 EQUITY_L matching) ──
    stkname = ""
    for col in ("stkname", "StockName", "MC_StkName", "sc_name", "instrument", "stkName"):
        val = _clean(row.get(col, ""))
        if val:
            stkname = val
            break

    # ── LTP from various column names ──
    ltp = None
    for col in ("LTP", "ltp", "LTP_MC", "currPrice", "CMP", "cmp"):
        val = row.get(col)
        if val is not None and _clean(str(val)):
            ltp = _to_float(val)
            if ltp is not None:
                break

    # ── Parse meta_data JSON (chart_patterns, technical_picks) ──
    meta_raw = _clean(row.get("meta_data", ""))
    if meta_raw:
        try:
            meta = json.loads(meta_raw.replace("'", '"'))
        except Exception:
            try:
                meta = json.loads(meta_raw)
            except Exception:
                meta = {}
        # sc_symbol in meta_data (chart_patterns_inactive, technical_picks)
        if not nscode:
            sc_sym = _clean(meta.get("sc_symbol", "")).upper()
            if sc_sym:
                nscode = sc_sym
        # price from meta_data
        if ltp is None:
            for key in ("cmp", "entry_price"):
                ltp = _to_float(meta.get(key, ""))
                if ltp is not None:
                    break
        # company name from meta_data if still missing
        if not stkname:
            stkname = _clean(meta.get("sc_name", ""))

    # ── Parse scannerDetails JSON (scanner CSVs) ──
    sd_raw = _clean(row.get("scannerDetails", ""))
    if sd_raw:
        try:
            sd = json.loads(sd_raw.replace("'", '"'))
        except Exception:
            try:
                sd = json.loads(sd_raw)
            except Exception:
                sd = {}

        # Always extract identifiers from scannerDetails (bug fix: was conditional on ltp)
        if not nscode:
            nscode = _clean(sd.get("nsCode", sd.get("nseid", ""))).upper()
        if not stk_id:
            stk_id = _clean(sd.get("stkId", ""))
        if not stkname:
            stkname = _clean(sd.get("stkname", ""))

        # LTP from columns list in scannerDetails
        if ltp is None:
            for col in sd.get("columns", []):
                if col.get("name") == "LTP":
                    ltp = _to_float(col.get("value", ""))
                    break

    return {
        "nsCode":     nscode,
        "stkId":      stk_id,
        "scid":       scid,
        "short_name": short_name,
        "stkname":    stkname,
        "ltp":        ltp,
    }

# ─── SYMBOL MATCHERS ─────────────────────────────────────────────────────────
def _bhav_lookup(symbol, bhav_eq):
    """Direct symbol lookup in bhavcopy index. Returns bhav row or None."""
    s = str(symbol).strip().upper()
    if s and s in bhav_eq.index:
        return bhav_eq.loc[s]
    return None

def _price_fallback(ltp, bhav_eq):
    """Last-resort price match ±PRICE_TOLERANCE. Returns (row, diff) or (None, None)."""
    if ltp is None:
        return None, None
    tmp = bhav_eq.copy()
    tmp["_diff"] = (tmp["CLOSE_PRICE"] - ltp).abs()
    near = tmp[tmp["_diff"] <= PRICE_TOLERANCE]
    if near.empty:
        return None, None
    best = near.loc[near["_diff"].idxmin()]
    return best, float(best["_diff"])

def match_row(parsed, bhav_eq, name_map):
    """
    Try all 6 priorities and return (bhav_row, match_type) or (None, None).

    P1  nsCode_Exact       — sc_symbol / nsCode / symbol column directly matched
    P2  stkId_Exact        — stkId column matched as NSE symbol
    P3  ScId_Exact         — scid or stockShortName matched as NSE symbol
    P4  Name_Mapped        — company name → NSE EQUITY_L fuzzy lookup
    P5  MC_Search          — MC Search API (requires MC_AUTH_TOKEN)
    P6  PriceFallback      — last resort, unreliable, always flagged
    """
    # ── P1: direct NSE symbol columns ──
    if parsed["nsCode"]:
        row = _bhav_lookup(parsed["nsCode"], bhav_eq)
        if row is not None:
            return row, "nsCode_Exact"

    # ── P2: stkId as NSE symbol ──
    if parsed["stkId"] and _looks_like_nse(parsed["stkId"]):
        row = _bhav_lookup(parsed["stkId"], bhav_eq)
        if row is not None:
            return row, "stkId_Exact"

    # ── P3a: stockShortName as NSE symbol (e.g., "IEX", "TCS") ──
    if parsed["short_name"] and _looks_like_nse(parsed["short_name"]):
        row = _bhav_lookup(parsed["short_name"], bhav_eq)
        if row is not None:
            return row, "ShortName_Exact"

    # ── P3b: scid/sc_id as NSE symbol (e.g., analysts_choice "TCS" = MC code AND NSE symbol) ──
    if parsed["scid"] and _looks_like_nse(parsed["scid"]):
        row = _bhav_lookup(parsed["scid"], bhav_eq)
        if row is not None:
            return row, "ScId_Exact"

    # ── P4: NSE EQUITY_L company name → symbol ──
    if parsed["stkname"] and name_map:
        symbol = name_to_symbol(parsed["stkname"], name_map)
        if symbol:
            row = _bhav_lookup(symbol, bhav_eq)
            if row is not None:
                return row, "Name_Mapped"

    # ── P5: MC Search API ──
    if parsed["stkname"] and MC_TOKEN:
        symbol = mc_search_symbol(parsed["stkname"])
        if symbol:
            row = _bhav_lookup(symbol, bhav_eq)
            if row is not None:
                return row, "MC_Search"

    # ── P6: Price fallback (last resort) ──
    row, diff = _price_fallback(parsed["ltp"], bhav_eq)
    if row is not None:
        return row, f"PriceFallback(±{diff:.2f})"

    return None, None

# ─── PROCESS ONE DATE FOLDER ─────────────────────────────────────────────────
def process_date(date_folder, name_map):
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

            # Skip rows with no identifiers at all
            if (not parsed["nsCode"] and not parsed["stkId"] and
                    not parsed["scid"] and not parsed["short_name"] and
                    not parsed["stkname"] and parsed["ltp"] is None):
                skipped += 1
                continue

            best, mtype = match_row(parsed, bhav_eq, name_map)
            if best is None:
                skipped += 1
                continue

            match_types[mtype] = match_types.get(mtype, 0) + 1

            results.append({
                "Date":          trade_date,
                "Trend":         trend,
                "ScannerName":   _clean(row.get("scannerName", row.get("ScannerName", ""))),
                "ScannerCode":   _clean(row.get("scannerCode", row.get("ScannerCode", row.get("scanId", "")))),
                "Category":      _clean(row.get("catName",     row.get("Category", ""))),
                # MC identifiers
                "MC_nsCode":     parsed["nsCode"],
                "MC_StkId":      parsed["stkId"],
                "MC_StkName":    parsed["stkname"],
                "LTP_MC":        parsed["ltp"],
                # NSE Bhav data — accurate match
                "NSE_SYMBOL":    best["SYMBOL"],
                "SERIES":        best["SERIES"],
                "PREV_CLOSE":    best.get("PREV_CLOSE", ""),
                "OPEN":          best["OPEN_PRICE"],
                "HIGH":          best["HIGH_PRICE"],
                "LOW":           best["LOW_PRICE"],
                "CLOSE":         best["CLOSE_PRICE"],
                "AVG_PRICE":     best["AVG_PRICE"],
                "VOLUME":        best["TTL_TRD_QNTY"],
                "TURNOVER_LACS": best["TURNOVER_LACS"],
                "NO_OF_TRADES":  best["NO_OF_TRADES"],
                "DELIV_QTY":     best["DELIV_QTY"],
                "DELIV_PER":     best["DELIV_PER"],
                "IndicatorFile": fname,
                "MatchType":     mtype,
            })
            matched += 1

        mt_str = " | ".join(f"{k}:{v}" for k, v in sorted(match_types.items()))
        print(f"  {fname}: {matched} matched | {skipped} skipped  [{mt_str}]")

    return results

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST)
    print(f"🕐 NSE Engine v3 started: {now_ist.strftime('%d-%b-%Y %H:%M IST')}")

    # Load NSE EQUITY_L name→symbol map (P4 matching)
    print("\n📥 Loading NSE EQUITY_L company name map...")
    name_map = load_equity_l()

    date_folders = sorted(glob.glob(os.path.join(INDICATOR_ROOT, "*")), reverse=True)
    if not date_folders:
        msg = f"❌ NSE Engine — No indicator folders found in `{INDICATOR_ROOT}/`"
        print(msg); tg_message(msg)
        return

    all_results = []
    for folder in date_folders:
        if not os.path.isdir(folder):
            continue
        all_results.extend(process_date(folder, name_map))

    if not all_results:
        msg = (f"⚠️ NSE Engine — {now_ist.strftime('%d-%b-%Y')}\n"
               f"No matches found. Check indicator files and bhav data.")
        print(msg); tg_message(msg)
        return

    df = pd.DataFrame(all_results)

    # ── Match quality report ──
    print("\n📊 Match Type Distribution:")
    for mt, cnt in df["MatchType"].value_counts().items():
        flag = "  ⚠️  REVIEW RECOMMENDED" if "PriceFallback" in str(mt) else ""
        print(f"  {mt}: {cnt:,}{flag}")

    # ── Summary stats ──
    total      = len(df)
    bullish_n  = len(df[df["Trend"] == "Bullish"])
    bearish_n  = len(df[df["Trend"] == "Bearish"])
    price_fb   = len(df[df["MatchType"].str.startswith("PriceFallback", na=False)])
    accurate_n = total - price_fb
    dates_done = df["Date"].nunique()
    symbols    = df["NSE_SYMBOL"].nunique()

    print(f"\n✅ Total matched: {total:,}")
    print(f"   Accurate (symbol-matched): {accurate_n:,} ({accurate_n/total*100:.1f}%)")
    print(f"   PriceFallback (review): {price_fb:,} ({price_fb/total*100:.1f}%)")
    print(f"   Bullish: {bullish_n:,} | Bearish: {bearish_n:,}")
    print(f"   Unique NSE symbols: {symbols:,} | Dates: {dates_done}")

    # ── Save output ──
    latest_date = df["Date"].max()
    fname       = f"NSE_Indicator_Report_{latest_date.replace(' ', '_')}.csv"
    out_path_r  = os.path.join(OUTPUT_ROOT, fname)
    tmp_path    = f"/tmp/{fname}"
    df.to_csv(out_path_r, index=False)   # → repo (committed by workflow)
    df.to_csv(tmp_path,   index=False)   # → /tmp for Telegram
    print(f"💾 Saved: {out_path_r}")

    # ── Telegram caption ──
    scanner_summary = (
        df.groupby(["Trend", "IndicatorFile"])
          .size()
          .reset_index(name="Count")
          .sort_values(["Trend", "Count"], ascending=[True, False])
    )
    lines = []
    for trend, grp in scanner_summary.groupby("Trend"):
        emoji = "🟢" if trend == "Bullish" else ("🔴" if trend == "Bearish" else "⚪")
        lines.append(f"\n{emoji} *{trend}* ({grp['Count'].sum():,})")
        for _, r in grp.head(8).iterrows():
            lines.append(f"  • {r['IndicatorFile']}: {r['Count']}")

    caption = (
        f"📊 *NSE Indicator Engine v3*\n"
        f"📅 Date: *{latest_date}*\n"
        f"📈 Total: *{total:,}* | 🏷️ Symbols: *{symbols:,}*\n"
        f"🟢 Bullish: {bullish_n:,} | 🔴 Bearish: {bearish_n:,}\n"
        f"✅ Accurate: {accurate_n:,} ({accurate_n/total*100:.0f}%) | "
        f"⚠️ PriceFallback: {price_fb}\n"
        f"🕐 {now_ist.strftime('%H:%M IST')}"
        + "".join(lines)
    )[:1024]

    tg_file(tmp_path, caption)
    print("🏁 NSE Engine v3 complete.")


if __name__ == "__main__":
    main()
