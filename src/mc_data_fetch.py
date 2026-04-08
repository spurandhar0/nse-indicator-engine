"""
MoneyControl Data Fetcher → GitHub + Telegram
==============================================
Runs automatically via GitHub Actions every weekday at 7 PM IST.
Tokens are stored as GitHub Secrets — never hardcoded here.

HOW TO UPDATE Auth-Token (do this whenever Bullish/Bearish/Scanners stop working):
  1. Open Chrome → go to https://www.moneycontrol.com/pro → Log in
  2. Press F12 → Network tab → Filter: "mcapi"
  3. Click any stock or scanner on the page
  4. In the request headers, copy the value of "Auth-Token"
  5. Go to your GitHub repo → Settings → Secrets → Update MC_AUTH_TOKEN

OUTPUT: All 30 CSV files are saved to indicator_data/DD-Mon-YYYY/ so they are
        committed to the repo and processed by nse_engine.py.
        A zip copy is also sent to Telegram for quick review.

FILES (30 total):
  Core (9):   52wk, chart_patterns_active, chart_patterns_inactive,
              technical_picks_active, technical_picks_inactive,
              stock_ideas, analysts_choice, bullish, bearish
  Scanners (18): candlestick_bull/bear, moving_avg_bull/bear,
                 volume_delivery_bull/bear, supertrend_bull/bear,
                 rsi, stochastic, adx, mfi, macd, adaptive_rsi,
                 range_breakout_bull/bear, bullish/bearish_breakout
  New (3):    gainers, losers, most_active
"""

import os, zipfile, requests, time, threading
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt, timedelta, timezone

# ======================= CONFIGURATION =======================
token     = os.environ['MC_AUTH_TOKEN']
bot_token = os.environ['TG_BOT_TOKEN']
chat_id   = os.environ['TG_CHAT_ID']
# =============================================================

baseurl = 'https://api.moneycontrol.com/mcapi'
mc_headers = {
    'Auth-Token': token,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'
}

IST = timezone(timedelta(hours=5, minutes=30))
NOW_IST = dt.now(tz=IST)
RUN_TIMESTAMP = NOW_IST.strftime('%Y-%m-%d_%H%M')

IST_DATE_FOLDER = NOW_IST.strftime('%d-%b-%Y')   # e.g., "08-Apr-2026"
OUTPUT_DIR = f'indicator_data/{IST_DATE_FOLDER}'
os.makedirs(OUTPUT_DIR, exist_ok=True)

TMP_ZIP_DIR = '/tmp'
SUMMARY = {}

# ======================== HELPERS ============================

def getISTtime():
    return dt.now(timezone.utc).astimezone(IST).replace(tzinfo=None)

def out_path(filename):
    return os.path.join(OUTPUT_DIR, filename)

def TGsendDocument(filepath, caption=''):
    try:
        with open(filepath, "rb") as f:
            url = f'https://api.telegram.org/bot{bot_token}/sendDocument'
            r = requests.post(url, data={"chat_id": chat_id, "caption": caption},
                              files={"document": f}, timeout=120)
            if r.json().get('ok'):
                print(f'  ✅ Sent to Telegram: {os.path.basename(filepath)}')
            else:
                print(f'  ⚠ Telegram error: {r.json()}')
    except Exception as e:
        print(f'  ❌ TGsendDocument error: {e}')

def TGsendMessage(text):
    try:
        url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
        requests.post(url, data={'chat_id': chat_id, 'text': text}, timeout=15)
    except Exception as e:
        print(f'TGsendMessage error: {e}')

def check_auth_token():
    url = baseurl + '/v1/technical-trends/uptrend/bullish'
    param = {'ex': 'N', 'deviceType': 'W', 'sort': 'performance',
             'appVersion': '142', 'index': 9, 'page': 1, 'order': 'desc'}
    try:
        r = requests.get(url=url, headers=mc_headers, params=param, timeout=10)
        if r.status_code == 401:
            msg = (
                "⚠️ MoneyControl Auth-Token EXPIRED\n\n"
                "To fix:\n"
                "1. Go to moneycontrol.com/pro and log in\n"
                "2. Press F12 → Network tab → Filter: mcapi\n"
                "3. Click any scanner/stock\n"
                "4. Copy 'Auth-Token' from request headers\n"
                "5. GitHub repo → Settings → Secrets → Update MC_AUTH_TOKEN\n\n"
                "⚙️ Bullish/Bearish trends and Scanners SKIPPED this run."
            )
            print(msg)
            TGsendMessage(msg)
            return False
    except Exception as e:
        print(f'Auth check error: {e}')
    return True

# =================== BULLISH / BEARISH =======================

index_mapping = {
    9: 'NIFTY 50', 23: 'NIFTY BANK', 27: 'NIFTY MIDCAP100', 6: 'NIFTY NEXT 50',
    100: 'NIFTY 100', 49: 'NIFTY 200', 7: 'NIFTY 500', 53: 'NIFTY SMALLCAP100',
    31: 'NIFTY MIDCAP50', -2: 'ALL NSE', 52: 'NIFTY AUTO', 19: 'NIFTY IT',
    43: 'NIFTY PSUBANK', 47: 'NIFTY FINSERVICE', 41: 'NIFTY PHARMA', 39: 'NIFTY FMCG',
    51: 'NIFTY METAL', 34: 'NIFTY REALTY', 50: 'NIFTY MEDIA', 38: 'NIFTY ENERGY',
    79: 'NIFTY PVTBANK', 35: 'NIFTY INFRA', 48: 'NIFTY COMMODITIES',
    56: 'NIFTY CONSUMPTION', 42: 'NIFTY PSE', 44: 'NIFTY SERVICE SECTOR',
    118: 'NIFTY FINSERV 25/50', 122: 'NIFTY CONSUMER DURABLE', 123: 'NIFTY HEALTHCARE',
    126: 'NIFTY OILGAS', 133: 'NIFTY INDIA MFG', 111: 'NIFTY MIDCAP 150',
    112: 'NIFTY MIDSML 400', 114: 'NIFTY SMLCAP 250', 40: 'NIFTY MNC',
    119: 'NIFTY AlphaLowVol 30', 120: 'NIFTY200 Momentum30', 124: 'NIFTY LargeMid250',
    125: 'NIFTY500 Mul50:25:25', 61: 'NIFTY CPSE', 128: 'NIFTY MID SELECT',
    130: 'NIFTY IND DIGITAL', 132: 'NIFTY M150 QLTY50', 135: 'NIFTY Microcap250',
    136: 'NIFTY TOTAL MKT', 'FNO': 'FNO', 'LCAP': 'LARGECAP',
    'MDCAP': 'MIDCAP', 'SMCAP': 'SMALLCAP'
}

list_index = [
    9, 23, 27, 6, 100, 49, 7, 53, 31, -2, 52, 19, 43, 47, 41, 39, 51, 34, 50, 38,
    79, 35, 48, 56, 42, 44, 118, 122, 123, 126, 133, 111, 112, 114, 40, 119, 120,
    124, 125, 61, 128, 130, 132, 135, 136, 'FNO', 'LCAP', 'MDCAP', 'SMCAP'
]

def getMCProdata(index, trend):
    indexname = index_mapping.get(index, str(index))
    if trend == 'bullish':
        url = baseurl + '/v1/technical-trends/uptrend/bullish'
        order = 'desc'
    else:
        url = baseurl + '/v1/technical-trends/downtrend/bearish'
        order = 'asc'

    page, all_data = 1, []
    while True:
        param = {'ex': 'N', 'deviceType': 'W', 'sort': 'performance',
                 'appVersion': '142', 'index': index, 'page': page, 'order': order}
        try:
            resp = requests.get(url=url, headers=mc_headers, params=param, timeout=15)
        except Exception as e:
            print(f'  Request error ({indexname}): {e}')
            break
        if resp.status_code == 401:
            return pd.DataFrame()
        if resp.status_code != 200:
            break
        data = resp.json().get('data', {}).get('list', [])
        if not data:
            break
        all_data.extend(data)
        page += 1

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    # Keep scId, stkId, currPrice — all useful for symbol resolution & LTP validation
    df.drop(columns=['prevTrend', 'url', 'analysisUrl'], inplace=True, errors='ignore')
    df.insert(0, 'Index', indexname)
    return df

def run_trend_fetch(trend_label, filename):
    print(f'\n{getISTtime()} | Fetching {trend_label} data...')
    start = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(getMCProdata, idx, trend_label.lower()) for idx in list_index]
        for f in futures:
            df = f.result()
            if not df.empty:
                results.append(df)

    if not results:
        print(f'  ⚠ No {trend_label} data.')
        SUMMARY[filename] = 'SKIPPED'
        return

    combined = pd.concat(results, ignore_index=True)
    combined = combined.drop_duplicates(subset=['StockName'])
    combined.to_csv(out_path(filename), index=False)
    SUMMARY[filename] = len(combined)
    print(f'  ✅ {trend_label}: {len(combined)} stocks → {filename} ({time.time()-start:.1f}s)')

# ===================== 52-WEEK HIGH/LOW ======================

def get52wkdata(category, result):
    ctg_name = '52High' if category == '52WeekHigh' else '52Low'
    url = baseurl + '/v1/marketstats/market-movers'
    page, all_data = 1, []
    while True:
        try:
            resp = requests.get(url=url, headers=mc_headers,
                                params={'ex': 'N', 'type': ctg_name, 'page': page}, timeout=15)
        except Exception as e:
            print(f'  52wk error: {e}')
            break
        if resp.status_code != 200:
            break
        data = resp.json().get('data', {}).get('list', {}).get('data', [])
        if not data:
            break
        all_data.extend(data)
        page += 1

    if not all_data:
        result[category] = pd.DataFrame()
        return

    rows = []
    for row in all_data:
        if isinstance(row, list):
            d = {}
            if len(row) > 0:  d['MC_Code']        = row[0]
            if len(row) > 1:  d['BSE_Code']        = row[1]
            if len(row) > 2:  d['StockName']       = row[2]
            if len(row) > 3:  d['nsCode']          = row[3] if row[3] else ''
            if len(row) > 4:  d['currPrice']       = row[4]
            if len(row) > 30: d['lastUpdatedTime'] = row[30]
            rows.append(d)
        elif isinstance(row, dict):
            rows.append(row)

    df = pd.DataFrame(rows)
    df.insert(0, 'Category', category)
    result[category] = df

def run_52wk():
    print(f'\n{getISTtime()} | Fetching 52-Week High/Low data...')
    result = {}
    threads = []
    for cat in ['52WeekHigh', '52WeekLow']:
        t = threading.Thread(target=get52wkdata, args=(cat, result))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    frames = [v for v in result.values() if not v.empty]
    if frames:
        df = pd.concat(frames, ignore_index=True)
        df.to_csv(out_path('52wk.csv'), index=False)
        SUMMARY['52wk.csv'] = len(df)
        print(f'  ✅ 52wk: {len(df)} stocks → 52wk.csv')
    else:
        SUMMARY['52wk.csv'] = 'NO DATA'
        print('  ⚠ No 52wk data.')

# ============ NEW: GAINERS / LOSERS / MOST ACTIVE ============

def _fetch_market_movers_type(mtype, label):
    """
    Fetch market movers of a specific type (Gainer, Loser, VolumeShockers).
    Returns DataFrame with nsCode, StockName, currPrice, pChange, etc.
    """
    url = baseurl + '/v1/marketstats/market-movers'
    page, all_data = 1, []
    while True:
        try:
            resp = requests.get(url=url, headers=mc_headers,
                                params={'ex': 'N', 'type': mtype, 'page': page}, timeout=15)
        except Exception as e:
            print(f'  {label} error: {e}')
            break
        if resp.status_code != 200:
            print(f'  {label} HTTP {resp.status_code} for type={mtype}')
            break
        data_section = resp.json().get('data', {})
        # Try both possible response structures
        data = (data_section.get('list', {}).get('data', []) or
                data_section.get('list', []) or
                data_section.get('data', []))
        if not data:
            break
        all_data.extend(data)
        page += 1
        if len(data) < 20:   # last page
            break

    if not all_data:
        return pd.DataFrame()

    rows = []
    for row in all_data:
        if isinstance(row, list):
            d = {}
            if len(row) > 0:  d['MC_Code']   = row[0]
            if len(row) > 1:  d['BSE_Code']  = row[1]
            if len(row) > 2:  d['StockName'] = row[2]
            if len(row) > 3:  d['nsCode']    = row[3] if row[3] else ''
            if len(row) > 4:  d['currPrice'] = row[4]
            if len(row) > 5:  d['pChange']   = row[5]
            rows.append(d)
        elif isinstance(row, dict):
            rows.append(row)

    return pd.DataFrame(rows)

def run_gainers_losers_active():
    """Fetch Top Gainers, Top Losers, and Most Active stocks."""
    print(f'\n{getISTtime()} | Fetching Gainers / Losers / Most Active...')

    movers_config = [
        ('Gainer',         'gainers.csv',     'Gainers'),
        ('Loser',          'losers.csv',       'Losers'),
        ('VolumeShockers', 'most_active.csv',  'Most Active'),
    ]

    for mtype, fname, label in movers_config:
        df = _fetch_market_movers_type(mtype, label)
        if not df.empty:
            df.to_csv(out_path(fname), index=False)
            SUMMARY[fname] = len(df)
            print(f'  ✅ {label}: {len(df)} stocks → {fname}')
        else:
            # Fallback: try alternative type names
            alt_types = {
                'Gainer': ['TopGainer', 'gainer', 'Gainers'],
                'Loser': ['TopLoser', 'loser', 'Losers'],
                'VolumeShockers': ['MostActive', 'Volume', 'ActiveByValue']
            }
            found = False
            for alt in alt_types.get(mtype, []):
                df2 = _fetch_market_movers_type(alt, label)
                if not df2.empty:
                    df2.to_csv(out_path(fname), index=False)
                    SUMMARY[fname] = len(df2)
                    print(f'  ✅ {label} (via {alt}): {len(df2)} stocks → {fname}')
                    found = True
                    break
            if not found:
                SUMMARY[fname] = 'NO DATA'
                print(f'  ⚠ No {label} data — all type variations tried.')

# =================== CHART PATTERNS ==========================

def _fetch_chart_patterns(pattern_type, limit=48):
    url = baseurl + '/technicalpicks/chart-patterns'
    start, all_data = 0, []
    while True:
        param = {'deviceType': 'W', 'version': 174, 'start': start,
                 'limit': limit, 'pattern_type': pattern_type}
        try:
            resp = requests.get(url=url, headers=mc_headers, params=param, timeout=15)
        except Exception as e:
            print(f'  Chart patterns error: {e}')
            break
        if resp.status_code != 200:
            break
        data = resp.json().get('list', {}).get('data', [])
        if not data:
            break
        all_data.extend(data)
        start += limit
    return pd.DataFrame(all_data)

def run_chart_patterns():
    print(f'\n{getISTtime()} | Fetching Chart Patterns...')
    for ptype in ['active', 'inactive']:
        df = _fetch_chart_patterns(ptype)
        fname = f'chart_patterns_{ptype}.csv'
        if not df.empty:
            df.to_csv(out_path(fname), index=False)
            SUMMARY[fname] = len(df)
            print(f'  ✅ Chart Patterns ({ptype}): {len(df)} rows → {fname}')
        else:
            SUMMARY[fname] = 'NO DATA'
            print(f'  ⚠ No chart patterns ({ptype}) data.')

# =================== TECHNICAL PICKS =========================

def _fetch_tech_picks_batch(url, reco_type, start, limit):
    params = {'deviceType': 'I', 'version': 150,
              'recommendation_type': reco_type, 'start': start, 'limit': limit}
    resp = requests.get(url, headers=mc_headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get('list', {}).get('data', [])

def run_technical_picks():
    print(f'\n{getISTtime()} | Fetching Technical Picks...')
    url = baseurl + '/technicalpicks/recommendations'
    for reco_type, count_key in [('active', 'activeRecoCount'), ('inactive', 'inactiveRecoCount')]:
        try:
            r = requests.get(url, headers=mc_headers,
                             params={'deviceType': 'I', 'version': 150,
                                     'recommendation_type': reco_type, 'start': 0, 'limit': 1}, timeout=15)
            total = r.json().get('list', {}).get(count_key, 0)
            limit = 12
            starts = list(range(0, total + limit, limit))
            all_data = []
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(_fetch_tech_picks_batch, url, reco_type, s, limit): s for s in starts}
                for future in as_completed(futures):
                    try:
                        all_data.extend(future.result())
                    except Exception as e:
                        print(f'  Batch error: {e}')
            df = pd.DataFrame(all_data)
            fname = f'technical_picks_{reco_type}.csv'
            df.to_csv(out_path(fname), index=False)
            SUMMARY[fname] = len(df)
            print(f'  ✅ Technical Picks ({reco_type}): {len(df)} rows → {fname}')
        except Exception as e:
            SUMMARY[f'technical_picks_{reco_type}.csv'] = 'ERROR'
            print(f'  ❌ Technical Picks ({reco_type}) error: {e}')

# =================== STOCK IDEAS =============================

def _fetch_stock_ideas_batch(url, start, limit):
    params = {'deviceType': 'W', 'start': start, 'limit': limit}
    try:
        resp = requests.get(url=url, headers=mc_headers, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        return resp.json().get('data', [])
    except Exception as e:
        print(f'  Stock ideas batch error: {e}')
        return []

def run_stock_ideas():
    print(f'\n{getISTtime()} | Fetching Stock Ideas...')
    url = baseurl + '/v1/broker-research/stock-ideas'
    limit = 50
    all_data = []
    first = _fetch_stock_ideas_batch(url, 0, limit)
    if not first:
        SUMMARY['stock_ideas.csv'] = 'NO DATA'
        print('  ⚠ No stock ideas data.')
        return
    all_data.extend(first)
    args_list = [(url, s, limit) for s in range(limit, 12000, limit)]
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_fetch_stock_ideas_batch, *args): args[1] for args in args_list}
        for future in as_completed(futures):
            result = future.result()
            if result:
                all_data.extend(result)
    df = pd.DataFrame(all_data)
    df.to_csv(out_path('stock_ideas.csv'), index=False)
    SUMMARY['stock_ideas.csv'] = len(df)
    print(f'  ✅ Stock Ideas: {len(df)} rows → stock_ideas.csv')

# =================== ANALYSTS CHOICE =========================

def run_analysts_choice():
    print(f'\n{getISTtime()} | Fetching Analysts Choice...')
    url = baseurl + '/v1/broker-research/get-analysts-choice'
    start, limit, all_data = 0, 960, []
    while True:
        try:
            resp = requests.get(url=url, headers=mc_headers,
                                params={'deviceType': 'W', 'start': start, 'limit': limit}, timeout=15)
        except Exception as e:
            print(f'  Analysts choice error: {e}')
            break
        if resp.status_code != 200:
            break
        data = resp.json().get('data', [])
        if not data:
            break
        all_data.extend(data)
        start += limit
    if all_data:
        df = pd.DataFrame(all_data)
        df.to_csv(out_path('analysts_choice.csv'), index=False)
        SUMMARY['analysts_choice.csv'] = len(df)
        print(f'  ✅ Analysts Choice: {len(df)} rows → analysts_choice.csv')
    else:
        SUMMARY['analysts_choice.csv'] = 'NO DATA'
        print('  ⚠ No analysts choice data.')

# =================== STOCK SCANNER ==========================

SCAN_GROUPS = {
    "candlestick_bullish":  {"cat_id": 20, "ids": ["OHLC_D_P_3UNWINBULL","OHLC_W_P_3UNWINBULL","OHLC_M_P_3UNWINBULL","OHLC_D_P_BPBULL","OHLC_W_P_BPBULL","OHLC_M_P_BPBULL","OHLC_D_P_ENGBULL","OHLC_W_P_ENGBULL","OHLC_M_P_ENGBULL","OHLC_D_P_HARBULL","OHLC_W_P_HARBULL","OHLC_M_P_HARBULL","OHLC_D_P_HAM","OHLC_W_P_HAM","OHLC_M_P_HAM","OHLC_D_P_SHOOT","OHLC_W_P_SHOOT","OHLC_M_P_SHOOT","OHLC_D_P_SANDBULL","OHLC_W_P_SANDBULL","OHLC_M_P_SANDBULL","OHLC_D_P_IBAR","OHLC_W_P_IBAR","OHLC_M_P_IBAR","OHLC_D_P_3MORNSTAR","OHLC_W_P_3MORNSTAR","OHLC_M_P_3MORNSTAR","OHLC_D_P_PIERCING","OHLC_W_P_PIERCING","OHLC_M_P_PIERCING","OHLC_D_P_KICKBULL","OHLC_W_P_KICKBULL","OHLC_M_P_KICKBULL","OHLC_D_P_TASBULL","OHLC_W_P_TASBULL","OHLC_M_P_TASBULL","OHLC_D_P_BUTTERDOJI","OHLC_W_P_BUTTERDOJI","OHLC_M_P_BUTTERDOJI","OHLC_D_P_DOJI","OHLC_W_P_DOJI","OHLC_M_P_DOJI","OHLC_D_I_RISE3BULL","OHLC_W_I_RISE3BULL","OHLC_M_I_RISE3BULL","OHLC_D_P_DJSBULL","OHLC_W_P_DJSBULL","OHLC_M_P_DJSBULL","OHLC_D_P_MULTIINCAND","OHLC_W_P_MULTIINCAND","OHLC_M_P_MULTIINCAND","OHLC_D_P_LLDOJI","OHLC_W_P_LLDOJI","OHLC_M_P_LLDOJI"]},
    "candlestick_bearish":  {"cat_id": 20, "ids": ["OHLC_D_P_3UNWINBEAR","OHLC_W_P_3UNWINBEAR","OHLC_M_P_3UNWINBEAR","OHLC_D_P_BPBEAR","OHLC_W_P_BPBEAR","OHLC_M_P_BPBEAR","OHLC_D_P_ENGBEAR","OHLC_W_P_ENGBEAR","OHLC_M_P_ENGBEAR","OHLC_D_P_HARBEAR","OHLC_W_P_HARBEAR","OHLC_M_P_HARBEAR","OHLC_D_P_HANGMAN","OHLC_W_P_HANGMAN","OHLC_M_P_HANGMAN","OHLC_D_P_SSTAR","OHLC_W_P_SSTAR","OHLC_M_P_SSTAR","OHLC_D_P_3EVESTAR","OHLC_W_P_3EVESTAR","OHLC_M_P_3EVESTAR","OHLC_D_P_DARKCC","OHLC_W_P_DARKCC","OHLC_M_P_DARKCC","OHLC_D_P_KICKBEAR","OHLC_W_P_KICKBEAR","OHLC_M_P_KICKBEAR","OHLC_D_P_TBCBEAR","OHLC_W_P_TBCBEAR","OHLC_M_P_TBCBEAR","OHLC_D_P_GRAVEDOJI","OHLC_W_P_GRAVEDOJI","OHLC_M_P_GRAVEDOJI","OHLC_D_P_OUTBARBULL2","OHLC_W_P_OUTBARBULL2","OHLC_M_P_OUTBARBULL2","OHLC_D_P_DJSBEAR","OHLC_W_P_DJSBEAR","OHLC_M_P_DJSBEAR","OHLC_D_P_OUTBARBULL","OHLC_W_P_OUTBARBULL","OHLC_M_P_OUTBARBULL","OHLC_D_P_MULTIINBAR","OHLC_W_P_MULTIINBAR","OHLC_M_P_MULTIINBAR"]},
    "moving_average_bullish": {"cat_id": 26, "ids": ["OHLC_D_I_50200GOLD","OHLC_D_I_ABV5DMA","OHLC_D_I_20DMABULL","OHLC_D_I_50DMABULL","OHLC_D_I_100DMABULL","OHLC_D_I_200DMABULL","OHLC_D_I_513BULLCO","OHLC_D_I_821BULLCO","OHLC_D_I_2050MABULL","OHLC_D_I_GUPPYBULL"]},
    "moving_average_bearish": {"cat_id": 26, "ids": ["OHLC_D_I_50200DEATH","OHLC_D_I_BLW5DMA","OHLC_D_I_20DMABEAR","OHLC_D_I_50DMABEAR","OHLC_D_I_100DMABEAR","OHLC_D_I_200DMABEAR","OHLC_D_I_513BEARCO","OHLC_D_I_821BEARCO","OHLC_D_I_2050MABEAR","OHLC_D_I_GUPPYBEAR"]},
    "volume_delivery_bullish": {"cat_id": 27, "ids": ["OHLC_D_I_20DPVOLBULL","OHLC_D_I_20DELVBULL","OHLC_D_I_10DELVBULL","OHLC_D_I_UNUSVBULL"]},
    "volume_delivery_bearish": {"cat_id": 27, "ids": ["OHLC_D_I_20DPVOLBEAR","OHLC_D_I_10DELVBEAR","OHLC_D_I_UNUSVBEAR"]},
    "supertrend_bullish":     {"cat_id": 28, "ids": ["OHLC_D_I_STBULLC","OHLC_D_I_PSARBULL"]},
    "supertrend_bearish":     {"cat_id": 28, "ids": ["OHLC_D_I_STBEARC","OHLC_D_I_PSARBear"]},
    "rsi":                    {"cat_id": 15, "subcat_id": 10, "ids": ["OHLC_D_I_RSI70FABV","OHLC_D_I_RSI30FBELOW","OHLC_D_I_RSI70FBELOW","OHLC_D_I_RSI30FABV","OHLC_D_I_RSIABVAVE","OHLC_D_I_RSIBELAVE","OHLC_W_I_RSI70FABV","OHLC_W_I_RSI30FBELOW","OHLC_W_I_RSI70FBELOW","OHLC_W_I_RSI30FABV","OHLC_W_I_RSIABVAVE","OHLC_W_I_RSIBELAVE","OHLC_M_I_RSI70FABV","OHLC_M_I_RSI30FBELOW","OHLC_M_I_RSI70FBELOW","OHLC_M_I_RSIABVAVE","OHLC_M_I_RSIBELAVE"]},
    "stochastic":             {"cat_id": 15, "subcat_id": 11, "ids": ["OHLC_D_I_STOCH70BEAR","OHLC_D_I_STOCH30BULL","OHLC_D_I_STOCH70REVBULL","OHLC_D_I_STOCH70REVBEAR","OHLC_D_I_STOCHSETUPBULL","OHLC_D_I_STOCHSETUPBEAR","OHLC_W_I_STOCH70BEAR","OHLC_W_I_STOCH30BULL","OHLC_W_I_STOCH70REVBULL","OHLC_W_I_STOCH70REVBEAR","OHLC_W_I_STOCHSETUPBULL","OHLC_W_I_STOCHSETUPBEAR","OHLC_M_I_STOCH70BEAR","OHLC_M_I_STOCH30BULL","OHLC_M_I_STOCH70REVBULL","OHLC_M_I_STOCH70REVBEAR","OHLC_M_I_STOCHSETUPBULL","OHLC_M_I_STOCHSETUPBEAR"]},
    "adx":                    {"cat_id": 15, "subcat_id": 12, "ids": ["OHLC_D_I_STRONGADXBULL","OHLC_D_I_STRONGADXBEAR","OHLC_D_I_ADXBULLDMI","OHLC_D_I_ADXBEARDMI","OHLC_D_I_ADX24STR","OHLC_D_I_ADX24EXH","OHLC_W_I_STRONGADXBEAR","OHLC_W_I_ADXBULLDMI","OHLC_W_I_ADXBEARDMI","OHLC_M_I_STRONGADXBULL","OHLC_M_I_STRONGADXBEAR","OHLC_M_I_ADXBULLDMI","OHLC_M_I_ADXBEARDMI"]},
    "mfi":                    {"cat_id": 15, "subcat_id": 14, "ids": ["OHLC_D_I_MFI70FBELOW","OHLC_D_I_MFI30FABV","OHLC_D_I_MFI70FABV","OHLC_D_I_MFI30FBELOW","OHLC_D_I_MFIABVAVE","OHLC_D_I_MFIBELAVE","OHLC_D_I_MFI50COBULL","OHLC_D_I_MFI50COBEAR","OHLC_W_I_MFI70FBELOW","OHLC_W_I_MFI30FABV","OHLC_W_I_MFI70FABV","OHLC_W_I_MFI30FBELOW","OHLC_W_I_MFIABVAVE","OHLC_W_I_MFI50COBULL","OHLC_W_I_MFIBELAVE","OHLC_W_I_MFI50COBEAR","OHLC_M_I_MFI70FBELOW","OHLC_M_I_MFI30FABV","OHLC_M_I_MFI70FABV","OHLC_M_I_MFI30FBELOW","OHLC_M_I_MFIABVAVE","OHLC_M_I_MFI50COBULL","OHLC_M_I_MFIBELAVE","OHLC_M_I_MFI50COBEAR"]},
    "macd":                   {"cat_id": 15, "subcat_id": 15, "ids": ["OHLC_D_I_MACD0BULLCO","OHLC_D_I_MACD0BEARCO","OHLC_D_I_MACDREVBULL","OHLC_D_I_MACDREVBEAR"]},
    "adaptive_rsi":           {"cat_id": 15, "subcat_id": 16, "ids": ["OHLC_D_I_ADRBULLCO","OHLC_D_I_ADRBEARCO","OHLC_W_I_ADRBULLCO","OHLC_W_I_ADRBEARCO","OHLC_M_I_ADRBULLCO","OHLC_M_I_ADRBEARCO"]},
    "range_breakout_bullish": {"cat_id": 18, "ids": ["OHLC_D_I_NR4BULLBO","OHLC_D_I_NR7BULLBO","OHLC_D_I_ABVBOLUUPRBAND"]},
    "range_breakout_bearish": {"cat_id": 18, "ids": ["OHLC_D_I_NR4BEARBO","OHLC_D_I_NR7BEARBO","OHLC_D_I_BELOWBOLLOWEBAND"]},
    "bullish_breakout":       {"cat_id": 25, "ids": ["OHLC_D_P_BPBULL","OHLC_D_I_DSMARTBULLC","OHLC_D_I_RSIPOWBO","OHLC_D_I_RSI70607DNBU","OHLC_D_I_ADBBPBUY","OHLC_D_I_MOMRAVBU","OHLC_D_I_ST5133BULL","OHLC_D_I_SQZBULLBO","OHLC_D_I_10DSTOCHBULL","OHLC_20D_P_CLABVPWH","OHLC_W_I_RSIMULTIBAG","OHLC_D_I_BOLDBULL","OHLC_D_I_BTSTOND","OHLC_D_I_CLSERIESBULL","OHLC_D_I_TRNGLCANDBULL","OHLC_D_I_RISE3BULL"]},
    "bearish_breakout":       {"cat_id": 25, "ids": ["OHLC_D_P_BPBEAR","OHLC_D_I_DSMARTBEARC","OHLC_D_I_RSIPOWBD","OHLC_D_I_RSI70607DNBE","OHLC_D_I_ADBBPSELL","OHLC_D_I_MOMRAVBE","OHLC_D_I_ST5133BEAR","OHLC_D_I_SQZBEARBO","OHLC_D_I_10DSTOCHBEAR","OHLC_20D_P_CLBLWPWL","OHLC_D_I_BOLDBEAR","OHLC_D_I_STBTOND","OHLC_D_I_CLSERIESBEAR","OHLC_D_I_TRNGLCANDBEAR","OHLC_D_I_RISE3BEAR"]},
}

def _fetch_scanner_id(scan_id, cat_id, subcat_id=None):
    url = baseurl + '/v1/techscanner/scanner-detail'
    params = {'catId': str(cat_id), 'scanId': scan_id}
    if subcat_id:
        params['subcatId'] = str(subcat_id)
    try:
        resp = requests.get(url, headers=mc_headers, params=params, timeout=15)
        if resp.status_code == 401:
            return None
        resp.raise_for_status()
        data = resp.json().get('data', {}).get('list', [])
        df = pd.DataFrame(data)
        if not df.empty:
            df['scanId'] = scan_id
        return df
    except Exception as e:
        print(f'  Scanner error ({scan_id}): {e}')
        return pd.DataFrame()

def run_stock_scanners():
    print(f'\n{getISTtime()} | Fetching Stock Scanners...')
    token_ok = True
    for group_name, cfg in SCAN_GROUPS.items():
        if not token_ok:
            break
        cat_id = cfg['cat_id']
        subcat_id = cfg.get('subcat_id')
        scan_ids = cfg['ids']
        dfs = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_fetch_scanner_id, sid, cat_id, subcat_id): sid for sid in scan_ids}
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    print(f'  ❌ Scanner: Auth-Token expired. Skipping remaining scanners.')
                    token_ok = False
                    break
                if not result.empty:
                    dfs.append(result)
        if dfs:
            df = pd.concat(dfs, ignore_index=True)
            fname = f'{group_name}.csv'
            df.to_csv(out_path(fname), index=False)
            SUMMARY[fname] = len(df)
            print(f'  ✅ Scanner [{group_name}]: {len(df)} rows → {fname}')
        else:
            SUMMARY[f'{group_name}.csv'] = 'NO DATA'
            print(f'  ⚠ No data for scanner: {group_name}')

# =================== ZIP & SEND ==============================

def zip_and_send():
    csv_files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.csv')]
    if not csv_files:
        TGsendMessage('⚠️ MC Data Fetch: No CSV files were generated this run.')
        return

    zip_name = f'mc_data_{RUN_TIMESTAMP}.zip'
    zip_path = os.path.join(TMP_ZIP_DIR, zip_name)

    print(f'\n{getISTtime()} | Creating zip: {zip_name} ({len(csv_files)} files)...')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(csv_files):
            zf.write(out_path(fname), fname)

    zip_size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f'  ✅ Zip: {zip_path} ({zip_size_mb:.2f} MB)')

    ok_count  = sum(1 for v in SUMMARY.values() if isinstance(v, int))
    err_count = sum(1 for v in SUMMARY.values() if not isinstance(v, int))
    lines = [f'📦 MC Data — {RUN_TIMESTAMP}',
             f'Files: {len(csv_files)} ({ok_count} ✅ {err_count} ⚠️) | {zip_size_mb:.1f} MB', '']
    for fname, count in sorted(SUMMARY.items()):
        icon = '✅' if isinstance(count, int) else '⚠️'
        lines.append(f'{icon} {fname}: {count}')
    caption = '\n'.join(lines)

    TGsendDocument(zip_path, caption[:1024])

# ========================= MAIN ==============================

if __name__ == '__main__':
    print(f'\n{"="*60}')
    print(f'{getISTtime()} | MoneyControl Data Fetch Started')
    print(f'Output folder: {OUTPUT_DIR}')
    print(f'{"="*60}')

    overall_start = time.time()
    token_valid = check_auth_token()

    # Core data (no auth token needed for most)
    run_52wk()
    run_chart_patterns()
    run_technical_picks()
    run_stock_ideas()
    run_analysts_choice()

    # New: Gainers / Losers / Most Active
    run_gainers_losers_active()

    if token_valid:
        run_trend_fetch('Bullish', 'bullish.csv')
        run_trend_fetch('Bearish', 'bearish.csv')
        run_stock_scanners()
    else:
        print(f'\n⚠️  Skipping Bullish/Bearish and Scanners — Auth-Token expired.')

    zip_and_send()

    elapsed = time.time() - overall_start
    print(f'\n{"="*60}')
    print(f'{getISTtime()} | All done! Total time: {elapsed:.1f}s')
    print(f'{"="*60}')
