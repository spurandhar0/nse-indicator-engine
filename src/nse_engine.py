"""
NSE Indicator Engine — v5 (Master Mapping + Rich Output)
=========================================================
Processes all 27+ indicator CSV files from MoneyControl and matches
each stock with NSE Bhavcopy using multi-priority symbol resolution.

SYMBOL RESOLUTION PRIORITY (highest → lowest accuracy):
  P0. MC Master Mapping (mc_nse_mapping.csv)         → 99%+ accurate
      – stkId  from scannerDetails                    (18 scanner files)
      – scUrl  last segment from scannerDetails       (alternate MC code)
      – scId   from bullish/bearish                   (bullish, bearish CSVs)
      – scid   from analysts_choice/stock_ideas       (analysts, ideas CSVs)
      – MC_Code from 52wk                             (52wk CSV)
  P1. Direct NSE symbol column (sc_symbol, nsCode)   → 100% accurate
  P2. stockShortName looks like NSE symbol            →  95% accurate
  P3. NSE EQUITY_L name → symbol (fuzzy)             →  80% accurate
  P4. MC Search API (name → nseid)                   →  85% accurate
  P5. LTP Price Fallback ±0.5 (LAST RESORT)          →  30-65% accurate

Every P3/P4 match is LTP-validated. Mismatches > 30% are flagged.

OUTPUT COLUMNS:
  Date, Trend, Signal, Comment, IndicatorSummary,
  ScannerName, ScannerCode, Category,
  MC_StkName, MC_Code_Used, LTP_MC,
  NSE_SYMBOL, SERIES, PREV_CLOSE, OPEN, HIGH, LOW, CLOSE,
  AVG_PRICE, VOLUME, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER,
  Ind1_Name, Ind1_Value, Ind2_Name, Ind2_Value, Ind3_Name, Ind3_Value,
  IndicatorFile, MatchType, ConfidenceScore, LTP_Close_Pct

MASTER MAPPING UPDATE:
  Unknown MC codes discovered during processing are looked up via
  MC Price Feed API and appended to src/mc_nse_mapping.csv for
  future reference (auto-growing master table).
"""

import os, glob, re, json, ast, difflib, time
from io import StringIO
import pandas as pd
import requests
from datetime import datetime
import pytz

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["TG_BOT_TOKEN"]
CHAT_ID    = os.environ["TG_CHAT_ID"]
MC_TOKEN   = os.environ.get("MC_AUTH_TOKEN", "")

INDICATOR_ROOT  = "indicator_data"
BHAV_ROOT       = "bhav_data"
OUTPUT_ROOT     = "output_data"
MAPPING_PATH    = "src/mc_nse_mapping.csv"
os.makedirs(OUTPUT_ROOT, exist_ok=True)

PRICE_TOLERANCE = 0.5   # ±₹ for P5 last-resort price fallback
IST = pytz.timezone("Asia/Kolkata")

# ─── ENCODING FIX ────────────────────────────────────────────────────────────
_MOJIBAKE = [
    ("\u00e2\u0080\u0099", "'"),
    ("\u00e2\u0080\u009c", '"'),
    ("\u00e2\u0080\u009d", '"'),
    ("\u00e2\u0080\u0093", "-"),
    ("\u00e2\u0080\u0094", "--"),
    ("\u00e2\u0080\u00a6", "..."),
    ("\u00e2\u0080\u00a2", "*"),
    ("\u00c2\u00a0", " "),
    ("\u00c2\u00ae", "(R)"),
    ("\u00c3\u00a9", "e"),
    ("\u00e2\u0082\u00b9", "Rs."),
]

def fix_encoding(text):
    """Fix common mojibake / encoding issues in MC API text."""
    if not text:
        return text
    s = str(text)
    # Try UTF-8 re-decode (handles most mojibake)
    try:
        s = s.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    # Manual replacements for any remaining artifacts
    for bad, good in _MOJIBAKE:
        s = s.replace(bad, good)
    return s.strip()

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

# ─── P0: MC MASTER MAPPING ────────────────────────────────────────────────────
def load_mc_mapping():
    """Load MC_Code → NSE_Symbol master mapping. Returns (mc_map, mapping_df)."""
    if not os.path.isfile(MAPPING_PATH):
        print(f"  ⚠️ {MAPPING_PATH} not found — P0 lookup disabled")
        return {}, pd.DataFrame()
    df = pd.read_csv(MAPPING_PATH, dtype=str).fillna("")
    mc_map = {}
    for _, r in df.iterrows():
        code = r.get("MC_Code", "").strip()
        sym  = r.get("NSE_Symbol", "").strip().upper()
        if code and sym and sym not in ("NAN", "NONE", ""):
            mc_map[code] = sym
    print(f"  ✅ MC Master Mapping: {len(mc_map):,} entries ({MAPPING_PATH})")
    return mc_map, df

def lookup_new_mc_code(mc_code):
    """
    Query MC price feed API for an unknown MC code.
    Returns dict with NSE_Symbol, BSE_Code, ISIN, Full_Name, Series, Sector, LTP
    or None if not found.
    """
    try:
        url = f"https://priceapi.moneycontrol.com/pricefeed/nse/equitycash/{mc_code}"
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                         "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
            "Referer": "https://m.moneycontrol.com/"
        }, timeout=8)
        if r.status_code == 200:
            d = r.json().get("data", {})
            if d and d.get("NSEID"):
                return {
                    "MC_Code": mc_code,
                    "NSE_Symbol": str(d.get("NSEID", "")).strip().upper(),
                    "BSE_Code": str(d.get("BSEID", "")),
                    "ISIN": str(d.get("isinid", "")),
                    "Full_Name": str(d.get("SC_FULLNM", "")),
                    "MC_StockName": str(d.get("SC_FULLNM", "")),
                    "Series": str(d.get("SERIES", "EQ")),
                    "Sector": str(d.get("SC_GROUP", "")),
                    "LTP": str(d.get("CMP", "")),
                    "MC_ShortName": "",
                    "MC_UrlCode": "",
                    "Source": "auto_discovered",
                    "API_Status": "FOUND",
                }
    except Exception:
        pass
    return None

def update_mc_mapping(mapping_df, new_entries):
    """Append new MC code entries to the mapping CSV and save."""
    if not new_entries:
        return mapping_df
    new_df = pd.DataFrame(new_entries)
    updated = pd.concat([mapping_df, new_df], ignore_index=True)
    updated.drop_duplicates(subset=["MC_Code"], keep="last", inplace=True)
    updated.to_csv(MAPPING_PATH, index=False)
    print(f"  📝 Master mapping updated: +{len(new_entries)} new entries (total: {len(updated)})")
    return updated

# ─── NSE EQUITY_L  (P3 fallback — name → symbol) ─────────────────────────────
def _normalize_name(name):
    s = str(name).lower().strip()
    for suffix in [
        ' limited', ' ltd.', ' ltd', ' pvt. ltd.', ' pvt. ltd', ' pvt ltd',
        ' private limited', ' private', ' pvt.', ' pvt',
        ' corporation', ' corp.', ' corp', ' incorporated', ' inc.',
        ' inc', ' llp', ' lp', ' co.', ' and co', ' (india)', ' india',
    ]:
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    s = re.sub(r'[^a-z0-9& ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def load_equity_l():
    """Download NSE EQUITY_L.csv → {normalized_name: NSE_SYMBOL} map."""
    urls = [
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
        "https://www1.nseindia.com/content/equities/EQUITY_L.csv",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": "https://www.nseindia.com/",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=15)
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
                    name_map[norm]           = symbol
                    name_map[symbol.lower()] = symbol  # symbol as key too
                print(f"  ✅ NSE EQUITY_L: {len(name_map):,} entries from {url}")
                return name_map
        except Exception as e:
            print(f"  ⚠️ EQUITY_L from {url}: {e}")
    print("  ⚠️ NSE EQUITY_L unavailable")
    return {}

def name_to_symbol(company_name, name_map):
    """Normalized + fuzzy match. Returns NSE symbol or None."""
    if not company_name or not name_map:
        return None
    norm = _normalize_name(company_name)
    if norm in name_map:
        return name_map[norm]
    matches = difflib.get_close_matches(norm, name_map.keys(), n=1, cutoff=0.82)
    if matches:
        return name_map[matches[0]]
    return None

# ─── P4: MC SEARCH API ────────────────────────────────────────────────────────
_mc_search_cache = {}

def mc_search_symbol(company_name):
    """MC Search API → NSE symbol. Requires MC_AUTH_TOKEN."""
    if not MC_TOKEN or not company_name:
        return None
    name = str(company_name).strip()
    if name in _mc_search_cache:
        return _mc_search_cache[name]
    try:
        r = requests.get(
            "https://api.moneycontrol.com/mcapi/v1/search/query",
            params={"q": name, "type": "stock", "deviceType": "W"},
            headers={"Auth-Token": MC_TOKEN, "User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                for item in data:
                    if str(item.get("exchange", "")).upper() in ("N", "NSE"):
                        sym = str(item.get("nseid", item.get("symbol", ""))).strip().upper()
                        if sym:
                            _mc_search_cache[name] = sym
                            return sym
                sym = str(data[0].get("nseid", data[0].get("symbol", ""))).strip().upper()
                if sym:
                    _mc_search_cache[name] = sym
                    return sym
    except Exception:
        pass
    _mc_search_cache[name] = None
    return None


# ─── AUTO-DOWNLOAD BHAVCOPY ──────────────────────────────────────────────────
def _download_bhav(dt_obj, save_path):
    """Download bhavcopy from NSE if local file not found. Returns True on success."""
    import time as _time
    ddmmyyyy = dt_obj.strftime("%d%m%Y")
    filename = f"sec_bhavdata_full_{ddmmyyyy}.csv"
    url = f"https://nsearchives.nseindia.com/products/content/{filename}"
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    dl_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.nseindia.com/",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=dl_headers, timeout=15)
        _time.sleep(1)
        resp = session.get(url, headers=dl_headers, timeout=60)
        if resp.status_code == 200 and len(resp.content) > 1000:
            with open(save_path, "wb") as f:
                f.write(resp.content)
            print(f"  ✅ Auto-downloaded: {filename} ({len(resp.content)//1024} KB) → {save_path}")
            return True
        else:
            print(f"  ❌ Auto-download failed: HTTP {resp.status_code} for {url}")
    except Exception as e:
        print(f"  ❌ Auto-download error: {e}")
    return False

# ─── BHAV LOADER ─────────────────────────────────────────────────────────────
def load_bhav(trade_date_str):
    try:
        dt_obj = datetime.strptime(trade_date_str, "%d-%b-%Y")
    except ValueError:
        print(f"  ⚠️ Could not parse trade date: {trade_date_str}")
        return None

    month_folder = dt_obj.strftime("%b-%Y")
    ddmmyyyy     = dt_obj.strftime("%d%m%Y")
    fname        = f"sec_bhavdata_full_{ddmmyyyy}.csv"
    path         = os.path.join(BHAV_ROOT, month_folder, fname)

    if not os.path.isfile(path):
        print(f"  ⚠️ Bhav file not found: {path} — attempting auto-download...")
        if not _download_bhav(dt_obj, path):
            tg_message(f"⚠️ NSE Engine — Bhav file not found and auto-download failed for {trade_date_str}")
            return None

    bhav = pd.read_csv(path)
    bhav.columns        = bhav.columns.str.strip()
    bhav["SYMBOL"]      = bhav["SYMBOL"].str.strip().str.upper()
    bhav["SERIES"]      = bhav["SERIES"].str.strip()
    bhav["CLOSE_PRICE"] = pd.to_numeric(bhav["CLOSE_PRICE"], errors="coerce")

    eq = bhav[bhav["SERIES"].isin(["EQ", "BE", "BZ", "SM", "ST"])].copy()
    eq.set_index("SYMBOL", inplace=True, drop=False)
    return eq

# ─── HELPERS ─────────────────────────────────────────────────────────────────
_NSE_RE = re.compile(r'^[A-Z0-9&\-]{1,20}$')

def _clean(val):
    s = str(val).strip()
    return "" if s.upper() in ("NAN", "NONE", "NULL", "", "NA") else s

def _to_float(val):
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None

def _looks_like_nse(s):
    return bool(s and 1 <= len(s) <= 20 and _NSE_RE.match(s.upper()))

def _parse_dict_str(raw):
    """Parse Python dict string (single quotes) OR JSON string."""
    if not raw:
        return {}
    try:
        return ast.literal_eval(raw)
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _extract_url_code(url_str):
    """Extract MC code from URL path like 'cement-major/ultratechcement/UTC01' → 'UTC01'."""
    s = _clean(url_str)
    if not s:
        return ""
    return s.rstrip("/").split("/")[-1].strip()

def _extract_nsecode_clean(raw):
    """Extract clean NSE symbol from nsCode/sc_symbol. Handles URL paths."""
    s = _clean(raw).upper()
    if not s:
        return ""
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    if _looks_like_nse(s):
        return s
    return ""

# ─── SCANNER DESCRIPTION MAP ──────────────────────────────────────────────────
# Maps scannerCode patterns → human-readable description
_SCANNER_DESCRIPTIONS = {
    "ADRBEAR": "Adaptive RSI indicator fell below its signal line — Bearish crossover",
    "ADRBULL": "Adaptive RSI indicator crossed above its signal line — Bullish crossover",
    "MACDBEARO": "MACD line crossed below Signal line — Bearish crossover",
    "MACDBULL": "MACD line crossed above Signal line — Bullish crossover",
    "RSIBEAR": "RSI fell below oversold zone boundary — Bearish signal",
    "RSIBULL": "RSI crossed above oversold zone — Bullish recovery signal",
    "MFIBEAR": "Money Flow Index fell below signal line — Bearish divergence",
    "MFIBULL": "Money Flow Index crossed above signal line — Bullish accumulation",
    "STOCHBEAR": "Stochastic %K crossed below %D line — Bearish crossover",
    "STOCHBULL": "Stochastic %K crossed above %D line — Bullish crossover",
    "ADXBEAR": "ADX falling — trend weakening, Bearish momentum",
    "ADXBULL": "ADX rising — trend strengthening, Bullish momentum",
    "SUPERBEARO": "Price fell below SuperTrend line — Bearish trend reversal",
    "SUPERBULL": "Price crossed above SuperTrend line — Bullish trend reversal",
    "MABEAR": "Price crossed below Moving Average — Bearish signal",
    "MABULL": "Price crossed above Moving Average — Bullish signal",
    "RBEAR": "Range Breakout to the downside — Bearish breakout",
    "RBULL": "Range Breakout to the upside — Bullish breakout",
    "CANBEAR": "Bearish candlestick pattern detected",
    "CANBULL": "Bullish candlestick pattern detected",
    "VDKBEAR": "Volume Delivery ratio declining — Bearish distribution",
    "VDKBULL": "Volume Delivery ratio rising — Bullish accumulation",
    "BREAKOUTBEAR": "Price breaking down from support — Bearish breakout",
    "BREAKOUTBULL": "Price breaking out above resistance — Bullish breakout",
}

def _get_scanner_description(scanner_code, scanner_name, scanner_desc):
    """Build a human-readable scanner description from available fields."""
    # Try to match scanner code against known patterns
    if scanner_code:
        sc = scanner_code.upper()
        for pattern, desc in _SCANNER_DESCRIPTIONS.items():
            if pattern in sc:
                return desc
    # Fall back to scanner description / name
    if scanner_desc and scanner_desc != scanner_name:
        return fix_encoding(scanner_desc)
    if scanner_name:
        return fix_encoding(scanner_name)
    return ""

# ─── ROW PARSER ───────────────────────────────────────────────────────────────
def parse_row(row, fname):
    """
    Extract all identifiers + indicator values from any indicator CSV row.

    Returns dict:
      nsCode, mc_codes (list of MC codes to try P0 lookup),
      stkname, ltp, indicators (list of (name, value) tuples),
      signal, comment, scanner_name, scanner_code, scanner_desc, category
    """
    result = {
        "nsCode":       "",
        "mc_codes":     [],   # ordered list of MC codes for P0 lookup
        "stkname":      "",
        "ltp":          None,
        "indicators":   [],
        "signal":       "",
        "comment":      "",
        "scanner_name": "",
        "scanner_code": "",
        "scanner_desc": "",
        "category":     "",
        "mc_code_used": "",   # which MC code resolved
    }

    # ── Direct NSE symbol columns (P1) ──
    for col in ("nsCode", "nseid", "nseCode", "sc_symbol", "symbol", "NSE_SYMBOL"):
        val = _clean(row.get(col, ""))
        if val:
            nsc = _extract_nsecode_clean(val)
            if nsc:
                result["nsCode"] = nsc
                break

    # ── Company name ──
    for col in ("stkname", "StockName", "MC_StkName", "sc_name", "instrument", "stkName"):
        val = _clean(row.get(col, ""))
        if val:
            result["stkname"] = fix_encoding(val)
            break

    # ── LTP ──
    for col in ("LTP", "ltp", "LTP_MC", "currPrice", "cmp", "CMP"):
        val = row.get(col)
        if val is not None and _clean(str(val)):
            ltp = _to_float(val)
            if ltp is not None and ltp > 0:
                result["ltp"] = ltp
                break

    # ── Scanner metadata ──
    result["scanner_name"] = fix_encoding(_clean(row.get("scannerName", row.get("ScannerName", ""))))
    result["scanner_code"] = _clean(row.get("scannerCode", row.get("scanId", "")))
    result["scanner_desc"] = fix_encoding(_clean(row.get("scannerDescription", row.get("scannerDesc", ""))))
    result["category"]     = _clean(row.get("catName", row.get("Category", "")))

    # ── P0 MC code candidates ──

    # From top-level fields (bullish/bearish → scId, analysts_choice/stock_ideas → scid)
    for col in ("scId", "sc_id", "scid"):
        val = _clean(row.get(col, ""))
        if val:
            result["mc_codes"].append(val)
            break

    # From MC_Code column (52wk)
    mc_col = _clean(row.get("MC_Code", ""))
    if mc_col and mc_col not in result["mc_codes"]:
        result["mc_codes"].append(mc_col)

    # From stk_url last segment (stock_ideas, analysts_choice)
    stk_url = _clean(row.get("stk_url", ""))
    if stk_url:
        url_code = _extract_url_code(stk_url)
        if url_code and url_code not in result["mc_codes"]:
            result["mc_codes"].append(url_code)

    # ── Parse meta_data (chart_patterns, technical_picks) ──
    meta_raw = _clean(row.get("meta_data", ""))
    if meta_raw:
        meta = _parse_dict_str(meta_raw)
        if not result["nsCode"]:
            sc_sym = _extract_nsecode_clean(meta.get("sc_symbol", ""))
            if sc_sym:
                result["nsCode"] = sc_sym
        if not result["stkname"]:
            result["stkname"] = fix_encoding(_clean(meta.get("sc_name", "")))
        if result["ltp"] is None or result["ltp"] <= 0:
            for key in ("cmp", "entry_price"):
                v = _to_float(meta.get(key, ""))
                if v and v > 0:
                    result["ltp"] = v
                    break
        # Comment from meta
        if "technical_picks" in fname or "chart_patterns" in fname:
            entry  = _clean(str(meta.get("entry_price", "")))
            tgt1   = _clean(str(meta.get("target_price", meta.get("target_price_1", ""))))
            tgt2   = _clean(str(meta.get("target_price_2", "")))
            sl     = _clean(str(meta.get("stoploss_price", "")))
            ptype  = _clean(str(meta.get("pattern_type", "")))
            tgt_r  = _clean(str(meta.get("target_return_prcnt", "")))
            if ptype and not result["signal"]:
                result["signal"] = ptype.upper()
            parts = []
            if entry: parts.append(f"Entry: ₹{entry}")
            if tgt1:  parts.append(f"Target: ₹{tgt1}")
            if tgt2:  parts.append(f"T2: ₹{tgt2}")
            if sl:    parts.append(f"SL: ₹{sl}")
            if tgt_r: parts.append(f"Upside: {tgt_r}%")
            if parts:
                result["comment"] = " | ".join(parts)

    # ── Parse scannerDetails (scanner CSV files — Python dict format) ──
    sd_raw = _clean(row.get("scannerDetails", ""))
    if sd_raw:
        sd = _parse_dict_str(sd_raw)  # KEY: ast.literal_eval for single-quoted Python dicts

        if not result["stkname"]:
            result["stkname"] = fix_encoding(_clean(sd.get("stkname", "")))

        # stkId → P0 candidate
        sd_stkid  = _clean(sd.get("stkId", ""))
        sd_scurl  = _clean(sd.get("scUrl", ""))
        sd_url_code = _extract_url_code(sd_scurl)

        if sd_stkid and sd_stkid not in result["mc_codes"]:
            result["mc_codes"].insert(0, sd_stkid)   # stkId is highest priority P0
        if sd_url_code and sd_url_code not in result["mc_codes"]:
            result["mc_codes"].append(sd_url_code)

        # LTP from columns array (often the only LTP available)
        sd_cols = sd.get("columns", [])
        for col in sd_cols:
            if col.get("name") == "LTP":
                v = _to_float(col.get("value", ""))
                if v and v > 0 and (result["ltp"] is None or result["ltp"] <= 0):
                    result["ltp"] = v
                break

        # Indicator values from columns
        for col in sd_cols:
            cname = _clean(col.get("name", ""))
            cval  = _clean(col.get("value", ""))
            if cname and cname != "LTP" and cval:
                result["indicators"].append((cname, cval))

    # ── Build IndicatorSummary for scanner files ──
    # (set from scanner columns — e.g. "Adaptive RSI Indicator value: 693.50 | Change%: 3.42%")
    # This is done after we have all indicators collected

    # ── File-specific Signal + Comment ──
    if "chart_patterns" in fname:
        pname   = fix_encoding(_clean(row.get("pattern_name", "")))
        raw_cmt = fix_encoding(_clean(row.get("comment", "")))
        tf      = _clean(row.get("time_frame", ""))
        analyst = _clean(row.get("analyst_name", ""))
        if not result["signal"]:
            result["signal"] = raw_cmt or pname
        extra = []
        if pname:   extra.append(pname)
        if tf:      extra.append(f"TF: {tf}")
        if analyst: extra.append(f"By: {analyst}")
        if extra:
            result["comment"] = (result["comment"] + " | " + " | ".join(extra)).strip(" |")

    elif "technical_picks" in fname:
        reco_type   = _clean(row.get("reco_type", ""))
        call_status = _clean(row.get("call_status", ""))
        strategy    = fix_encoding(_clean(row.get("strategy_name_t", "")))
        rationale   = fix_encoding(str(row.get("rationale", ""))[:200])
        entry       = _clean(str(row.get("entry_price", "")))
        tgt1        = _clean(str(row.get("target_price_1", "")))
        sl          = _clean(str(row.get("stoploss_price", "")))
        analyst     = _clean(row.get("analyst_name", ""))
        unr_pl      = _clean(str(row.get("unrealized_pl_p", "")))
        if not result["signal"]:
            result["signal"] = reco_type.upper() if reco_type else (call_status or "Technical Pick")
        parts = []
        if strategy:    parts.append(strategy)
        if entry:       parts.append(f"Entry: ₹{entry}")
        if tgt1:        parts.append(f"Target: ₹{tgt1}")
        if sl:          parts.append(f"SL: ₹{sl}")
        if unr_pl:      parts.append(f"P&L: {unr_pl}%")
        if analyst:     parts.append(f"By: {analyst}")
        if rationale and not result["comment"]:
            parts.append(rationale[:150])
        if parts:
            result["comment"] = " | ".join(parts)

    elif "stock_ideas" in fname:
        recommend_flag = _clean(str(row.get("recommend_flag", "")))
        org            = _clean(row.get("organization", ""))
        heading        = fix_encoding(str(row.get("heading", ""))[:200])
        rec_price      = _clean(str(row.get("recommended_price", "")))
        tgt_price      = _clean(str(row.get("target_price", "")))
        cur_returns    = _clean(str(row.get("current_returns", "")))
        pot_returns    = _clean(str(row.get("potential_returns_per", "")))
        if not result["signal"]:
            result["signal"] = recommend_flag if recommend_flag else "Research"
        parts = []
        if org:         parts.append(f"By: {org}")
        if heading:     parts.append(heading)
        if rec_price:   parts.append(f"Reco @ ₹{rec_price}")
        if tgt_price:   parts.append(f"Target: ₹{tgt_price}")
        if pot_returns: parts.append(f"Upside: {pot_returns}%")
        if cur_returns: parts.append(f"Current Return: {cur_returns}%")
        result["comment"] = " | ".join(parts)

    elif "analysts_choice" in fname:
        buy_c  = int(_to_float(row.get("buy_count",  0)) or 0)
        hold_c = int(_to_float(row.get("hold_count", 0)) or 0)
        sell_c = int(_to_float(row.get("sell_count", 0)) or 0)
        potential = _clean(str(row.get("profitPotential", "")))
        targets   = _clean(str(row.get("targets", "")))
        if not result["signal"]:
            if buy_c > hold_c and buy_c > sell_c:    result["signal"] = "BUY"
            elif sell_c > buy_c and sell_c > hold_c: result["signal"] = "SELL"
            else:                                     result["signal"] = "HOLD"
        result["comment"] = f"Buy: {buy_c} | Hold: {hold_c} | Sell: {sell_c}"
        if potential:
            result["comment"] += f" | Potential: {potential}%"
        if targets:
            try:
                tgt_data = json.loads(targets) if targets.startswith('[') else ast.literal_eval(targets)
                if tgt_data and isinstance(tgt_data, list):
                    tgt_parts = []
                    for t in tgt_data:
                        if isinstance(t, dict):
                            name = t.get('name','')
                            val  = t.get('value','')
                            pct  = t.get('percentages','')
                            if name and val:
                                pct_str = f" ({pct}%)" if pct != '' else ""
                                tgt_parts.append(f"{name}: ₹{val}{pct_str}")
                    if tgt_parts:
                        result["comment"] += " | " + " / ".join(tgt_parts)
            except Exception:
                pass

    elif "52wk" in fname:
        category = _clean(row.get("Category", ""))
        if not result["signal"]:
            result["signal"] = category if category else "52wk"
        result["comment"] = f"52-Week {'High' if '52weekhigh' in str(category).lower() else 'Low/Event'}"

    elif "bullish" in fname or "bearish" in fname:
        curr_trend   = _clean(str(row.get("currTrend", "")))
        change_date  = _clean(str(row.get("trendChngDate", "")))
        change_price = _clean(str(row.get("trendChngPrice", "")))
        if not result["signal"]:
            result["signal"] = "Bullish Trend" if "bullish" in fname.lower() else "Bearish Trend"
        parts = []
        if curr_trend:   parts.append(f"Trend: {curr_trend}")
        if change_date:  parts.append(f"Since: {change_date}")
        if change_price: parts.append(f"From: ₹{change_price}")
        result["comment"] = " | ".join(parts)

    elif "gainers" in fname:
        chg = _clean(str(row.get("pChange", row.get("perChange", ""))))
        result["signal"]  = "Gainer"
        result["comment"] = f"Change: {chg}%" if chg else ""

    elif "losers" in fname:
        chg = _clean(str(row.get("pChange", row.get("perChange", ""))))
        result["signal"]  = "Loser"
        result["comment"] = f"Change: {chg}%" if chg else ""

    elif "most_active" in fname:
        result["signal"] = "Most Active"

    else:
        # Generic scanner file — build comment from scanner description + indicator values
        sc_desc = _get_scanner_description(
            result["scanner_code"], result["scanner_name"], result["scanner_desc"])
        if not result["signal"]:
            result["signal"] = result["scanner_name"] or result["scanner_code"]
        if sc_desc and not result["comment"]:
            result["comment"] = sc_desc

    return result

# ─── BHAV LOOKUP ─────────────────────────────────────────────────────────────
def _bhav_lookup(symbol, bhav_eq):
    s = str(symbol).strip().upper()
    if s and s in bhav_eq.index:
        row = bhav_eq.loc[s]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return row
    return None

def _price_fallback(ltp, bhav_eq):
    if ltp is None or ltp <= 0:
        return None, None
    tmp = bhav_eq.copy()
    tmp["_diff"] = (tmp["CLOSE_PRICE"] - ltp).abs()
    near = tmp[tmp["_diff"] <= PRICE_TOLERANCE]
    if near.empty:
        return None, None
    best = near.loc[near["_diff"].idxmin()]
    if isinstance(best, pd.DataFrame):
        best = best.iloc[0]
    return best, float(best["_diff"])

# ─── MATCH ROW ────────────────────────────────────────────────────────────────
def match_row(parsed, bhav_eq, mc_map, name_map):
    """
    Multi-priority symbol resolution.
    Returns (bhav_row, match_type, ltp_close_pct, mc_code_used)
    """
    ltp = parsed["ltp"]

    def _validate_ltp(row):
        if ltp is None or ltp <= 0:
            return None
        close = float(row["CLOSE_PRICE"]) if row["CLOSE_PRICE"] is not None else None
        if close and close > 0:
            return round(abs(close - ltp) / ltp * 100, 2)
        return None

    # ── P0: MC Master Mapping ──
    for mc_code in parsed["mc_codes"]:
        if mc_code and mc_code in mc_map:
            nse_sym = mc_map[mc_code]
            row = _bhav_lookup(nse_sym, bhav_eq)
            if row is not None:
                return row, "MC_Mapping", _validate_ltp(row), mc_code
            # Symbol in mapping but not in today's bhav → try anyway with validation
            # (might be a non-trading day or ETF not in EQ series)

    # ── P1: Direct NSE symbol column ──
    if parsed["nsCode"]:
        row = _bhav_lookup(parsed["nsCode"], bhav_eq)
        if row is not None:
            return row, "nsCode_Exact", _validate_ltp(row), ""

    # ── P2: scid that looks like an NSE symbol ──
    for mc_code in parsed["mc_codes"]:
        if mc_code and _looks_like_nse(mc_code):
            row = _bhav_lookup(mc_code, bhav_eq)
            if row is not None:
                pct = _validate_ltp(row)
                if pct is None or pct <= 25:
                    return row, "ScId_AsNSE", pct, mc_code

    # ── P3: NSE EQUITY_L company name → symbol ──
    if parsed["stkname"] and name_map:
        symbol = name_to_symbol(parsed["stkname"], name_map)
        if symbol:
            row = _bhav_lookup(symbol, bhav_eq)
            if row is not None:
                pct = _validate_ltp(row)
                if pct is not None and pct > 30:
                    # LTP mismatch — try price fallback first
                    pf_row, pf_diff = _price_fallback(ltp, bhav_eq)
                    if pf_row is not None and pf_diff is not None and pf_diff <= 0.1:
                        return pf_row, f"PriceFallback(±{pf_diff:.2f})", 0.0, ""
                    return row, "Name_Mapped(LTP_WARN)", pct, ""
                return row, "Name_Mapped", pct, ""

    # ── P4: MC Search API ──
    if parsed["stkname"] and MC_TOKEN:
        symbol = mc_search_symbol(parsed["stkname"])
        if symbol:
            row = _bhav_lookup(symbol, bhav_eq)
            if row is not None:
                return row, "MC_Search", _validate_ltp(row), ""

    # ── P5: Price fallback (last resort, flagged) ──
    row, diff = _price_fallback(ltp, bhav_eq)
    if row is not None:
        return row, f"PriceFallback(±{diff:.2f})", 0.0, ""

    return None, None, None, ""

# ─── CONFIDENCE SCORE ────────────────────────────────────────────────────────
def compute_confidence(mtype, ltp_close_pct):
    base = {
        "MC_Mapping":   100,
        "nsCode_Exact":  98,
        "ScId_AsNSE":    88,
        "Name_Mapped":   82,
        "MC_Search":     85,
    }
    if mtype in base:
        score = base[mtype]
    elif mtype == "Name_Mapped(LTP_WARN)":
        score = 55
    elif mtype.startswith("PriceFallback"):
        try:
            diff = float(mtype.split("±")[1].rstrip(")"))
            score = max(int(65 - diff * 80), 15)
        except Exception:
            score = 30
    else:
        score = 50

    if ltp_close_pct is not None:
        if ltp_close_pct <= 1:    score = min(score + 5, 100)
        elif ltp_close_pct <= 3:  score = min(score + 2, 100)
        elif ltp_close_pct > 20 and mtype not in ("MC_Mapping", "nsCode_Exact"):
            score = max(score - 15, 15)
        elif ltp_close_pct > 10 and mtype == "Name_Mapped":
            score = max(score - 8, 30)

    return round(score)

# ─── TREND DETECTION ─────────────────────────────────────────────────────────
def detect_trend(fname, signal):
    fl = fname.lower()
    sl = str(signal).lower()
    if any(x in fl for x in ["bullish", "bull", "gainer"]):
        return "Bullish"
    if any(x in fl for x in ["bearish", "bear", "loser"]):
        return "Bearish"
    if "buy" in sl or "bullish" in sl:
        return "Bullish"
    if "sell" in sl or "bearish" in sl:
        return "Bearish"
    return "Neutral"

# ─── PROCESS ONE DATE FOLDER ─────────────────────────────────────────────────
def process_date(date_folder, mc_map, mapping_df, name_map):
    trade_date = os.path.basename(date_folder)
    print(f"\n📅 Processing: {trade_date}")

    bhav_eq = load_bhav(trade_date)
    if bhav_eq is None:
        return [], mc_map, mapping_df

    print(f"  📊 Bhav: {len(bhav_eq):,} rows | {bhav_eq.index.nunique():,} unique symbols")

    results   = []
    ind_files = sorted(glob.glob(os.path.join(date_folder, "*.csv")))
    print(f"  📂 Files: {len(ind_files)}")

    # Track unknown MC codes for master mapping update
    unknown_mc_codes = set()
    matched_count = 0

    for ind_file in ind_files:
        fname = os.path.basename(ind_file)
        try:
            df = pd.read_csv(ind_file, low_memory=False)
        except Exception as e:
            print(f"  ⚠️ Cannot read {fname}: {e}")
            continue
        df.columns = df.columns.str.strip()

        file_matched = file_skipped = 0
        match_types  = {}
        conf_scores  = []

        for _, row in df.iterrows():
            parsed = parse_row(row, fname)

            # Skip rows with nothing to work with
            if (not parsed["nsCode"] and not parsed["mc_codes"] and
                    not parsed["stkname"] and
                    (parsed["ltp"] is None or parsed["ltp"] <= 0)):
                file_skipped += 1
                continue

            # Track unknown MC codes for batch API lookup
            for mc in parsed["mc_codes"]:
                if mc and mc not in mc_map:
                    unknown_mc_codes.add(mc)

            best, mtype, ltp_pct, mc_used = match_row(parsed, bhav_eq, mc_map, name_map)
            if best is None:
                file_skipped += 1
                continue

            match_types[mtype] = match_types.get(mtype, 0) + 1
            confidence = compute_confidence(mtype, ltp_pct)
            conf_scores.append(confidence)
            matched_count += 1

            # Build IndicatorSummary: e.g. "Adaptive RSI Indicator value: 693.50 | Change%: 3.42%"
            inds = parsed["indicators"]
            ind_summary_parts = []
            for (iname, ival) in inds:
                ind_summary_parts.append(f"{iname}: {ival}")
            indicator_summary = " | ".join(ind_summary_parts) if ind_summary_parts else ""

            # Also add scanner description to comment if it's a scanner file and comment is empty
            if not parsed["comment"] and parsed["scanner_name"]:
                sc_desc = _get_scanner_description(
                    parsed["scanner_code"], parsed["scanner_name"], parsed["scanner_desc"])
                if sc_desc:
                    parsed["comment"] = sc_desc

            # Up to 3 indicator name/value pairs
            ind1_n = inds[0][0] if len(inds) > 0 else ""
            ind1_v = inds[0][1] if len(inds) > 0 else ""
            ind2_n = inds[1][0] if len(inds) > 1 else ""
            ind2_v = inds[1][1] if len(inds) > 1 else ""
            ind3_n = inds[2][0] if len(inds) > 2 else ""
            ind3_v = inds[2][1] if len(inds) > 2 else ""

            trend = detect_trend(fname, parsed["signal"])

            results.append({
                "Date":             trade_date,
                "Trend":            trend,
                "Signal":           fix_encoding(parsed["signal"]),
                "Comment":          fix_encoding(parsed["comment"]),
                "IndicatorSummary": indicator_summary,
                "ScannerName":      parsed["scanner_name"],
                "ScannerCode":      parsed["scanner_code"],
                "Category":         parsed["category"],
                # MC source data (for audit / reference)
                "MC_StkName":       parsed["stkname"],
                "MC_Code_Used":     mc_used,
                "LTP_MC":           parsed["ltp"],
                # NSE confirmed data from Bhavcopy
                "NSE_SYMBOL":       best["SYMBOL"],
                "SERIES":           best["SERIES"],
                "PREV_CLOSE":       best.get("PREV_CLOSE", ""),
                "OPEN":             best["OPEN_PRICE"],
                "HIGH":             best["HIGH_PRICE"],
                "LOW":              best["LOW_PRICE"],
                "CLOSE":            best["CLOSE_PRICE"],
                "AVG_PRICE":        best["AVG_PRICE"],
                "VOLUME":           best["TTL_TRD_QNTY"],
                "TURNOVER_LACS":    best["TURNOVER_LACS"],
                "NO_OF_TRADES":     best["NO_OF_TRADES"],
                "DELIV_QTY":        best["DELIV_QTY"],
                "DELIV_PER":        best["DELIV_PER"],
                # Indicator values
                "Ind1_Name":        ind1_n,
                "Ind1_Value":       ind1_v,
                "Ind2_Name":        ind2_n,
                "Ind2_Value":       ind2_v,
                "Ind3_Name":        ind3_n,
                "Ind3_Value":       ind3_v,
                # Quality
                "IndicatorFile":    fname,
                "MatchType":        mtype,
                "ConfidenceScore":  confidence,
                "LTP_Close_Pct":    f"{ltp_pct:.1f}%" if ltp_pct is not None else "",
            })
            file_matched += 1

        avg_conf = round(sum(conf_scores) / len(conf_scores)) if conf_scores else 0
        mt_str = " | ".join(f"{k}:{v}" for k, v in sorted(match_types.items()))
        print(f"  ✅ {fname:<42} {file_matched:>5} matched | {file_skipped:>4} skipped | AvgConf: {avg_conf}  [{mt_str}]")

    # ── Auto-discover unknown MC codes → update master mapping ──
    if unknown_mc_codes:
        print(f"\n  🔍 Looking up {len(unknown_mc_codes)} unknown MC codes...")
        new_entries = []
        for mc_code in sorted(unknown_mc_codes):
            result = lookup_new_mc_code(mc_code)
            if result and result["NSE_Symbol"]:
                mc_map[mc_code] = result["NSE_Symbol"]
                new_entries.append(result)
                print(f"    + {mc_code} → {result['NSE_Symbol']} ({result['Full_Name']})")
            time.sleep(0.05)  # gentle rate limit
        if new_entries:
            mapping_df = update_mc_mapping(mapping_df, new_entries)
            print(f"  ✅ {len(new_entries)} new MC codes added to master mapping")

    print(f"\n  📊 Date total: {matched_count:,} matched")
    return results, mc_map, mapping_df

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST)
    print(f"🕐 NSE Engine v5 started: {now_ist.strftime('%d-%b-%Y %H:%M IST')}")

    print("\n📥 Loading MC Master Mapping (P0)...")
    mc_map, mapping_df = load_mc_mapping()

    print("\n📥 Loading NSE EQUITY_L (P3 name→symbol)...")
    name_map = load_equity_l()

    def _folder_sort_key(path):
        try:
            return datetime.strptime(os.path.basename(path), "%d-%b-%Y")
        except Exception:
            return datetime.min

    date_folders = sorted(glob.glob(os.path.join(INDICATOR_ROOT, "*")), key=_folder_sort_key, reverse=True)
    if not date_folders:
        msg = f"❌ NSE Engine — No indicator folders in `{INDICATOR_ROOT}/`"
        print(msg); tg_message(msg)
        return

    # Process ONLY the latest date folder (today's data — not consolidated)
    date_folders = date_folders[:1]

    all_results = []
    for folder in date_folders:
        if not os.path.isdir(folder):
            continue
        results, mc_map, mapping_df = process_date(folder, mc_map, mapping_df, name_map)
        all_results.extend(results)

    if not all_results:
        msg = f"⚠️ NSE Engine — {now_ist.strftime('%d-%b-%Y')}\\nNo matches found."
        print(msg); tg_message(msg)
        return

    df = pd.DataFrame(all_results)

    # Sort: MC_Mapping first, then by confidence, trend, symbol
    df["_sort_match"] = df["MatchType"].apply(lambda x: 0 if x == "MC_Mapping" else
                                               1 if x == "nsCode_Exact" else
                                               2 if x == "ScId_AsNSE" else
                                               3 if x.startswith("Name_Mapped") else
                                               4 if x == "MC_Search" else 5)
    df.sort_values(["_sort_match", "ConfidenceScore", "Trend", "NSE_SYMBOL"],
                   ascending=[True, False, True, True], inplace=True)
    df.drop(columns=["_sort_match"], inplace=True)
    df.reset_index(drop=True, inplace=True)

    # ── Match quality report ──
    print("\n📊 Match Type Distribution:")
    for mt, cnt in df["MatchType"].value_counts().items():
        flag = "  ⚠️ REVIEW" if "PriceFallback" in str(mt) or "WARN" in str(mt) else "  ✅"
        pct = cnt / len(df) * 100
        print(f"  {mt:<40}: {cnt:>6,} ({pct:.1f}%){flag}")

    # ── Summary ──
    total      = len(df)
    accurate   = len(df[df["ConfidenceScore"] >= 80])
    moderate   = len(df[(df["ConfidenceScore"] >= 60) & (df["ConfidenceScore"] < 80)])
    low_conf   = len(df[df["ConfidenceScore"] < 60])
    bullish_n  = len(df[df["Trend"] == "Bullish"])
    bearish_n  = len(df[df["Trend"] == "Bearish"])
    symbols    = df["NSE_SYMBOL"].nunique()
    avg_conf   = round(df["ConfidenceScore"].mean())
    files_done = df["IndicatorFile"].nunique()

    mc_map_count = len(df[df["MatchType"] == "MC_Mapping"])
    mc_map_pct   = mc_map_count / total * 100

    print(f"\n✅ Total: {total:,} | Files: {files_done} | Symbols: {symbols:,}")
    print(f"   🎯 MC_Mapping (P0): {mc_map_count:,} ({mc_map_pct:.0f}%) — master table hits")
    print(f"   🟢 High confidence (≥80): {accurate:,} ({accurate/total*100:.1f}%)")
    print(f"   🟡 Moderate (60-79):      {moderate:,} ({moderate/total*100:.1f}%)")
    print(f"   🔴 Low (<60):             {low_conf:,} ({low_conf/total*100:.1f}%)")
    print(f"   Avg ConfidenceScore: {avg_conf}")
    print(f"   Bullish: {bullish_n:,} | Bearish: {bearish_n:,}")

    # ── Save output ──
    latest_date = df["Date"].max()
    out_fname   = f"NSE_Indicator_Report_{latest_date.replace(' ', '_')}.csv"
    out_path    = os.path.join(OUTPUT_ROOT, out_fname)
    tmp_path    = f"/tmp/{out_fname}"
    df.to_csv(out_path, index=False)
    df.to_csv(tmp_path, index=False)
    print(f"💾 Saved: {out_path}")

    # ── Save consolidated report ──
    CONSOLIDATED_ROOT = "consolidated_data"
    os.makedirs(CONSOLIDATED_ROOT, exist_ok=True)
    consolidated_path = os.path.join(CONSOLIDATED_ROOT, "NSE_Indicator_Report_Consolidated.csv")

    # Load existing consolidated file
    if os.path.exists(consolidated_path):
        existing = pd.read_csv(consolidated_path, low_memory=False)
        existing_dates = set(existing["Date"].unique()) if "Date" in existing.columns else set()
    else:
        existing = pd.DataFrame()
        existing_dates = set()

    # Scan ALL daily output files — pick up any dates not yet in consolidated
    all_parts = [df]  # today's fresh data always included
    for daily_file in sorted(glob.glob(os.path.join(OUTPUT_ROOT, "NSE_Indicator_Report_*.csv"))):
        try:
            part = pd.read_csv(daily_file, low_memory=False)
            if "Date" not in part.columns or part.empty:
                continue
            file_date = part["Date"].max()
            if file_date not in existing_dates and file_date != df["Date"].max():
                all_parts.append(part)
                print(f"  ➕ Backfilled from {os.path.basename(daily_file)} ({len(part):,} rows)")
        except Exception as e:
            print(f"  ⚠️ Could not read {daily_file}: {e}")

    # Merge: existing consolidated (minus today to avoid dupes) + all missing dates + today
    if not existing.empty and "Date" in existing.columns:
        existing = existing[existing["Date"] != df["Date"].max()]
        all_parts.append(existing)

    combined = pd.concat(all_parts, ignore_index=True)
    # Sort by Date descending then Symbol
    if "Date" in combined.columns:
        combined.sort_values(["Date", "NSE_SYMBOL"], ascending=[False, True], inplace=True)
        combined.reset_index(drop=True, inplace=True)

    combined.to_csv(consolidated_path, index=False)
    print(f"📦 Consolidated: {consolidated_path} ({len(combined):,} rows, {combined['Date'].nunique()} dates)")

    # ── Top signals ──
    top_bull = (df[(df["Trend"] == "Bullish") & (df["ConfidenceScore"] >= 80)]
                ["NSE_SYMBOL"].value_counts().head(6).index.tolist())
    top_bear = (df[(df["Trend"] == "Bearish") & (df["ConfidenceScore"] >= 80)]
                ["NSE_SYMBOL"].value_counts().head(6).index.tolist())

    # ── Telegram ──
    caption = (
        f"📊 *NSE Indicator Engine v5*\n"
        f"📅 *{latest_date}* | Files: {files_done}\n"
        f"📈 Total: *{total:,}* | Symbols: *{symbols:,}*\n"
        f"🎯 MC Mapping (P0): *{mc_map_count:,}* ({mc_map_pct:.0f}%)\n"
        f"✅ High Conf (≥80): *{accurate:,}* ({accurate/total*100:.0f}%) | Avg: {avg_conf}\n"
        f"🟢 Bullish: {bullish_n:,} | 🔴 Bearish: {bearish_n:,}\n"
        f"🟢 Top Bull: {', '.join(top_bull) if top_bull else 'None'}\n"
        f"🔴 Top Bear: {', '.join(top_bear) if top_bear else 'None'}\n"
        f"🕐 {now_ist.strftime('%H:%M IST')}"
    )[:1024]

    tg_file(tmp_path, caption)
    print("🏁 NSE Engine v5 complete.")

if __name__ == "__main__":
    main()
