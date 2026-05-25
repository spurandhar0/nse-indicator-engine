"""
NSE Indicator Simulation Engine — v1
======================================
Reads signals from signal_data/signals.json
Simulates trades using bhavcopy daily prices.

RULES:
  BUY 1  : Signal date close price
  BUY 2  : Price drops 10% below avg_buy after BUY 1
  BUY 3  : Price drops 10% below avg_buy after BUY 2
  BUY 4  : Price drops 10% below avg_buy after BUY 3
  TARGET : Price >= avg_buy * 1.10  → PROFIT exit
  SL     : Price <= avg_buy * 0.75  → LOSS exit
  FE     : 40 market days elapsed   → FE exit

Uses CLOSE_PRICE for all checks (end-of-day).
"""

import os, json, glob, re
import pandas as pd
from datetime import datetime, date
from pathlib import Path

SIGNAL_FILE  = Path("signal_data/signals.json")
SIM_FILE     = Path("signal_data/sim_results.json")
BHAV_ROOT    = Path("bhav_data")
TARGET_PCT   = 1.10   # +10% target
SL_PCT       = 0.75   # -25% stop loss
BUY_DIP_PCT  = 0.90   # -10% for next buy (of avg_buy)
MAX_BUYS     = 4
MAX_DAYS     = 40     # force exit after 40 market days

# ── Load signals ─────────────────────────────────────────────────────────────
if not SIGNAL_FILE.exists():
    print("No signals file found. Run 06_generate_signals.py first.")
    exit(0)

with open(SIGNAL_FILE) as f:
    signals = json.load(f)

print(f"Signals loaded: {len(signals)}")

# ── Build price history from bhavcopy ────────────────────────────────────────
print("Building price history from bhavcopy...")
price_history = {}   # {date_str: {SYMBOL: {open, high, low, close}}}

for bhav_file in sorted(BHAV_ROOT.glob("**/sec_bhavdata_full_*.csv")):
    try:
        df = pd.read_csv(bhav_file, low_memory=False)
        df.columns = df.columns.str.strip()
        # Normalize column names
        col_map = {}
        for c in df.columns:
            cl = c.strip().upper()
            if cl == "SYMBOL":        col_map[c] = "SYMBOL"
            elif cl == "SERIES":      col_map[c] = "SERIES"
            elif cl in ("DATE1","TIMESTAMP","DATE"): col_map[c] = "DATE"
            elif cl in ("OPEN_PRICE","OPEN"):  col_map[c] = "OPEN"
            elif cl in ("HIGH_PRICE","HIGH"):  col_map[c] = "HIGH"
            elif cl in ("LOW_PRICE","LOW"):    col_map[c] = "LOW"
            elif cl in ("CLOSE_PRICE","CLOSE"): col_map[c] = "CLOSE"
        df = df.rename(columns=col_map)
        needed = {"SYMBOL","DATE","CLOSE"}
        if not needed.issubset(df.columns):
            continue

        # Filter EQ series
        if "SERIES" in df.columns:
            df = df[df["SERIES"].fillna("").str.strip() == "EQ"]

        for _, row in df.iterrows():
            sym = str(row["SYMBOL"]).strip().upper()
            raw_date = str(row["DATE"]).strip()
            # Parse date formats: 22-May-2026 or 22/05/2026 or YYYY-MM-DD
            for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    d_str = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            else:
                continue

            if d_str not in price_history:
                price_history[d_str] = {}
            try:
                price_history[d_str][sym] = {
                    "open":  float(str(row.get("OPEN",  0)).replace(",", "") or 0),
                    "high":  float(str(row.get("HIGH",  0)).replace(",", "") or 0),
                    "low":   float(str(row.get("LOW",   0)).replace(",", "") or 0),
                    "close": float(str(row.get("CLOSE", 0)).replace(",", "") or 0),
                }
            except (ValueError, TypeError):
                continue
    except Exception as e:
        print(f"  Error reading {bhav_file}: {e}")

trading_days = sorted(price_history.keys())
print(f"Trading days loaded: {len(trading_days)}")

# ── Load existing sim results ─────────────────────────────────────────────────
if SIM_FILE.exists():
    with open(SIM_FILE) as f:
        sim = json.load(f)
else:
    sim = {"open": [], "closed": [], "fe": []}

# Convert open positions to dict for fast lookup {symbol: position}
open_pos = {}
for p in sim.get("open", []):
    open_pos[p["symbol"]] = p
closed_pos = sim.get("closed", [])
fe_pos     = sim.get("fe", [])

# Track processed signal keys
processed_signals = set()
for p in list(open_pos.values()) + closed_pos + fe_pos:
    processed_signals.add((p["symbol"], p["signal_date"]))

# ── Helper: recalculate avg_buy ───────────────────────────────────────────────
def calc_avg(pos):
    prices = [pos["buy1_price"]]
    for i in [2, 3, 4]:
        bp = pos.get(f"buy{i}_price")
        if bp:
            prices.append(bp)
    return round(sum(prices) / len(prices), 2)

def next_buy_trigger(pos):
    """Price threshold to trigger next buy = avg_buy * 0.90"""
    return round(pos["avg_buy"] * BUY_DIP_PCT, 2)

def target_price(pos):
    return round(pos["avg_buy"] * TARGET_PCT, 2)

def sl_price(pos):
    return round(pos["avg_buy"] * SL_PCT, 2)

def market_days_between(start_date, end_date):
    """Count trading days between two dates (inclusive of end, exclusive of start)."""
    if start_date >= end_date:
        return 0
    return sum(1 for d in trading_days if start_date < d <= end_date)

# ── Process each trading day ──────────────────────────────────────────────────
# Build signal map: {signal_date: [signal, ...]}
signal_map = {}
for sig in signals:
    signal_map.setdefault(sig["signal_date"], []).append(sig)

new_exits  = 0
new_buys_added = 0
new_pos_added  = 0

for day in trading_days:
    prices_today = price_history[day]

    # 1. Update existing open positions
    to_close = []
    for sym, pos in list(open_pos.items()):
        if day <= pos["buy1_date"]:
            continue  # Skip days before or on signal date

        p = prices_today.get(sym)
        if not p or p["close"] <= 0:
            continue  # No price data for this symbol today

        ltp   = p["close"]
        tgt   = target_price(pos)
        sl    = sl_price(pos)
        mdays = market_days_between(pos["buy1_date"], day)
        pos["ltp"]          = ltp
        pos["market_days"]  = mdays
        pos["gain_pct"]     = round((ltp - pos["avg_buy"]) / pos["avg_buy"] * 100, 2)
        pos["pnl"]          = round(ltp - pos["avg_buy"], 2)
        pos["target"]       = tgt
        pos["stoploss"]     = sl

        # Check exits (priority: target > SL > FE)
        if ltp >= tgt:
            pos["exit_price"]  = tgt
            pos["exit_date"]   = day
            pos["result"]      = "PROFIT"
            pos["gain_pct"]    = round((tgt - pos["avg_buy"]) / pos["avg_buy"] * 100, 2)
            pos["pnl"]         = round(tgt - pos["avg_buy"], 2)
            to_close.append((sym, "closed", "PROFIT"))
            new_exits += 1
        elif ltp <= sl:
            pos["exit_price"]  = sl
            pos["exit_date"]   = day
            pos["result"]      = "LOSS"
            pos["gain_pct"]    = round((sl - pos["avg_buy"]) / pos["avg_buy"] * 100, 2)
            pos["pnl"]         = round(sl - pos["avg_buy"], 2)
            to_close.append((sym, "closed", "LOSS"))
            new_exits += 1
        elif mdays >= MAX_DAYS:
            pos["exit_price"]  = ltp
            pos["exit_date"]   = day
            pos["result"]      = "FE"
            to_close.append((sym, "fe", "FE"))
            new_exits += 1
        elif pos["buys_done"] < MAX_BUYS:
            # Check next buy trigger
            trigger = next_buy_trigger(pos)
            if ltp <= trigger:
                n = pos["buys_done"] + 1
                pos[f"buy{n}_price"] = ltp
                pos[f"buy{n}_date"]  = day
                pos["buys_done"]     = n
                pos["avg_buy"]       = calc_avg(pos)
                pos["target"]        = target_price(pos)
                pos["stoploss"]      = sl_price(pos)
                new_buys_added += 1

    # Move closed/FE positions
    for sym, bucket, result in to_close:
        pos = open_pos.pop(sym)
        if bucket == "closed":
            closed_pos.append(pos)
        else:
            fe_pos.append(pos)

    # 2. Check new signals for today
    for sig in signal_map.get(day, []):
        sym = sig["symbol"]
        key = (sym, day)
        if key in processed_signals:
            continue
        if sym in open_pos:
            continue  # Already have open position for this symbol

        p = prices_today.get(sym)
        buy1 = sig["close"] if (p is None or p["close"] <= 0) else p["close"]
        if buy1 <= 0:
            buy1 = sig["close"]

        pos = {
            "symbol":        sym,
            "signal_date":   day,
            "buy1_price":    round(buy1, 2),
            "buy1_date":     day,
            "buy2_price":    None,
            "buy2_date":     None,
            "buy3_price":    None,
            "buy3_date":     None,
            "buy4_price":    None,
            "buy4_date":     None,
            "buys_done":     1,
            "avg_buy":       round(buy1, 2),
            "target":        round(buy1 * TARGET_PCT, 2),
            "stoploss":      round(buy1 * SL_PCT, 2),
            "ltp":           round(buy1, 2),
            "pnl":           0.0,
            "gain_pct":      0.0,
            "market_days":   0,
            "bullish_count": sig["bullish_count"],
            "bearish_count": sig["bearish_count"],
            "neutral_count": sig["neutral_count"],
            "total_count":   sig["total_count"],
            "bullish_pct":   sig["bullish_pct"],
        }
        open_pos[sym] = pos
        processed_signals.add(key)
        new_pos_added += 1

# ── Also update LTP for remaining open positions with latest bhavcopy ─────────
if trading_days:
    latest_day = trading_days[-1]
    prices_latest = price_history[latest_day]
    for sym, pos in open_pos.items():
        p = prices_latest.get(sym)
        if p and p["close"] > 0:
            ltp = p["close"]
            pos["ltp"]         = ltp
            pos["gain_pct"]    = round((ltp - pos["avg_buy"]) / pos["avg_buy"] * 100, 2)
            pos["pnl"]         = round(ltp - pos["avg_buy"], 2)
            pos["market_days"] = market_days_between(pos["buy1_date"], latest_day)

# ── Build sim_results ─────────────────────────────────────────────────────────
sim_out = {
    "open":   sorted(list(open_pos.values()), key=lambda x: x["signal_date"], reverse=True),
    "closed": sorted(closed_pos, key=lambda x: x.get("exit_date",""), reverse=True),
    "fe":     sorted(fe_pos,     key=lambda x: x.get("exit_date",""), reverse=True),
}

# ── Save sim results ──────────────────────────────────────────────────────────
SIGNAL_DIR = Path("signal_data")
SIGNAL_DIR.mkdir(exist_ok=True)

with open(SIM_FILE, "w") as f:
    json.dump(sim_out, f, indent=2)

# ── Compute meta stats ────────────────────────────────────────────────────────
closed_all  = sim_out["closed"] + sim_out["fe"]
profits     = [p for p in sim_out["closed"] if p.get("result") == "PROFIT"]
losses      = [p for p in sim_out["closed"] if p.get("result") == "LOSS"]
total_closed = len(sim_out["closed"])
win_rate    = round(len(profits) / total_closed * 100, 1) if total_closed > 0 else 0
avg_gain    = round(sum(p.get("gain_pct",0) for p in profits) / len(profits), 2) if profits else 0
avg_loss    = round(sum(p.get("gain_pct",0) for p in losses)  / len(losses),  2) if losses  else 0

meta = {
    "last_run":     datetime.now().strftime("%Y-%m-%d %H:%M IST"),
    "last_trade_day": trading_days[-1] if trading_days else "",
    "total_signals": len(signals),
    "open_count":   len(sim_out["open"]),
    "closed_count": len(sim_out["closed"]),
    "fe_count":     len(sim_out["fe"]),
    "profit_count": len(profits),
    "loss_count":   len(losses),
    "win_rate":     win_rate,
    "avg_gain_pct": avg_gain,
    "avg_loss_pct": avg_loss,
    "new_exits":    new_exits,
    "new_buys_added": new_buys_added,
    "new_positions": new_pos_added,
}

with open(Path("signal_data/sim_meta.json"), "w") as f:
    json.dump(meta, f, indent=2)

print(f"\n✅ Simulation complete!")
print(f"   Open: {len(sim_out['open'])} | Closed: {len(sim_out['closed'])} | FE: {len(sim_out['fe'])}")
print(f"   New positions: {new_pos_added} | New exits: {new_exits} | New buys: {new_buys_added}")
print(f"   Win rate: {win_rate}% | Avg gain: {avg_gain}% | Avg loss: {avg_loss}%")
