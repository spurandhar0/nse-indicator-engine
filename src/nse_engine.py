"""
NSE Indicator Engine  — v4  (Double-Validated Symbol Resolution)
================================================================
Processes all 30 indicator CSV files (from MoneyControl) and matches
each stock with NSE Bhavcopy using multi-priority symbol resolution
with LTP-based cross-validation and confidence scoring.

Symbol resolution priority (highest → lowest accuracy):
  P1. nsCode / sc_symbol column (clean NSE code)    → 100% accurate
  P2. stockShortName (looks like NSE symbol)         → 95% accurate
  P3. scid / scId (MC code that matches NSE symbol)  → 90% accurate
  P4. NSE EQUITY_L company name → symbol (fuzzy)    → 80% accurate
  P5. MC Search API (name → nseid)                   → 85% accurate
  P6. LTP price match ±0.5 (LAST RESORT)            → 30-65% accurate

Every P4/P5 match is cross-validated against LTP to adjust
the ConfidenceScore. Mismatches > 20% are flagged.

Output columns:
  Date, Trend, Signal, Comment, ScannerName, ScannerCode, Category,
  MC_StkName, LTP_MC, NSE_SYMBOL, SERIES, PREV_CLOSE, OPEN, HIGH, LOW,
  CLOSE, AVG_PRICE, VOLUME, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY,
  DELIV_PER, Ind1_Name, Ind1_Value, Ind2_Name, Ind2_Value, Ind3_Name,
  Ind3_Value, IndicatorFile, MatchType, ConfidenceScore, LTP_Close_Pct
"""

import os, glob, re, json, ast, difflib
from io import StringIO
import pandas as pd
import requests
from datetime import datetime
import pytz

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["TG_BOT_TOKEN"]
CHAT_ID    = os.environ["TG_CHAT_ID"]
MC_TOKEN   = os.environ.get("MC_AUTH_TOKEN", "")

INDICATOR_ROOT = "indicator_data"
BHAV_ROOT      = "bhav_data"
OUTPUT_ROOT    = "output_data"
os.makedirs(OUTPUT_ROOT, exist_ok=True)

PRICE_TOLERANCE = 0.5   # ±₹ for P6 last-resort
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
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                df = pd.read_csv(StringIO(r.text))
                df.columns = df.columns.str.strip()
                name_col = [c for c in df.columns if "NAME" in c.upper()]
                sym_col  = [c for c in df.columns if "SYMBOL" in c.upper()]
                isin_col = [c for c in df.columns if "ISIN" in c.upper()]
                if not name_col or not sym_col:
                    continue
                name_map, isin_map = {}, {}
                for _, row in df.iterrows():
                    symbol = str(row[sym_col[0]]).strip().upper()
                    name   = str(row[name_col[0]]).strip()
                    norm   = _normalize_name(name)
                    name_map[norm] = symbol
                    # Also map symbol itself as key (for short-name hits)
                    name_map[symbol.lower()] = symbol
                    if isin_col:
                        isin = str(row[isin_col[0]]).strip()
                        if isin:
                            isin_map[isin] = symbol
                print(f"  ✅ NSE EQUITY_L loaded: {len(name_map)} entries from {url}")
                return name_map, isin_map
        except Exception as e:
            print(f"  ⚠️ EQUITY_L from {url}: {e}")
    print("  ⚠️ NSE EQUITY_L unavailable — falling back to price matching")
    return {}, {}

def name_to_symbol(company_name, name_map):
    """Normalized + fuzzy match. Returns NSE symbol or None."""
    if not company_name or not name_map:
        return None
    norm = _normalize_name(company_name)
    # Exact normalized
    if norm in name_map:
        return name_map[norm]
    # Fuzzy (cutoff 0.80)
    matches = difflib.get_close_matches(norm, name_map.keys(), n=1, cutoff=0.80)
    if matches:
        return name_map[matches[0]]
    return None

# ─── MC SEARCH API (Priority 5) ──────────────────────────────────────────────
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

# ─── LOAD BHAV ────────────────────────────────────────────────────────────────
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

# ─── HELPERS ─────────────────────────────────────────────────────────────────
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
    """True if string looks like a valid NSE equity symbol."""
    return bool(s and len(s) <= 20 and _NSE_RE.match(s.upper()))

def _parse_dict_str(raw):
    """Parse Python dict string (single quotes) OR JSON string. Returns dict."""
    if not raw:
        return {}
    try:
        return ast.literal_eval(raw)   # handles Python-style dicts (single quotes)
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _extract_nsecode_clean(raw):
    """
    Extract clean NSE symbol from a raw nsCode / sc_symbol value.
    Handles URL paths like '/infrastructure-general/abbindia/ABB' → 'ABB'
    Returns '' if no valid NSE code found.
    """
    s = _clean(raw).upper()
    if not s:
        return ""
    # URL path format → extract last segment
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    # Only return if it looks like a valid NSE symbol
    if _looks_like_nse(s):
        return s
    return ""

# ─── ROW PARSER ───────────────────────────────────────────────────────────────
def parse_row(row, fname):
    """
    Extract all identifiers + indicator values from any row.
    Returns dict with: nsCode, stkId, scid, short_name, stkname, ltp,
                       indicators (list of (name, value) tuples),
                       signal, comment
    """
    # ── P1: Direct NSE symbol columns ──
    nscode = ""
    for col in ("nsCode", "nseid", "nseCode", "sc_symbol", "symbol", "NSE_SYMBOL"):
        val = _clean(row.get(col, ""))
        if val:
            nscode = _extract_nsecode_clean(val)
            if nscode:
                break

    # ── P2: stockShortName (sometimes is NSE symbol) ──
    short_name = _clean(row.get("stockShortName", "")).upper()

    # ── P3: scid / scId (MC code, some equal NSE symbols) ──
    scid = _clean(row.get("scid", row.get("sc_id", row.get("scId", ""))))

    # ── Company name for P4 (Name_Mapped) ──
    stkname = ""
    for col in ("stkname", "StockName", "MC_StkName", "sc_name", "instrument", "stkName"):
        val = _clean(row.get(col, ""))
        if val:
            stkname = val
            break

    # ── LTP from various columns ──
    ltp = None
    for col in ("LTP", "ltp", "LTP_MC", "currPrice", "cmp", "CMP"):
        val = row.get(col)
        if val is not None and _clean(str(val)):
            ltp = _to_float(val)
            if ltp is not None and ltp > 0:
                break

    # ── Indicator values (from scanner columns array) ──
    indicators = []

    # ── Signal + Comment derivation ──
    signal  = ""
    comment = ""

    # ── Parse meta_data (chart_patterns, technical_picks) ──
    meta_raw = _clean(row.get("meta_data", ""))
    if meta_raw:
        meta = _parse_dict_str(meta_raw)
        if not nscode:
            sc_sym = _extract_nsecode_clean(meta.get("sc_symbol", ""))
            if sc_sym:
                nscode = sc_sym
        if not stkname:
            stkname = _clean(meta.get("sc_name", ""))
        if ltp is None or ltp <= 0:
            for key in ("cmp", "entry_price"):
                v = _to_float(meta.get(key, ""))
                if v and v > 0:
                    ltp = v
                    break

        # Technical picks / chart patterns comment
        if "technical_picks" in fname or "chart_patterns" in fname:
            entry  = _clean(str(meta.get("entry_price", "")))
            tgt1   = _clean(str(meta.get("target_price", meta.get("target_price_1", ""))))
            tgt2   = _clean(str(meta.get("target_price_2", "")))
            sl     = _clean(str(meta.get("stoploss_price", "")))
            ptype  = _clean(str(meta.get("pattern_type", "")))
            tgt_r  = _clean(str(meta.get("target_return_prcnt", "")))
            if ptype and not signal:
                signal = ptype.upper()
            parts = []
            if entry:  parts.append(f"Entry:{entry}")
            if tgt1:   parts.append(f"Target:{tgt1}")
            if tgt2:   parts.append(f"T2:{tgt2}")
            if sl:     parts.append(f"SL:{sl}")
            if tgt_r:  parts.append(f"Upside:{tgt_r}%")
            if parts:
                comment = " | ".join(parts)

    # ── Parse scannerDetails (scanner CSV files — Python dict format!) ──
    sd_raw = _clean(row.get("scannerDetails", ""))
    if sd_raw:
        sd = _parse_dict_str(sd_raw)   # ← KEY FIX: ast.literal_eval handles single quotes

        if not stkname:
            stkname = _clean(sd.get("stkname", ""))
        # stkId from scannerDetails (MC internal code, P3 candidate only)
        sd_stkid = _clean(sd.get("stkId", ""))

        # LTP from scannerDetails.columns (ltp field is often empty!)
        sd_cols = sd.get("columns", [])
        for col in sd_cols:
            if col.get("name") == "LTP":
                v = _to_float(col.get("value", ""))
                if v and v > 0:
                    ltp = v
                break

        # Extract indicator values from scannerDetails.columns
        for col in sd_cols:
            cname = _clean(col.get("name", ""))
            cval  = _clean(col.get("value", ""))
            if cname and cname != "LTP" and cname != "change %" and cval:
                indicators.append((cname, cval))
            elif cname == "change %" and cval:
                indicators.append(("Change%", cval))

    # ── File-specific Signal + Comment ──
    if "chart_patterns" in fname:
        pname    = _clean(row.get("pattern_name", ""))
        raw_cmt  = _clean(row.get("comment", ""))
        tf       = _clean(row.get("time_frame", ""))
        analyst  = _clean(row.get("analyst_name", ""))
        if not signal:
            if raw_cmt:
                signal = raw_cmt
            elif pname:
                signal = pname
        extra_parts = []
        if pname:    extra_parts.append(pname)
        if tf:       extra_parts.append(f"TF:{tf}")
        if analyst:  extra_parts.append(f"By:{analyst}")
        if extra_parts:
            comment = (comment + " | " + " | ".join(extra_parts)).strip(" |")

    elif "technical_picks" in fname:
        reco_type    = _clean(row.get("reco_type", ""))
        call_status  = _clean(row.get("call_status", ""))
        strategy     = _clean(row.get("strategy_name_t", ""))
        rationale    = _clean(str(row.get("rationale", ""))[:200])
        entry        = _clean(str(row.get("entry_price", "")))
        tgt1         = _clean(str(row.get("target_price_1", "")))
        sl           = _clean(str(row.get("stoploss_price", "")))
        analyst      = _clean(row.get("analyst_name", ""))
        unr_pl       = _clean(str(row.get("unrealized_pl_p", "")))
        if not signal:
            signal = reco_type.upper() if reco_type else (call_status or "Technical Pick")
        parts = []
        if strategy:    parts.append(strategy)
        if entry:       parts.append(f"Entry:{entry}")
        if tgt1:        parts.append(f"Target:{tgt1}")
        if sl:          parts.append(f"SL:{sl}")
        if unr_pl:      parts.append(f"P&L:{unr_pl}%")
        if analyst:     parts.append(f"By:{analyst}")
        if rationale and not comment:
            parts.append(rationale[:150])
        if parts:
            comment = " | ".join(parts)

    elif "stock_ideas" in fname:
        recommend_flag = _clean(str(row.get("recommend_flag", "")))
        org            = _clean(row.get("organization", ""))
        heading        = _clean(str(row.get("heading", ""))[:200])
        rec_price      = _clean(str(row.get("recommended_price", "")))
        tgt_price      = _clean(str(row.get("target_price", "")))
        cur_returns    = _clean(str(row.get("current_returns", "")))
        pot_returns    = _clean(str(row.get("potential_returns_per", "")))
        if not signal:
            signal = recommend_flag if recommend_flag else "Research"
        parts = []
        if org:         parts.append(f"By:{org}")
        if heading:     parts.append(heading)
        if rec_price:   parts.append(f"Reco@{rec_price}")
        if tgt_price:   parts.append(f"Target:{tgt_price}")
        if pot_returns: parts.append(f"Upside:{pot_returns}%")
        if cur_returns: parts.append(f"Current:{cur_returns}%")
        comment = " | ".join(parts)

    elif "analysts_choice" in fname:
        buy_c  = int(_to_float(row.get("buy_count", 0)) or 0)
        hold_c = int(_to_float(row.get("hold_count", 0)) or 0)
        sell_c = int(_to_float(row.get("sell_count", 0)) or 0)
        potential = _clean(str(row.get("profitPotential", "")))
        targets   = _clean(str(row.get("targets", "")))
        if not signal:
            if buy_c > hold_c and buy_c > sell_c:
                signal = "BUY"
            elif sell_c > buy_c and sell_c > hold_c:
                signal = "SELL"
            else:
                signal = "HOLD"
        comment = f"Buy:{buy_c} Hold:{hold_c} Sell:{sell_c}"
        if potential:
            comment += f" | Potential:{potential}%"
        if targets:
            try:
                tgt_data = json.loads(targets) if targets.startswith('[') else ast.literal_eval(targets)
                if tgt_data and isinstance(tgt_data, list):
                    tgt_val = tgt_data[0].get('targetPrice','') if isinstance(tgt_data[0], dict) else ''
                    if tgt_val:
                        comment += f" | Target:{tgt_val}"
            except Exception:
                pass

    elif "52wk" in fname:
        category = _clean(row.get("Category", ""))
        if not signal:
            signal = category if category else "52wk"

    elif "bullish" in fname or "bearish" in fname:
        curr_trend   = _clean(str(row.get("currTrend", "")))
        change_date  = _clean(str(row.get("trendChngDate", "")))
        change_price = _clean(str(row.get("trendChngPrice", "")))
        if not signal:
            signal = "Bullish Trend" if "bullish" in fname.lower() else "Bearish Trend"
        parts = []
        if curr_trend:   parts.append(f"Trend:{curr_trend}")
        if change_date:  parts.append(f"Since:{change_date}")
        if change_price: parts.append(f"From:₹{change_price}")
        comment = " | ".join(parts)

    elif "gainers" in fname:
        signal  = "Gainer"
        chg     = _clean(str(row.get("pChange", row.get("perChange", ""))))
        comment = f"Change:{chg}%" if chg else ""

    elif "losers" in fname:
        signal  = "Loser"
        chg     = _clean(str(row.get("pChange", row.get("perChange", ""))))
        comment = f"Change:{chg}%" if chg else ""

    elif "most_active" in fname:
        signal = "Most Active"

    else:
        # Generic scanner
        scanner_name = _clean(row.get("scannerName", row.get("ScannerName", "")))
        if not signal:
            signal = scanner_name if scanner_name else ""
        scanner_desc = _clean(row.get("scannerDescription", row.get("scannerDesc", "")))
        if scanner_desc and not comment:
            comment = scanner_desc[:250]

    return {
        "nsCode":     nscode,
        "short_name": short_name,
        "scid":       scid,
        "stkname":    stkname,
        "ltp":        ltp,
        "indicators": indicators,
        "signal":     signal,
        "comment":    comment,
    }

# ─── SYMBOL MATCHERS ─────────────────────────────────────────────────────────
def _bhav_lookup(symbol, bhav_eq):
    s = str(symbol).strip().upper()
    if s and s in bhav_eq.index:
        row = bhav_eq.loc[s]
        # Handle duplicate symbols (take first)
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

def match_row(parsed, bhav_eq, name_map):
    """
    6-priority symbol resolution.
    Returns (bhav_row, match_type, ltp_close_pct)
    """
    ltp = parsed["ltp"]

    def _validate_ltp(row):
        """Returns pct diff between LTP and CLOSE_PRICE (None if can't compute)."""
        if ltp is None or ltp <= 0:
            return None
        close = float(row["CLOSE_PRICE"]) if row["CLOSE_PRICE"] is not None else None
        if close and close > 0:
            return round(abs(close - ltp) / ltp * 100, 2)
        return None

    # ── P1: Direct NSE symbol ──
    if parsed["nsCode"]:
        row = _bhav_lookup(parsed["nsCode"], bhav_eq)
        if row is not None:
            return row, "nsCode_Exact", _validate_ltp(row)

    # ── P2: stockShortName as NSE symbol (e.g., "IEX", "TCS") ──
    if parsed["short_name"] and _looks_like_nse(parsed["short_name"]):
        row = _bhav_lookup(parsed["short_name"], bhav_eq)
        if row is not None:
            pct = _validate_ltp(row)
            # Accept if LTP is reasonable or not available
            if pct is None or pct <= 30:
                return row, "ShortName_Exact", pct

    # ── P3: scid as NSE symbol (some MC codes = NSE codes e.g. "TCS", "IT") ──
    if parsed["scid"] and _looks_like_nse(parsed["scid"]):
        row = _bhav_lookup(parsed["scid"], bhav_eq)
        if row is not None:
            pct = _validate_ltp(row)
            if pct is None or pct <= 30:
                return row, "ScId_Exact", pct

    # ── P4: NSE EQUITY_L company name fuzzy lookup ──
    if parsed["stkname"] and name_map:
        symbol = name_to_symbol(parsed["stkname"], name_map)
        if symbol:
            row = _bhav_lookup(symbol, bhav_eq)
            if row is not None:
                pct = _validate_ltp(row)
                # Cross-validate: if LTP mismatch > 30%, try price fallback first
                if pct is not None and pct > 30:
                    pf_row, pf_diff = _price_fallback(ltp, bhav_eq)
                    if pf_row is not None and pf_diff is not None and pf_diff <= 0.1:
                        pf_pct = _validate_ltp(pf_row)
                        return pf_row, f"PriceFallback(±{pf_diff:.2f})", pf_pct
                    # Keep name match but flag LTP mismatch
                    return row, "Name_Mapped(LTP_WARN)", pct
                return row, "Name_Mapped", pct

    # ── P5: MC Search API ──
    if parsed["stkname"] and MC_TOKEN:
        symbol = mc_search_symbol(parsed["stkname"])
        if symbol:
            row = _bhav_lookup(symbol, bhav_eq)
            if row is not None:
                return row, "MC_Search", _validate_ltp(row)

    # ── P6: Price fallback (last resort) ──
    row, diff = _price_fallback(ltp, bhav_eq)
    if row is not None:
        return row, f"PriceFallback(±{diff:.2f})", 0.0

    return None, None, None

# ─── CONFIDENCE SCORE ────────────────────────────────────────────────────────
def compute_confidence(mtype, ltp_close_pct):
    """Score 0-100: how confident are we in the symbol match."""
    base = {
        "nsCode_Exact":    100,
        "ShortName_Exact":  95,
        "ScId_Exact":       90,
        "Name_Mapped":      82,
        "MC_Search":        86,
    }
    if mtype in base:
        score = base[mtype]
    elif mtype == "Name_Mapped(LTP_WARN)":
        score = 60   # name matched but LTP diverges — lower confidence
    elif mtype.startswith("PriceFallback"):
        try:
            diff = float(mtype.split("±")[1].rstrip(")"))
            # ±0.00 → 65, ±0.25 → 50, ±0.50 → 25
            score = max(int(65 - diff * 80), 15)
        except Exception:
            score = 30
    else:
        score = 50

    # Adjust based on LTP-vs-Close validation
    if ltp_close_pct is not None:
        if ltp_close_pct <= 2:
            score = min(score + 5, 100)
        elif ltp_close_pct <= 5:
            score = min(score + 2, 100)
        elif ltp_close_pct > 20 and mtype not in ("nsCode_Exact",):
            score = max(score - 15, 15)
        elif ltp_close_pct > 10 and mtype == "Name_Mapped":
            score = max(score - 8, 30)

    return round(score)

# ─── TREND DETECTION ─────────────────────────────────────────────────────────
def detect_trend(fname, signal):
    """Determine Bullish/Bearish/Unknown from filename and signal."""
    fl = fname.lower()
    sl = str(signal).lower()
    if any(x in fl for x in ["bull", "gainer", "gain"]):
        return "Bullish"
    if any(x in fl for x in ["bear", "loser", "loss"]):
        return "Bearish"
    if "buy" in sl:
        return "Bullish"
    if "sell" in sl:
        return "Bearish"
    if "bullish" in sl:
        return "Bullish"
    if "bearish" in sl:
        return "Bearish"
    if "52weekhigh" in sl.replace(" ", ""):
        return "Bullish"
    if "52weeklow" in sl.replace(" ", ""):
        return "Bearish"
    return "Unknown"

# ─── PROCESS ONE DATE FOLDER ─────────────────────────────────────────────────
def process_date(date_folder, name_map):
    trade_date = os.path.basename(date_folder)
    print(f"\n📅 Processing: {trade_date}")

    bhav_eq = load_bhav(trade_date)
    if bhav_eq is None:
        return []

    print(f"  📊 Bhav: {len(bhav_eq):,} EQ rows | {bhav_eq.index.nunique():,} unique symbols")

    results   = []
    ind_files = sorted(glob.glob(os.path.join(date_folder, "*.csv")))
    print(f"  📂 Files: {len(ind_files)}")

    for ind_file in ind_files:
        fname = os.path.basename(ind_file)
        try:
            df = pd.read_csv(ind_file, low_memory=False)
        except Exception as e:
            print(f"  ⚠️ Cannot read {fname}: {e}")
            continue
        df.columns = df.columns.str.strip()

        matched = skipped = 0
        match_types = {}
        conf_scores = []

        for _, row in df.iterrows():
            parsed = parse_row(row, fname)

            # Skip rows with no identifying info at all
            if (not parsed["nsCode"] and not parsed["short_name"] and
                    not parsed["scid"] and not parsed["stkname"] and
                    (parsed["ltp"] is None or parsed["ltp"] <= 0)):
                skipped += 1
                continue

            best, mtype, ltp_pct = match_row(parsed, bhav_eq, name_map)
            if best is None:
                skipped += 1
                continue

            match_types[mtype] = match_types.get(mtype, 0) + 1
            confidence = compute_confidence(mtype, ltp_pct)
            conf_scores.append(confidence)

            # Indicator values (up to 3)
            inds = parsed["indicators"]
            ind1_n = inds[0][0] if len(inds) > 0 else ""
            ind1_v = inds[0][1] if len(inds) > 0 else ""
            ind2_n = inds[1][0] if len(inds) > 1 else ""
            ind2_v = inds[1][1] if len(inds) > 1 else ""
            ind3_n = inds[2][0] if len(inds) > 2 else ""
            ind3_v = inds[2][1] if len(inds) > 2 else ""

            trend = detect_trend(fname, parsed["signal"])

            results.append({
                "Date":          trade_date,
                "Trend":         trend,
                "Signal":        parsed["signal"],
                "Comment":       parsed["comment"],
                "ScannerName":   _clean(row.get("scannerName", row.get("ScannerName", ""))),
                "ScannerCode":   _clean(row.get("scannerCode", row.get("scanId", ""))),
                "Category":      _clean(row.get("catName", row.get("Category", ""))),
                # MC identifiers (for audit)
                "MC_StkName":    parsed["stkname"],
                "LTP_MC":        parsed["ltp"],
                # NSE confirmed data
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
                # Indicator values (from scanner columns)
                "Ind1_Name":     ind1_n,
                "Ind1_Value":    ind1_v,
                "Ind2_Name":     ind2_n,
                "Ind2_Value":    ind2_v,
                "Ind3_Name":     ind3_n,
                "Ind3_Value":    ind3_v,
                # Quality columns
                "IndicatorFile": fname,
                "MatchType":     mtype,
                "ConfidenceScore": confidence,
                "LTP_Close_Pct": f"{ltp_pct:.1f}%" if ltp_pct is not None else "",
            })
            matched += 1

        avg_conf = round(sum(conf_scores) / len(conf_scores)) if conf_scores else 0
        mt_str = " | ".join(f"{k}:{v}" for k, v in sorted(match_types.items()))
        print(f"  ✅ {fname}: {matched} matched | {skipped} skipped | AvgConf:{avg_conf}  [{mt_str}]")

    return results

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST)
    print(f"🕐 NSE Engine v4 started: {now_ist.strftime('%d-%b-%Y %H:%M IST')}")

    print("\n📥 Loading NSE EQUITY_L name map...")
    name_map, isin_map = load_equity_l()

    date_folders = sorted(glob.glob(os.path.join(INDICATOR_ROOT, "*")), reverse=True)
    if not date_folders:
        msg = f"❌ NSE Engine — No indicator folders in `{INDICATOR_ROOT}/`"
        print(msg); tg_message(msg)
        return

    all_results = []
    for folder in date_folders:
        if not os.path.isdir(folder):
            continue
        all_results.extend(process_date(folder, name_map))

    if not all_results:
        msg = f"⚠️ NSE Engine — {now_ist.strftime('%d-%b-%Y')}\nNo matches found."
        print(msg); tg_message(msg)
        return

    df = pd.DataFrame(all_results)

    # Sort: high confidence first, then by trend and symbol
    df.sort_values(["ConfidenceScore", "Trend", "NSE_SYMBOL"],
                   ascending=[False, True, True], inplace=True)
    df.reset_index(drop=True, inplace=True)

    # ── Match quality report ──
    print("\n📊 Match Type Distribution:")
    for mt, cnt in df["MatchType"].value_counts().items():
        flag = "  ⚠️ REVIEW" if "PriceFallback" in str(mt) or "WARN" in str(mt) else ""
        print(f"  {mt}: {cnt:,}{flag}")

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

    print(f"\n✅ Total: {total:,} | Files: {files_done} | Symbols: {symbols:,}")
    print(f"   🟢 High confidence (≥80): {accurate:,} ({accurate/total*100:.1f}%)")
    print(f"   🟡 Moderate (60-79):       {moderate:,} ({moderate/total*100:.1f}%)")
    print(f"   🔴 Low (<60):              {low_conf:,} ({low_conf/total*100:.1f}%)")
    print(f"   Avg ConfidenceScore: {avg_conf}")
    print(f"   Bullish: {bullish_n:,} | Bearish: {bearish_n:,}")

    # ── Save ──
    latest_date = df["Date"].max()
    fname       = f"NSE_Indicator_Report_{latest_date.replace(' ', '_')}.csv"
    out_path_r  = os.path.join(OUTPUT_ROOT, fname)
    tmp_path    = f"/tmp/{fname}"
    df.to_csv(out_path_r, index=False)
    df.to_csv(tmp_path,   index=False)
    print(f"💾 Saved: {out_path_r}")

    # ── Telegram summary ──
    # Top signals table
    buy_signals  = df[df["Signal"].str.upper().str.contains("BUY|BULLISH TREND|GAINER|52WEEKHIGH", na=False)]
    sell_signals = df[df["Signal"].str.upper().str.contains("SELL|BEARISH TREND|LOSER|52WEEKLOW", na=False)]

    # Top high-confidence stocks
    top_bull = (df[(df["Trend"]=="Bullish") & (df["ConfidenceScore"]>=80)]
                ["NSE_SYMBOL"].value_counts().head(5).index.tolist())
    top_bear = (df[(df["Trend"]=="Bearish") & (df["ConfidenceScore"]>=80)]
                ["NSE_SYMBOL"].value_counts().head(5).index.tolist())

    caption = (
        f"📊 *NSE Indicator Engine v4*\n"
        f"📅 *{latest_date}* | Files: {files_done}\n"
        f"📈 Total: *{total:,}* | Symbols: *{symbols:,}*\n"
        f"✅ High Conf (≥80): *{accurate:,}* ({accurate/total*100:.0f}%) | Avg: {avg_conf}\n"
        f"🟢 Bullish: {bullish_n:,} | 🔴 Bearish: {bearish_n:,}\n"
        f"🟢 Top Bull: {', '.join(top_bull) if top_bull else 'None'}\n"
        f"🔴 Top Bear: {', '.join(top_bear) if top_bear else 'None'}\n"
        f"🕐 {now_ist.strftime('%H:%M IST')}"
    )[:1024]

    tg_file(tmp_path, caption)
    print("🏁 NSE Engine v4 complete.")


if __name__ == "__main__":
    main()
