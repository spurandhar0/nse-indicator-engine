"""
NSE Telegram + WhatsApp Image Report
4 images:
  1. Bullish Trend  — Top 10 EQ Symbols (white bg)
  2. Bearish Trend  — Top 10 EQ Symbols (white bg)
  3. Neutral Trend  — Top 10 EQ Symbols (white bg)
  4. Market Movers  — Top 10 Gainers / Losers / Most Active (white bg)

Fixes vs previous version:
  - CHANGE_PCT computed from CLOSE & PREV_CLOSE (not a CSV column)
  - Scanner name extraction simplified — no silent failures
  - Layout: wider canvas (12"), dynamic height, no text clipping
  - 4th image: Gainers / Losers / Most Active in one image
  - WhatsApp sending via Twilio + ImgBB image hosting
  - Today-specific data only (nse_engine already writes date-named file)
"""

import os, sys, glob, re, base64
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "")
OUTPUT_DIR    = os.environ.get("OUTPUT_DIR", "output_data")
INDICATOR_DIR = os.environ.get("INDICATOR_DIR", "indicator_data")
MAPPING_FILE  = "src/mc_nse_mapping.csv"

# WhatsApp via Twilio + ImgBB
TWILIO_SID    = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
WA_FROM       = os.environ.get("WA_FROM_NUMBER", "")   # e.g. whatsapp:+14155238886
WA_TO         = "whatsapp:+97450740794"
IMGBB_KEY     = os.environ.get("IMGBB_API_KEY", "")

IST   = pytz.timezone("Asia/Kolkata")
NOW   = datetime.now(IST)
TOP_N = 10

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = "#1B8C3E"
RED    = "#C0392B"
BLUE   = "#2471A3"
ORANGE = "#E67E22"
LGREY  = "#F7F7F7"
MGREY  = "#DDDDDD"
DARK   = "#1A1A1A"
MUTED  = "#666666"
WHITE  = "#FFFFFF"


# ── Load latest output report (date-specific) ─────────────────────────────────
def load_report():
    files = sorted(glob.glob(f"{OUTPUT_DIR}/NSE_Indicator_Report_*.csv"), reverse=True)
    if not files:
        print("[ERROR] No report CSV found in", OUTPUT_DIR)
        sys.exit(1)
    path = files[0]
    print(f"[DATA] Loading: {path}")
    df = pd.read_csv(path, dtype=str)
    print(f"[DATA] Total rows: {len(df):,}")

    # Convert numeric columns
    for col in ["CLOSE", "PREV_CLOSE", "LTP_MC"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Compute CHANGE_PCT (not present in CSV — derive from bhavcopy prices)
    if "CLOSE" in df.columns and "PREV_CLOSE" in df.columns:
        df["CHANGE_PCT"] = (
            (df["CLOSE"] - df["PREV_CLOSE"]) / df["PREV_CLOSE"].replace(0, np.nan)
        ) * 100
    elif "LTP_MC" in df.columns and "PREV_CLOSE" in df.columns:
        df["CHANGE_PCT"] = (
            (df["LTP_MC"] - df["PREV_CLOSE"]) / df["PREV_CLOSE"].replace(0, np.nan)
        ) * 100
    else:
        df["CHANGE_PCT"] = np.nan

    # Fallback CLOSE → LTP_MC if bhavcopy price missing
    if "CLOSE" in df.columns and "LTP_MC" in df.columns:
        df["CLOSE"] = df["CLOSE"].fillna(df["LTP_MC"])
    if "PREV_CLOSE" not in df.columns:
        df["PREV_CLOSE"] = np.nan

    # EQ series only, valid NSE symbol
    if "SERIES" in df.columns:
        df = df[df["SERIES"].fillna("").str.upper() == "EQ"]
    if "NSE_SYMBOL" in df.columns:
        df = df[df["NSE_SYMBOL"].notna() & (df["NSE_SYMBOL"].str.strip() != "")]

    m = re.search(r"(\d{2}-\w{3}-\d{4})", path)
    report_date = m.group(1) if m else NOW.strftime("%d-%b-%Y")
    print(f"[DATA] EQ rows: {len(df):,} | Date: {report_date}")
    return df, report_date


# ── Build top-10 per trend ────────────────────────────────────────────────────
def top10(df, trend):
    sub = df[df["Trend"].fillna("").str.lower() == trend.lower()].copy()
    if sub.empty:
        return pd.DataFrame()

    # Signal count per symbol → pick top N
    counts = (
        sub.groupby("NSE_SYMBOL").size()
        .reset_index(name="Count")
        .sort_values("Count", ascending=False)
        .head(TOP_N)
        .reset_index(drop=True)
    )
    top_syms = counts["NSE_SYMBOL"].tolist()
    sub_top = sub[sub["NSE_SYMBOL"].isin(top_syms)]

    # Price: median per symbol (handles duplicates)
    price_cols = [c for c in ["CLOSE", "PREV_CLOSE", "CHANGE_PCT"] if c in sub_top.columns]
    if price_cols:
        price = sub_top.groupby("NSE_SYMBOL")[price_cols].median().reset_index()
    else:
        price = pd.DataFrame({"NSE_SYMBOL": top_syms})

    # Top scanner name per symbol — simplified, no silent failure
    scanner_col = next(
        (c for c in ["ScannerName", "Scanner", "SignalName", "IndicatorSummary", "Signal"]
         if c in sub_top.columns),
        None
    )
    if scanner_col:
        def _top_name(x):
            vals = x.dropna().astype(str)
            return vals.value_counts().index[0] if len(vals) > 0 else ""
        sc = (
            sub_top.groupby("NSE_SYMBOL")[scanner_col]
            .agg(_top_name)
            .reset_index(name="Scanner")
        )
    else:
        sc = pd.DataFrame({"NSE_SYMBOL": top_syms, "Scanner": [""] * len(top_syms)})

    result = counts.merge(price, on="NSE_SYMBOL", how="left").merge(sc, on="NSE_SYMBOL", how="left")
    result["Scanner"] = result["Scanner"].fillna("").astype(str)
    return result


# ── Draw one trend image ──────────────────────────────────────────────────────
def draw_trend_image(df_top, trend, accent, icon, report_date,
                     bull_cnt, bear_cnt, neut_cnt, total_cnt, out_path):
    n = max(len(df_top), 1)

    # Canvas sizing
    HDR_H   = 1.05
    COLH_H  = 0.42
    ROW_H   = 0.58
    GAP     = 0.12
    FOOT_H  = 0.90
    fig_h   = HDR_H + GAP + COLH_H + n * ROW_H + GAP*2 + FOOT_H

    fig, ax = plt.subplots(figsize=(12, fig_h))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor(WHITE)

    y = fig_h

    # ── Header bar ───────────────────────────────────────────────────────────
    y -= HDR_H
    ax.add_patch(FancyBboxPatch((0, y), 12, HDR_H, boxstyle="square,pad=0",
                                linewidth=0, facecolor=accent, zorder=3))
    ax.add_patch(plt.Circle((0.7, y + HDR_H * 0.5), 0.27, color=WHITE, alpha=0.20, zorder=4))
    ax.text(0.7, y + HDR_H * 0.5, icon, ha="center", va="center",
            fontsize=18, color=WHITE, fontweight="bold", zorder=5)
    trend_cnt = bull_cnt if trend == "Bullish" else (bear_cnt if trend == "Bearish" else neut_cnt)
    ax.text(1.25, y + HDR_H * 0.68,
            f"{trend.upper()} TREND — Top {n} NSE EQ Symbols",
            ha="left", va="center", fontsize=13, color=WHITE, fontweight="bold", zorder=5)
    ax.text(1.25, y + HDR_H * 0.28,
            f"Date: {report_date}   |   {trend} Signals: {trend_cnt:,}   /   Total EQ: {total_cnt:,}",
            ha="left", va="center", fontsize=9, color=WHITE, alpha=0.90, zorder=5)

    # ── Column header row ─────────────────────────────────────────────────────
    y -= GAP + COLH_H
    cx = {"rank": 0.38, "sym": 1.55, "scan": 5.20,
          "cls": 8.30, "prev": 9.65, "chg": 10.95, "cnt": 11.75}
    ax.add_patch(FancyBboxPatch((0.1, y), 11.8, COLH_H, boxstyle="round,pad=0.04",
                                linewidth=0, facecolor=accent, alpha=0.10, zorder=2))
    hy = y + COLH_H / 2
    hs = dict(ha="center", va="center", fontsize=8, color=accent, fontweight="bold", zorder=3)
    ax.text(cx["rank"], hy, "#", **hs)
    ax.text(cx["sym"],  hy, "NSE SYMBOL",
            ha="left", va="center", fontsize=8, color=accent, fontweight="bold", zorder=3)
    ax.text(cx["scan"], hy, "TOP SCANNER / SIGNAL",
            ha="left", va="center", fontsize=8, color=accent, fontweight="bold", zorder=3)
    ax.text(cx["cls"],  hy, "CLOSE (Rs.)", **hs)
    ax.text(cx["prev"], hy, "PREV CLOSE",  **hs)
    ax.text(cx["chg"],  hy, "CHG %",       **hs)
    ax.text(cx["cnt"],  hy, "SIG",         **hs)

    # ── Data rows ─────────────────────────────────────────────────────────────
    for i, row in enumerate(df_top.itertuples(index=False)):
        y -= ROW_H
        ry = y + ROW_H / 2

        bg = LGREY if i % 2 == 0 else WHITE
        ax.add_patch(FancyBboxPatch((0.1, y), 11.8, ROW_H * 0.90,
                                    boxstyle="round,pad=0.04", linewidth=0.3,
                                    edgecolor=MGREY, facecolor=bg, zorder=1))

        rk = i + 1
        bc = accent if rk <= 3 else MGREY
        tc = WHITE  if rk <= 3 else DARK
        ax.add_patch(plt.Circle((cx["rank"], ry), 0.19, color=bc, zorder=2))
        ax.text(cx["rank"], ry, str(rk), ha="center", va="center",
                fontsize=7.5, color=tc, fontweight="bold", zorder=3)

        ax.text(cx["sym"], ry, str(row.NSE_SYMBOL),
                ha="left", va="center", fontsize=11, color=accent, fontweight="bold", zorder=3)

        # Scanner — safe access + truncate
        scan_txt = str(getattr(row, "Scanner", "") or "")
        if len(scan_txt) > 48:
            scan_txt = scan_txt[:46] + "…"
        ax.text(cx["scan"], ry, scan_txt,
                ha="left", va="center", fontsize=7, color=MUTED, zorder=3)

        # Prices
        cls_v = getattr(row, "CLOSE", np.nan)      if hasattr(row, "CLOSE")      else np.nan
        prv_v = getattr(row, "PREV_CLOSE", np.nan) if hasattr(row, "PREV_CLOSE") else np.nan
        chg_v = getattr(row, "CHANGE_PCT", np.nan) if hasattr(row, "CHANGE_PCT") else np.nan

        try: cls_v = float(cls_v)
        except: cls_v = np.nan
        try: prv_v = float(prv_v)
        except: prv_v = np.nan
        try: chg_v = float(chg_v)
        except: chg_v = np.nan

        ax.text(cx["cls"],  ry, f"{cls_v:,.2f}" if not np.isnan(cls_v) else "—",
                ha="center", va="center", fontsize=9, color=DARK, fontweight="bold", zorder=3)
        ax.text(cx["prev"], ry, f"{prv_v:,.2f}" if not np.isnan(prv_v) else "—",
                ha="center", va="center", fontsize=9, color=MUTED, zorder=3)

        if not np.isnan(chg_v):
            cc  = GREEN if chg_v >= 0 else RED
            cbg = "#E9F7EF" if chg_v >= 0 else "#FDEDEC"
            ax.add_patch(FancyBboxPatch((cx["chg"] - 0.58, ry - 0.15), 1.16, 0.30,
                                        boxstyle="round,pad=0.04", linewidth=0, facecolor=cbg, zorder=2))
            ax.text(cx["chg"], ry, f"{'+'if chg_v>=0 else ''}{chg_v:.2f}%",
                    ha="center", va="center", fontsize=8, color=cc, fontweight="bold", zorder=3)
        else:
            ax.text(cx["chg"], ry, "—", ha="center", va="center", fontsize=9, color=MUTED, zorder=3)

        cnt_v = int(getattr(row, "Count", 0))
        ax.add_patch(FancyBboxPatch((cx["cnt"] - 0.36, ry - 0.14), 0.72, 0.28,
                                    boxstyle="round,pad=0.03", linewidth=0, facecolor=accent, alpha=0.12, zorder=2))
        ax.text(cx["cnt"], ry, str(cnt_v),
                ha="center", va="center", fontsize=8, color=accent, fontweight="bold", zorder=3)

    # ── Divider ───────────────────────────────────────────────────────────────
    y -= GAP
    ax.plot([0.1, 11.9], [y, y], color=MGREY, lw=0.8, zorder=2)

    # ── Totals summary bar ────────────────────────────────────────────────────
    SUM_H = 0.52
    y -= SUM_H
    ax.add_patch(FancyBboxPatch((0.1, y), 11.8, SUM_H, boxstyle="round,pad=0.05",
                                linewidth=0, facecolor=LGREY, zorder=1))
    sy = y + SUM_H / 2
    share = (trend_cnt / total_cnt * 100) if total_cnt else 0

    ax.text(1.5,  sy, f"● Bullish: {bull_cnt:,}", ha="center", va="center",
            fontsize=9, color=GREEN,  fontweight="bold", zorder=3)
    ax.plot([2.7, 2.7], [y+0.08, y+0.44], color=MGREY, lw=0.8)
    ax.text(3.95, sy, f"● Bearish: {bear_cnt:,}", ha="center", va="center",
            fontsize=9, color=RED,    fontweight="bold", zorder=3)
    ax.plot([5.1, 5.1], [y+0.08, y+0.44], color=MGREY, lw=0.8)
    ax.text(6.35, sy, f"● Neutral: {neut_cnt:,}", ha="center", va="center",
            fontsize=9, color=BLUE,   fontweight="bold", zorder=3)
    ax.plot([7.55, 7.55], [y+0.08, y+0.44], color=MGREY, lw=0.8)
    ax.text(9.30, sy, f"Total EQ Signals: {total_cnt:,}", ha="center", va="center",
            fontsize=9, color=DARK,   fontweight="bold", zorder=3)
    ax.text(11.2, sy, f"{share:.1f}%", ha="center", va="center",
            fontsize=9, color=accent, fontweight="bold", zorder=3)

    # ── Footer ────────────────────────────────────────────────────────────────
    y -= 0.28
    ax.text(6, y,
            f"Generated: {NOW.strftime('%d %b %Y  %I:%M %p IST')}   |   NSE Indicator Engine   |   Series: EQ",
            ha="center", va="top", fontsize=7.5, color=MUTED, zorder=3)

    plt.tight_layout(pad=0.2)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=WHITE, edgecolor="none")
    plt.close(fig)
    print(f"[IMAGE] Saved → {out_path}")


# ── Market Movers: load & prep ────────────────────────────────────────────────
def load_mc_mapping():
    if os.path.exists(MAPPING_FILE):
        try:
            mc_df = pd.read_csv(MAPPING_FILE, dtype=str)
            if "MC_Code" in mc_df.columns and "NSE_Symbol" in mc_df.columns:
                return dict(zip(mc_df["MC_Code"].fillna(""), mc_df["NSE_Symbol"].fillna("")))
        except Exception:
            pass
    return {}


def load_movers(report_date):
    folders = sorted(
        [f for f in glob.glob(f"{INDICATOR_DIR}/*") if os.path.isdir(f)],
        reverse=True
    )
    date_folder = None
    for f in folders:
        if os.path.basename(f) == report_date:
            date_folder = f
            break
    if not date_folder and folders:
        date_folder = folders[0]
        print(f"[MOVERS] Exact date folder not found — using {date_folder}")

    out = {}
    for fname in ["gainers.csv", "losers.csv", "most_active.csv"]:
        path = os.path.join(date_folder, fname) if date_folder else ""
        if path and os.path.exists(path):
            try:
                out[fname] = pd.read_csv(path, dtype=str)
                print(f"[MOVERS] {fname}: {len(out[fname])} rows")
            except Exception as e:
                print(f"[MOVERS] {fname} read error: {e}")
                out[fname] = pd.DataFrame()
        else:
            out[fname] = pd.DataFrame()
    return out


def prep_mover_df(df, mc_map):
    if df.empty:
        return df
    df = df.copy()

    # Resolve NSE symbol: direct nsCode > MC mapping > shortName > StockName
    if "nsCode" in df.columns:
        df["NSE_SYMBOL"] = df["nsCode"].fillna("")
    elif "MC_Code" in df.columns:
        df["NSE_SYMBOL"] = df["MC_Code"].map(mc_map).fillna(
            df.get("shortName", df.get("StockName", ""))
        )
    elif "shortName" in df.columns:
        df["NSE_SYMBOL"] = df["shortName"]
    else:
        df["NSE_SYMBOL"] = ""

    # Standardise price columns
    col_map = [
        ("currPrice",     "CLOSE"),
        ("lastPrice",     "CLOSE"),
        ("prevPrice",     "PREV_CLOSE"),
        ("previousClose", "PREV_CLOSE"),
        ("pChange",       "CHANGE_PCT"),
        ("volume",        "VOLUME"),
        ("quantityTraded","VOLUME"),
    ]
    for src, dst in col_map:
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    for col in ["CLOSE", "PREV_CLOSE", "CHANGE_PCT", "VOLUME"]:
        df[col] = pd.to_numeric(df.get(col, np.nan), errors="coerce")

    df = df[df["NSE_SYMBOL"].astype(str).str.strip() != ""].reset_index(drop=True)
    return df.head(TOP_N)


# ── Draw Market Movers image (3 sections stacked) ─────────────────────────────
def draw_movers_image(gainers, losers, most_active, report_date, out_path):
    sections = [
        ("TOP 10 GAINERS",     GREEN,  "▲", gainers),
        ("TOP 10 LOSERS",      RED,    "▼", losers),
        ("TOP 10 MOST ACTIVE", ORANGE, "★", most_active),
    ]

    TITLE_H  = 0.85
    SEC_H    = 0.45
    COLH_H   = 0.35
    ROW_H    = 0.42
    SEC_GAP  = 0.22
    FOOT_H   = 0.70

    def section_h(df):
        n = min(len(df), TOP_N) if not df.empty else 1
        return SEC_H + COLH_H + n * ROW_H + 0.10

    fig_h = TITLE_H + sum(section_h(s[3]) for s in sections) + SEC_GAP * 2 + FOOT_H + 0.30
    fig, ax = plt.subplots(figsize=(11, fig_h))
    ax.set_xlim(0, 11)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor(WHITE)

    y = fig_h

    # ── Main title bar ────────────────────────────────────────────────────────
    y -= TITLE_H
    ax.add_patch(FancyBboxPatch((0, y), 11, TITLE_H, boxstyle="square,pad=0",
                                linewidth=0, facecolor=DARK, zorder=3))
    ax.text(5.5, y + TITLE_H * 0.67, "NSE MARKET MOVERS",
            ha="center", va="center", fontsize=15, color=WHITE, fontweight="bold", zorder=5)
    ax.text(5.5, y + TITLE_H * 0.28,
            f"Date: {report_date}   |   Top 10 Gainers · Losers · Most Active by Volume",
            ha="center", va="center", fontsize=9, color=WHITE, alpha=0.85, zorder=5)

    # ── 3 sections ────────────────────────────────────────────────────────────
    for s_idx, (title, accent, icon, df) in enumerate(sections):
        if s_idx > 0:
            y -= SEC_GAP

        # Section header
        y -= SEC_H
        ax.add_patch(FancyBboxPatch((0.1, y), 10.8, SEC_H,
                                    boxstyle="square,pad=0", linewidth=0, facecolor=accent, zorder=3))
        ax.text(0.55, y + SEC_H * 0.5, icon, ha="center", va="center",
                fontsize=13, color=WHITE, fontweight="bold", zorder=5)
        ax.text(0.95, y + SEC_H * 0.5, title, ha="left", va="center",
                fontsize=11, color=WHITE, fontweight="bold", zorder=5)

        # Column header
        y -= COLH_H
        show_vol = (title == "TOP 10 MOST ACTIVE")
        cx = {"rank": 0.35, "sym": 1.35, "cls": 6.80, "prv": 8.20, "chg": 9.65, "vol": 10.70}
        ax.add_patch(FancyBboxPatch((0.1, y), 10.8, COLH_H, boxstyle="round,pad=0.03",
                                    linewidth=0, facecolor=accent, alpha=0.10, zorder=2))
        hy = y + COLH_H / 2
        hs = dict(ha="center", va="center", fontsize=7.5, color=accent, fontweight="bold", zorder=3)
        ax.text(cx["rank"], hy, "#", **hs)
        ax.text(cx["sym"],  hy, "NSE SYMBOL",
                ha="left", va="center", fontsize=7.5, color=accent, fontweight="bold", zorder=3)
        ax.text(cx["cls"],  hy, "CLOSE (Rs.)", **hs)
        ax.text(cx["prv"],  hy, "PREV CLOSE",  **hs)
        ax.text(cx["chg"],  hy, "CHG %",        **hs)
        if show_vol:
            ax.text(cx["vol"], hy, "VOL (Cr)", **hs)

        # Data rows
        rows_n = min(len(df), TOP_N) if not df.empty else 0
        if rows_n == 0:
            y -= ROW_H
            ax.text(5.5, y + ROW_H / 2, "No data available for today",
                    ha="center", va="center", fontsize=9, color=MUTED, style="italic", zorder=3)
        else:
            for i in range(rows_n):
                row = df.iloc[i]
                y -= ROW_H
                ry = y + ROW_H / 2

                bg = LGREY if i % 2 == 0 else WHITE
                ax.add_patch(FancyBboxPatch((0.1, y), 10.8, ROW_H * 0.90,
                                            boxstyle="round,pad=0.03", linewidth=0.3,
                                            edgecolor=MGREY, facecolor=bg, zorder=1))

                rk = i + 1
                bc = accent if rk <= 3 else MGREY
                tc = WHITE  if rk <= 3 else DARK
                ax.add_patch(plt.Circle((cx["rank"], ry), 0.17, color=bc, zorder=2))
                ax.text(cx["rank"], ry, str(rk), ha="center", va="center",
                        fontsize=7, color=tc, fontweight="bold", zorder=3)

                sym = str(row.get("NSE_SYMBOL", ""))[:18]
                ax.text(cx["sym"], ry, sym, ha="left", va="center",
                        fontsize=10, color=accent, fontweight="bold", zorder=3)

                def _fval(key):
                    try: return float(row.get(key, np.nan))
                    except: return np.nan

                cls_v = _fval("CLOSE")
                prv_v = _fval("PREV_CLOSE")
                chg_v = _fval("CHANGE_PCT")
                vol_v = _fval("VOLUME")

                ax.text(cx["cls"], ry, f"{cls_v:,.2f}" if not np.isnan(cls_v) else "—",
                        ha="center", va="center", fontsize=9, color=DARK, fontweight="bold", zorder=3)
                ax.text(cx["prv"], ry, f"{prv_v:,.2f}" if not np.isnan(prv_v) else "—",
                        ha="center", va="center", fontsize=9, color=MUTED, zorder=3)

                if not np.isnan(chg_v):
                    cc  = GREEN if chg_v >= 0 else RED
                    cbg = "#E9F7EF" if chg_v >= 0 else "#FDEDEC"
                    ax.add_patch(FancyBboxPatch((cx["chg"] - 0.56, ry - 0.13), 1.12, 0.26,
                                                boxstyle="round,pad=0.03", linewidth=0, facecolor=cbg, zorder=2))
                    ax.text(cx["chg"], ry, f"{'+'if chg_v>=0 else ''}{chg_v:.2f}%",
                            ha="center", va="center", fontsize=8, color=cc, fontweight="bold", zorder=3)
                else:
                    ax.text(cx["chg"], ry, "—", ha="center", va="center", fontsize=9, color=MUTED, zorder=3)

                if show_vol and not np.isnan(vol_v):
                    ax.text(cx["vol"], ry, f"{vol_v/1e7:.2f}",
                            ha="center", va="center", fontsize=8.5, color=DARK, zorder=3)

    # ── Footer ────────────────────────────────────────────────────────────────
    y -= 0.30
    ax.text(5.5, y,
            f"Generated: {NOW.strftime('%d %b %Y  %I:%M %p IST')}   |   NSE Indicator Engine",
            ha="center", va="top", fontsize=7.5, color=MUTED, zorder=3)

    plt.tight_layout(pad=0.2)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=WHITE, edgecolor="none")
    plt.close(fig)
    print(f"[IMAGE] Saved → {out_path}")


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(path, caption):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] Credentials missing — skipping")
        return
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT_ID, "caption": caption},
            files={"photo": f},
            timeout=30,
        )
    status = "OK" if r.status_code == 200 else f"FAIL {r.status_code}: {r.text[:120]}"
    print(f"[TG] {status} — {os.path.basename(path)}")


# ── WhatsApp via Twilio + ImgBB ───────────────────────────────────────────────
def _upload_imgbb(path):
    """Upload image to ImgBB, return public URL or None."""
    if not IMGBB_KEY:
        return None
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post("https://api.imgbb.com/1/upload",
                      data={"key": IMGBB_KEY, "image": b64}, timeout=30)
    if r.status_code == 200:
        return r.json()["data"]["url"]
    print(f"[ImgBB] Upload failed ({r.status_code}): {r.text[:100]}")
    return None


def send_whatsapp(path, caption):
    """Send image to WhatsApp via Twilio REST API."""
    if not all([TWILIO_SID, TWILIO_TOKEN, WA_FROM]):
        print("[WA] Twilio credentials missing (TWILIO_SID / TWILIO_AUTH_TOKEN / WA_FROM_NUMBER) — skipping")
        return
    img_url = _upload_imgbb(path)
    if not img_url:
        print("[WA] No public image URL — skipping")
        return
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"From": WA_FROM, "To": WA_TO, "Body": caption, "MediaUrl": img_url},
        timeout=30,
    )
    status = "OK" if r.status_code in (200, 201) else f"FAIL {r.status_code}: {r.text[:120]}"
    print(f"[WA] {status} — {os.path.basename(path)}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("[START] NSE Image Report — 4 images (Bullish / Bearish / Neutral / Market Movers)")
    df, report_date = load_report()

    if "Trend" not in df.columns:
        print("[ERROR] 'Trend' column not found in report")
        sys.exit(1)

    bull_cnt  = len(df[df["Trend"].str.lower() == "bullish"])
    bear_cnt  = len(df[df["Trend"].str.lower() == "bearish"])
    neut_cnt  = len(df[df["Trend"].str.lower() == "neutral"])
    total_cnt = bull_cnt + bear_cnt + neut_cnt
    print(f"[DATA] Bullish:{bull_cnt:,} | Bearish:{bear_cnt:,} | Neutral:{neut_cnt:,} | Total:{total_cnt:,}")

    dt = report_date.replace("-", "_")
    images = []

    # ── Images 1–3: Trend images ──────────────────────────────────────────────
    for trend, accent, icon in [("Bullish", GREEN, "+"), ("Bearish", RED, "-"), ("Neutral", BLUE, "~")]:
        top = top10(df, trend)
        out = f"/tmp/NSE_{trend}_{dt}.png"
        draw_trend_image(top, trend, accent, icon, report_date,
                         bull_cnt, bear_cnt, neut_cnt, total_cnt, out)
        trend_cnt_now = bull_cnt if trend == "Bullish" else (bear_cnt if trend == "Bearish" else neut_cnt)
        caption = (
            f"*NSE — {trend} Trend*\n"
            f"Date: {report_date} | {trend}: {trend_cnt_now:,} / Total EQ: {total_cnt:,}\n"
            f"Top {len(top)} symbols by signal count"
        )
        send_telegram(out, caption)
        images.append((out, caption))

    # ── Image 4: Market Movers ────────────────────────────────────────────────
    mc_map   = load_mc_mapping()
    movers   = load_movers(report_date)
    gainers  = prep_mover_df(movers.get("gainers.csv",     pd.DataFrame()), mc_map)
    losers   = prep_mover_df(movers.get("losers.csv",      pd.DataFrame()), mc_map)
    active   = prep_mover_df(movers.get("most_active.csv", pd.DataFrame()), mc_map)

    out4 = f"/tmp/NSE_MarketMovers_{dt}.png"
    draw_movers_image(gainers, losers, active, report_date, out4)
    cap4 = (
        f"*NSE Market Movers*\n"
        f"Date: {report_date}\n"
        f"Gainers: {len(gainers)} | Losers: {len(losers)} | Most Active: {len(active)}"
    )
    send_telegram(out4, cap4)
    images.append((out4, cap4))

    # ── Send all 4 to WhatsApp ────────────────────────────────────────────────
    for path, caption in images:
        send_whatsapp(path, caption)

    print(f"[DONE] 4 images generated | Telegram: sent | WhatsApp: {'enabled' if TWILIO_SID else 'skipped (no creds)'}")


if __name__ == "__main__":
    main()
