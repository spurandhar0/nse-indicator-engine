"""
NSE Telegram Image Report — 3 separate professional white-background images
Bullish / Bearish / Neutral — Top 10 EQ symbols each
Columns: Rank, NSE Symbol, Scanner/Signal, Close (Rs.), Prev Close (Rs.), Change%
Footer:  Total counts — Bullish | Bearish | Neutral | Total
Triggered daily Mon–Fri at 9 PM IST via GitHub Actions
"""

import os
import sys
import glob
import json
import re
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
OUTPUT_DIR   = os.environ.get("OUTPUT_DIR", "output_data")
IST          = pytz.timezone("Asia/Kolkata")
NOW_IST      = datetime.now(IST)
TOP_N        = 10

# ── Colours ───────────────────────────────────────────────────────────────────
BULLISH_ACCENT = "#1B8C3E"   # deep green
BEARISH_ACCENT = "#C0392B"   # deep red
NEUTRAL_ACCENT = "#2471A3"   # deep blue
GOLD           = "#B7950B"
LIGHT_GREY     = "#F5F5F5"
MID_GREY       = "#E0E0E0"
TEXT_DARK      = "#1A1A1A"
TEXT_MUTED     = "#666666"
WHITE          = "#FFFFFF"

# ── Load latest report ────────────────────────────────────────────────────────
def load_report():
    files = sorted(glob.glob(f"{OUTPUT_DIR}/NSE_Indicator_Report_*.csv"), reverse=True)
    if not files:
        print("[ERROR] No report CSV found in", OUTPUT_DIR); sys.exit(1)
    path = files[0]
    print(f"[DATA] Loading: {path}")
    df = pd.read_csv(path, dtype=str)
    print(f"[DATA] Total rows: {len(df):,}")

    # Parse numeric columns
    for col in ["CLOSE", "PREV_CLOSE", "CHANGE_PCT"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Compute change% if missing
    if "CHANGE_PCT" not in df.columns or df["CHANGE_PCT"].isna().all():
        if "CLOSE" in df.columns and "PREV_CLOSE" in df.columns:
            df["CHANGE_PCT"] = ((df["CLOSE"] - df["PREV_CLOSE"]) / df["PREV_CLOSE"].replace(0, np.nan)) * 100

    # EQ series only, valid symbol
    if "SERIES" in df.columns:
        df = df[df["SERIES"].fillna("").str.upper() == "EQ"]
    if "NSE_SYMBOL" in df.columns:
        df = df[df["NSE_SYMBOL"].notna() & (df["NSE_SYMBOL"].str.strip() != "")]

    # Extract report date from filename
    m = re.search(r"(\d{2}-\w{3}-\d{4})", path)
    report_date = m.group(1) if m else NOW_IST.strftime("%d-%b-%Y")

    print(f"[DATA] EQ rows: {len(df):,}")
    return df, report_date


# ── Build top-10 table ────────────────────────────────────────────────────────
def top10(df, trend):
    sub = df[df["Trend"].fillna("").str.lower() == trend.lower()].copy()
    if sub.empty:
        return pd.DataFrame()
    # Count signals per symbol
    counts = sub.groupby("NSE_SYMBOL").size().reset_index(name="Count")
    counts = counts.sort_values("Count", ascending=False).head(TOP_N)
    # Merge price info (take last known CLOSE / PREV_CLOSE / CHANGE_PCT)
    price = sub.drop_duplicates("NSE_SYMBOL")[["NSE_SYMBOL","CLOSE","PREV_CLOSE","CHANGE_PCT"]]
    # Also grab scanner name (most common scanner for that symbol)
    scanner_col = None
    for c in ["ScannerName","Scanner","SignalName","IndicatorSummary"]:
        if c in sub.columns:
            scanner_col = c
            break
    if scanner_col:
        vc = sub.groupby("NSE_SYMBOL")[scanner_col].apply(
            lambda x: x.dropna().value_counts()
        )
        sc_dict = {}
        for sym, counts_s in vc.groupby(level=0):
            vals = counts_s.reset_index(level=0, drop=True)
            sc_dict[sym] = vals.index[0] if len(vals) else ""
        sc = pd.DataFrame(list(sc_dict.items()), columns=["NSE_SYMBOL","Scanner"])
    else:
        sc = pd.DataFrame({"NSE_SYMBOL": counts["NSE_SYMBOL"], "Scanner": ""})
    result = counts.merge(price, on="NSE_SYMBOL", how="left").merge(sc, on="NSE_SYMBOL", how="left")
    result["Scanner"] = result["Scanner"].fillna("").astype(str).str[:40]
    return result


# ── Draw a single trend image ─────────────────────────────────────────────────
def draw_image(df_top, trend, accent, icon, report_date,
               bull_cnt, bear_cnt, neut_cnt, total_cnt, out_path):

    n = len(df_top)
    # Canvas
    fig_h = 2.6 + n * 0.72 + 1.0   # dynamic height
    fig, ax = plt.subplots(figsize=(11, fig_h))
    ax.set_xlim(0, 11)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor(WHITE)

    y_cursor = fig_h  # draw from top

    # ── Top accent bar ────────────────────────────────────────────────────────
    bar_h = 1.0
    y_cursor -= bar_h
    bar = FancyBboxPatch((0, y_cursor), 11, bar_h,
                         boxstyle="square,pad=0", linewidth=0,
                         facecolor=accent, zorder=3)
    ax.add_patch(bar)

    # Icon circle
    ax.add_patch(plt.Circle((0.75, y_cursor + bar_h/2), 0.28,
                             color=WHITE, alpha=0.25, zorder=4))
    ax.text(0.75, y_cursor + bar_h/2, icon,
            ha="center", va="center", fontsize=20, color=WHITE,
            fontweight="bold", zorder=5)

    # Title
    ax.text(1.3, y_cursor + bar_h * 0.65,
            f"{trend.upper()} TREND  —  Top {n} NSE Symbols",
            ha="left", va="center", fontsize=14, color=WHITE,
            fontweight="bold", zorder=5)
    # Date + count
    trend_cnt = bull_cnt if trend=="Bullish" else (bear_cnt if trend=="Bearish" else neut_cnt)
    ax.text(1.3, y_cursor + bar_h * 0.28,
            f"Date: {report_date}   |   {trend} Signals: {trend_cnt:,}  /  Total EQ: {total_cnt:,}",
            ha="left", va="center", fontsize=9, color=WHITE, alpha=0.92, zorder=5)

    # ── Column header row ─────────────────────────────────────────────────────
    gap = 0.18
    y_cursor -= gap
    row_h = 0.44
    y_cursor -= row_h
    hdr_bg = FancyBboxPatch((0.1, y_cursor), 10.8, row_h,
                            boxstyle="round,pad=0.04", linewidth=0,
                            facecolor=accent, alpha=0.12, zorder=2)
    ax.add_patch(hdr_bg)

    # Column x positions
    cx = {"rank": 0.38, "sym": 1.55, "scanner": 4.30,
          "close": 7.05, "prev": 8.35, "chg": 9.95, "cnt": 10.85}
    hy = y_cursor + row_h / 2

    hdr_style = dict(ha="center", va="center", fontsize=8.5,
                     color=accent, fontweight="bold", zorder=3)
    ax.text(cx["rank"],    hy, "#",            **hdr_style)
    ax.text(cx["sym"],     hy, "NSE SYMBOL",   ha="left", va="center",
            fontsize=8.5, color=accent, fontweight="bold", zorder=3)
    ax.text(cx["scanner"], hy, "TOP SCANNER",  ha="left", va="center",
            fontsize=8.5, color=accent, fontweight="bold", zorder=3)
    ax.text(cx["close"],   hy, "CLOSE (Rs.)",  **hdr_style)
    ax.text(cx["prev"],    hy, "PREV CLOSE",   **hdr_style)
    ax.text(cx["chg"],     hy, "CHG %",        **hdr_style)
    ax.text(cx["cnt"],     hy, "SIG",          **hdr_style)

    # ── Data rows ─────────────────────────────────────────────────────────────
    for i, row in enumerate(df_top.itertuples(index=False)):
        y_cursor -= 0.06
        row_h_d = 0.62
        y_cursor -= row_h_d
        ry = y_cursor + row_h_d / 2

        # Alternating row background
        row_bg_col = LIGHT_GREY if i % 2 == 0 else WHITE
        bg = FancyBboxPatch((0.1, y_cursor), 10.8, row_h_d,
                            boxstyle="round,pad=0.04", linewidth=0.4,
                            edgecolor=MID_GREY, facecolor=row_bg_col, zorder=1)
        ax.add_patch(bg)

        rank = i + 1

        # Rank badge
        badge_col = accent if rank <= 3 else MID_GREY
        txt_col   = WHITE  if rank <= 3 else TEXT_DARK
        ax.add_patch(plt.Circle((cx["rank"], ry), 0.20,
                                color=badge_col, zorder=2))
        ax.text(cx["rank"], ry, str(rank), ha="center", va="center",
                fontsize=8, color=txt_col, fontweight="bold", zorder=3)

        # Symbol
        ax.text(cx["sym"], ry, str(row.NSE_SYMBOL),
                ha="left", va="center", fontsize=11,
                color=accent, fontweight="bold", zorder=3)

        # Scanner name
        scanner_txt = str(row.Scanner)[:42] if hasattr(row, "Scanner") else ""
        ax.text(cx["scanner"], ry, scanner_txt,
                ha="left", va="center", fontsize=7.5,
                color=TEXT_MUTED, zorder=3)

        # Close price
        close_val = row.CLOSE if not pd.isna(row.CLOSE) else None
        prev_val  = row.PREV_CLOSE if not pd.isna(row.PREV_CLOSE) else None
        close_txt = f"{close_val:,.2f}" if close_val else "—"
        prev_txt  = f"{prev_val:,.2f}"  if prev_val  else "—"
        ax.text(cx["close"], ry, close_txt, ha="center", va="center",
                fontsize=9, color=TEXT_DARK, fontweight="bold", zorder=3)
        ax.text(cx["prev"],  ry, prev_txt,  ha="center", va="center",
                fontsize=9, color=TEXT_MUTED, zorder=3)

        # Change % pill
        chg = row.CHANGE_PCT if not pd.isna(row.CHANGE_PCT) else None
        if chg is not None:
            chg_color = "#1B8C3E" if chg >= 0 else "#C0392B"
            chg_bg    = "#E9F7EF" if chg >= 0 else "#FDEDEC"
            arrow     = "+" if chg >= 0 else ""
            chg_txt   = f"{arrow}{chg:.2f}%"
            pill = FancyBboxPatch((cx["chg"]-0.6, ry-0.16), 1.2, 0.32,
                                  boxstyle="round,pad=0.04", linewidth=0,
                                  facecolor=chg_bg, zorder=2)
            ax.add_patch(pill)
            ax.text(cx["chg"], ry, chg_txt, ha="center", va="center",
                    fontsize=8.5, color=chg_color, fontweight="bold", zorder=3)
        else:
            ax.text(cx["chg"], ry, "—", ha="center", va="center",
                    fontsize=9, color=TEXT_MUTED, zorder=3)

        # Signal count pill
        cnt_pill = FancyBboxPatch((cx["cnt"]-0.38, ry-0.15), 0.76, 0.30,
                                  boxstyle="round,pad=0.04", linewidth=0,
                                  facecolor=accent, alpha=0.12, zorder=2)
        ax.add_patch(cnt_pill)
        ax.text(cx["cnt"], ry, str(int(row.Count)),
                ha="center", va="center", fontsize=8.5,
                color=accent, fontweight="bold", zorder=3)

    # ── Divider line ─────────────────────────────────────────────────────────
    y_cursor -= 0.20
    ax.plot([0.1, 10.9], [y_cursor, y_cursor], color=MID_GREY, lw=0.8, zorder=2)
    y_cursor -= 0.10

    # ── Totals summary bar ────────────────────────────────────────────────────
    summary_h = 0.50
    y_cursor -= summary_h
    sum_bg = FancyBboxPatch((0.1, y_cursor), 10.8, summary_h,
                            boxstyle="round,pad=0.06", linewidth=0,
                            facecolor=LIGHT_GREY, zorder=1)
    ax.add_patch(sum_bg)
    sy = y_cursor + summary_h / 2

    # Bullish
    ax.text(1.4, sy, f"Bullish: {bull_cnt:,}",
            ha="center", va="center", fontsize=9,
            color=BULLISH_ACCENT, fontweight="bold", zorder=3)
    ax.plot([2.3, 2.3], [y_cursor+0.08, y_cursor+0.42], color=MID_GREY, lw=1, zorder=2)
    # Bearish
    ax.text(3.5, sy, f"Bearish: {bear_cnt:,}",
            ha="center", va="center", fontsize=9,
            color=BEARISH_ACCENT, fontweight="bold", zorder=3)
    ax.plot([4.6, 4.6], [y_cursor+0.08, y_cursor+0.42], color=MID_GREY, lw=1, zorder=2)
    # Neutral
    ax.text(5.85, sy, f"Neutral: {neut_cnt:,}",
            ha="center", va="center", fontsize=9,
            color=NEUTRAL_ACCENT, fontweight="bold", zorder=3)
    ax.plot([7.0, 7.0], [y_cursor+0.08, y_cursor+0.42], color=MID_GREY, lw=1, zorder=2)
    # Total
    ax.text(8.45, sy, f"Total EQ Signals: {total_cnt:,}",
            ha="center", va="center", fontsize=9,
            color=TEXT_DARK, fontweight="bold", zorder=3)
    # Current trend share
    share = (trend_cnt / total_cnt * 100) if total_cnt else 0
    ax.text(10.2, sy, f"{share:.1f}%",
            ha="center", va="center", fontsize=9,
            color=accent, fontweight="bold", zorder=3)

    # ── Footer ────────────────────────────────────────────────────────────────
    y_cursor -= 0.12
    ax.text(5.5, y_cursor,
            f"Generated: {NOW_IST.strftime('%d %b %Y  %I:%M %p IST')}   |   NSE Indicator Engine   |   Series: EQ",
            ha="center", va="top", fontsize=7.5, color=TEXT_MUTED, zorder=3)

    plt.tight_layout(pad=0.3)
    fig.savefig(out_path, dpi=160, bbox_inches="tight",
                facecolor=WHITE, edgecolor="none")
    plt.close(fig)
    print(f"[IMAGE] Saved -> {out_path}")


# ── Send to Telegram ──────────────────────────────────────────────────────────
def send_telegram(path, caption):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] TG_BOT_TOKEN or TG_CHAT_ID not set — skipping Telegram send")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    with open(path, "rb") as f:
        resp = requests.post(url, data={"chat_id": TG_CHAT_ID, "caption": caption},
                             files={"photo": f}, timeout=30)
    if resp.status_code == 200:
        print(f"[TG] Sent: {os.path.basename(path)}")
    else:
        print(f"[TG] FAILED ({resp.status_code}): {resp.text[:200]}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("[START] NSE Telegram Image Report — 3 separate images")
    df, report_date = load_report()

    # Counts
    trend_col = "Trend" if "Trend" in df.columns else None
    if trend_col is None:
        print("[ERROR] No 'Trend' column found"); sys.exit(1)

    bull_df = df[df[trend_col].str.lower() == "bullish"]
    bear_df = df[df[trend_col].str.lower() == "bearish"]
    neut_df = df[df[trend_col].str.lower() == "neutral"]
    bull_cnt = len(bull_df)
    bear_cnt = len(bear_df)
    neut_cnt = len(neut_df)
    total_cnt = bull_cnt + bear_cnt + neut_cnt

    print(f"[DATA] Bullish: {bull_cnt:,}  |  Bearish: {bear_cnt:,}  |  Neutral: {neut_cnt:,}  |  Total: {total_cnt:,}")

    date_tag = report_date.replace("-", "_")

    # ── Bullish ───────────────────────────────────────────────────────────────
    top_bull = top10(df, "Bullish")
    out_b = f"/tmp/NSE_Bullish_{date_tag}.png"
    draw_image(top_bull, "Bullish", BULLISH_ACCENT, "+", report_date,
               bull_cnt, bear_cnt, neut_cnt, total_cnt, out_b)
    send_telegram(out_b,
        f"*NSE Indicator Engine — Bullish Trend*\n"
        f"Date: {report_date} | Bullish: {bull_cnt:,} / Total: {total_cnt:,}\n"
        f"Top {len(top_bull)} EQ symbols by signal count")

    # ── Bearish ───────────────────────────────────────────────────────────────
    top_bear = top10(df, "Bearish")
    out_r = f"/tmp/NSE_Bearish_{date_tag}.png"
    draw_image(top_bear, "Bearish", BEARISH_ACCENT, "-", report_date,
               bull_cnt, bear_cnt, neut_cnt, total_cnt, out_r)
    send_telegram(out_r,
        f"*NSE Indicator Engine — Bearish Trend*\n"
        f"Date: {report_date} | Bearish: {bear_cnt:,} / Total: {total_cnt:,}\n"
        f"Top {len(top_bear)} EQ symbols by signal count")

    # ── Neutral ───────────────────────────────────────────────────────────────
    top_neut = top10(df, "Neutral")
    out_n = f"/tmp/NSE_Neutral_{date_tag}.png"
    draw_image(top_neut, "Neutral", NEUTRAL_ACCENT, "~", report_date,
               bull_cnt, bear_cnt, neut_cnt, total_cnt, out_n)
    send_telegram(out_n,
        f"*NSE Indicator Engine — Neutral Trend*\n"
        f"Date: {report_date} | Neutral: {neut_cnt:,} / Total: {total_cnt:,}\n"
        f"Top {len(top_neut)} EQ symbols by signal count")

    print("[DONE] All 3 images generated and sent.")


if __name__ == "__main__":
    main()
