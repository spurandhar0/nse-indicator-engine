#!/usr/bin/env python3
"""
NSE Indicator Engine — Telegram Image Report
============================================
Reads latest NSE_Indicator_Report_*.csv, filters EQ series,
picks top 10 symbols per trend (Bullish/Bearish/Neutral) by signal count,
generates a styled dark-theme image, and sends to Telegram.

Columns used: Trend, NSE_SYMBOL, SERIES, CLOSE, PREV_CLOSE, Signal, Date
Schedule: Mon–Fri 9:00 PM IST (15:30 UTC)
"""

import os
import glob
import io
import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
from datetime import datetime
import pytz

# -------------------------────────────────────
# CONFIG
# -------------------------────────────────────
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")

TOP_N         = 10          # symbols per trend
MIN_CONF      = 70          # minimum ConfidenceScore to include

# ─── colour palette -------------------------──
BG_COLOR      = "#0d1117"
PANEL_BG      = "#161b22"
BORDER_COLOR  = "#30363d"
TEXT_COLOR    = "#e6edf3"
SUBTEXT_COLOR = "#8b949e"
BULLISH_CLR   = "#3fb950"   # green
BEARISH_CLR   = "#f85149"   # red
NEUTRAL_CLR   = "#58a6ff"   # blue
HEADER_CLR    = "#21262d"
UP_CLR        = "#3fb950"
DN_CLR        = "#f85149"
NEUTRAL_VAL   = "#8b949e"

# -------------------------────────────────────
# HELPERS
# -------------------------────────────────────

def find_latest_report():
    files = glob.glob("output_data/NSE_Indicator_Report_*.csv")
    if not files:
        raise FileNotFoundError("No NSE_Indicator_Report_*.csv found in output_data/")
    return max(files)


def load_and_prepare(path):
    df = pd.read_csv(path, dtype=str)

    # Normalise whitespace
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    # Filter EQ series & confidence
    df = df[df["SERIES"] == "EQ"].copy()
    df["ConfidenceScore"] = pd.to_numeric(df["ConfidenceScore"], errors="coerce").fillna(0)
    df = df[df["ConfidenceScore"] >= MIN_CONF].copy()

    # Numeric prices
    df["CLOSE"]      = pd.to_numeric(df["CLOSE"],      errors="coerce")
    df["PREV_CLOSE"] = pd.to_numeric(df["PREV_CLOSE"], errors="coerce")

    # Change %
    df["Change_Pct"] = ((df["CLOSE"] - df["PREV_CLOSE"]) / df["PREV_CLOSE"] * 100).round(2)

    # Normalise Trend
    df["Trend"] = df["Trend"].str.strip().str.capitalize()

    # Report date
    report_date = df["Date"].dropna().iloc[0] if len(df) > 0 else "N/A"

    return df, report_date


def top_symbols_for_trend(df, trend):
    """Return top-N symbols for a trend sorted by signal count desc."""
    tdf = df[df["Trend"] == trend].copy()
    if tdf.empty:
        return pd.DataFrame(columns=["NSE_SYMBOL", "Count", "CLOSE", "Change_Pct"])

    agg = (
        tdf.groupby("NSE_SYMBOL")
        .agg(
            Count      = ("Signal",     "count"),
            CLOSE      = ("CLOSE",      "first"),
            Change_Pct = ("Change_Pct", "first"),
        )
        .reset_index()
        .sort_values(["Count", "Change_Pct"], ascending=[False, False])
        .head(TOP_N)
        .reset_index(drop=True)
    )
    agg.index += 1   # 1-based rank
    return agg


# -------------------------────────────────────
# IMAGE GENERATION
# -------------------------────────────────────

TREND_META = {
    "Bullish": {"color": BULLISH_CLR, "emoji": "[+]", "label": "BULLISH TREND"},
    "Bearish": {"color": BEARISH_CLR, "emoji": "[-]", "label": "BEARISH TREND"},
    "Neutral": {"color": NEUTRAL_CLR,  "emoji": "[~]", "label": "NEUTRAL TREND"},
}


def draw_table(ax, data, trend):
    meta   = TREND_META[trend]
    color  = meta["color"]
    label  = meta["label"]

    ax.set_facecolor(PANEL_BG)
    ax.axis("off")

    # ── Section header -------------------------─
    ax.text(
        0.5, 1.02, f"{meta['emoji']}  {label}  ({len(data)} symbols shown)",
        transform=ax.transAxes, ha="center", va="bottom",
        fontsize=13, fontweight="bold", color=color,
        fontfamily="monospace"
    )

    if data.empty:
        ax.text(0.5, 0.5, "No data for this trend", transform=ax.transAxes,
                ha="center", va="center", color=SUBTEXT_COLOR, fontsize=11)
        return

    # ── Column headers -------------------------─
    col_labels = ["#", "NSE Symbol", "Signals", "Close Rs.", "Change %"]
    col_x      = [0.02, 0.10, 0.44, 0.62, 0.82]
    col_align  = ["left", "left", "center", "right", "right"]
    header_y   = 0.93

    for lbl, x, align in zip(col_labels, col_x, col_align):
        ax.text(x, header_y, lbl,
                transform=ax.transAxes, ha=align, va="top",
                fontsize=9.5, fontweight="bold", color=SUBTEXT_COLOR,
                fontfamily="monospace")

    # ── Separator line -------------------------─
    sep_y = header_y - 0.04
    ax.plot([0.01, 0.99], [sep_y, sep_y],
            color=color, linewidth=1.2,
            transform=ax.transAxes, clip_on=False)

    # ── Data rows -------------------------──────
    row_h   = 0.082
    start_y = header_y - 0.10

    for i, (rank, row) in enumerate(data.iterrows()):
        y = start_y - i * row_h

        # Alternating row shade
        if i % 2 == 0:
            rect = mpatches.FancyBboxPatch(
                (0.01, y - row_h * 0.55), 0.98, row_h * 0.92,
                boxstyle="round,pad=0.005",
                linewidth=0, facecolor="#1c2128",
                transform=ax.transAxes, clip_on=False
            )
            ax.add_patch(rect)

        # Rank
        ax.text(col_x[0], y, str(rank),
                transform=ax.transAxes, ha="left", va="center",
                fontsize=9, color=SUBTEXT_COLOR, fontfamily="monospace")

        # Symbol — bold + trend color
        ax.text(col_x[1], y, row["NSE_SYMBOL"],
                transform=ax.transAxes, ha="left", va="center",
                fontsize=10.5, fontweight="bold", color=color,
                fontfamily="monospace")

        # Signal count badge
        count_x  = col_x[2]
        badge_w  = 0.11
        badge_h  = row_h * 0.70
        badge_rx = count_x - badge_w / 2
        rect2 = mpatches.FancyBboxPatch(
            (badge_rx, y - badge_h / 2), badge_w, badge_h,
            boxstyle="round,pad=0.008",
            linewidth=1.0, edgecolor=color, facecolor=PANEL_BG,
            transform=ax.transAxes, clip_on=False
        )
        ax.add_patch(rect2)
        ax.text(count_x, y, str(int(row["Count"])),
                transform=ax.transAxes, ha="center", va="center",
                fontsize=9.5, fontweight="bold", color=color,
                fontfamily="monospace")

        # Close price
        close_str = f"Rs.{row['CLOSE']:,.2f}" if pd.notna(row["CLOSE"]) else "N/A"
        ax.text(col_x[3], y, close_str,
                transform=ax.transAxes, ha="right", va="center",
                fontsize=9.5, color=TEXT_COLOR, fontfamily="monospace")

        # Change %
        chg = row["Change_Pct"]
        if pd.isna(chg):
            chg_str = "N/A"
            chg_col = NEUTRAL_VAL
        elif chg > 0:
            chg_str = f"^ {chg:+.2f}%"
            chg_col = UP_CLR
        elif chg < 0:
            chg_str = f"v {chg:.2f}%"
            chg_col = DN_CLR
        else:
            chg_str = f"  {chg:.2f}%"
            chg_col = NEUTRAL_VAL

        ax.text(col_x[4], y, chg_str,
                transform=ax.transAxes, ha="right", va="center",
                fontsize=9.5, fontweight="bold", color=chg_col,
                fontfamily="monospace")

    # Bottom separator
    bottom_y = start_y - TOP_N * row_h + row_h * 0.1
    ax.plot([0.01, 0.99], [bottom_y, bottom_y],
            color=BORDER_COLOR, linewidth=0.8,
            transform=ax.transAxes, clip_on=False)


def generate_image(df, report_date, out_path="nse_report.png"):
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist).strftime("%d %b %Y  %I:%M %p IST")

    fig = plt.figure(figsize=(14, 22), facecolor=BG_COLOR)
    fig.patch.set_facecolor(BG_COLOR)

    # ── Main title -------------------------─────
    fig.text(
        0.5, 0.975,
        "NSE Indicator Engine -- Daily Signal Report",
        ha="center", va="top",
        fontsize=18, fontweight="bold", color=TEXT_COLOR,
        fontfamily="monospace"
    )
    fig.text(
        0.5, 0.960,
        f"Date: {report_date}   |   Series: EQ   |   Generated: {now_ist}",
        ha="center", va="top",
        fontsize=11, color=SUBTEXT_COLOR,
        fontfamily="monospace"
    )
    fig.text(
        0.5, 0.947,
        f"Top {TOP_N} symbols per trend  *  Ranked by signal count  *  Min confidence >= {MIN_CONF}",
        ha="center", va="top",
        fontsize=9.5, color=SUBTEXT_COLOR,
        fontfamily="monospace"
    )

    # ── Thin accent line under title ────────────
    line = plt.Line2D([0.05, 0.95], [0.938, 0.938],
                      transform=fig.transFigure, color=BORDER_COLOR, linewidth=1.5)
    fig.add_artist(line)

    # ── 3 panels (Bullish / Bearish / Neutral) ──
    gs = GridSpec(
        3, 1,
        figure=fig,
        top=0.928, bottom=0.03,
        hspace=0.22,
        left=0.04, right=0.97
    )

    trends = ["Bullish", "Bearish", "Neutral"]
    for i, trend in enumerate(trends):
        ax = fig.add_subplot(gs[i])
        data = top_symbols_for_trend(df, trend)
        draw_table(ax, data, trend)

        # Panel border
        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_edgecolor(TREND_META[trend]["color"])
            spine.set_linewidth(1.5)

    # ── Footer -------------------------─────────
    fig.text(
        0.5, 0.016,
        "* NSE Indicator Engine  |  MoneyControl + NSE BhavCopy  |  Not investment advice",
        ha="center", va="bottom",
        fontsize=8, color=SUBTEXT_COLOR,
        fontfamily="monospace"
    )

    plt.savefig(out_path, dpi=150, bbox_inches="tight",
                facecolor=BG_COLOR, edgecolor="none")
    plt.close(fig)
    print(f"[IMAGE] Saved → {out_path}")
    return out_path


# -------------------------────────────────────
# TELEGRAM
# -------------------------────────────────────

def send_to_telegram(image_path, report_date, df):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] TG_BOT_TOKEN or TG_CHAT_ID not set — skipping Telegram send")
        return False

    # Build summary counts
    summary_lines = []
    total_eq = len(df)
    for trend in ["Bullish", "Bearish", "Neutral"]:
        cnt = (df["Trend"] == trend).sum()
        meta = TREND_META[trend]
        summary_lines.append(f"{meta['emoji']} {trend}: {cnt:,} signals")

    caption = (
        f"[NSE] Daily Signal Report -- {report_date}\n"
        f"-------------------------\n"
        + "\n".join(summary_lines) +
        f"\n-------------------------\n"
        f"📌 *Top {TOP_N} EQ symbols per trend*\n"
        f"Ranked by signal count | Min confidence >= {MIN_CONF}\n\n"
        f"* NSE Indicator Engine"
    )

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = requests.post(url, data={
            "chat_id":    TG_CHAT_ID,
            "caption":    caption,
            "parse_mode": "Markdown",
        }, files={"photo": f}, timeout=30)

    if resp.status_code == 200:
        print(f"[TG] ✅ Image sent successfully to chat {TG_CHAT_ID}")
        return True
    else:
        print(f"[TG] ❌ Failed: {resp.status_code} — {resp.text[:300]}")
        return False


# -------------------------────────────────────
# MAIN
# -------------------------────────────────────

def main():
    print("[START] NSE Telegram Image Report")

    # 1. Find latest report
    report_path = find_latest_report()
    print(f"[DATA] Using: {report_path}")

    # 2. Load & prepare
    df, report_date = load_and_prepare(report_path)
    print(f"[DATA] Rows after EQ + confidence filter: {len(df):,}")
    for trend in ["Bullish", "Bearish", "Neutral"]:
        cnt = (df["Trend"] == trend).sum()
        print(f"       {trend}: {cnt:,}")

    # 3. Generate image
    out_path = f"/tmp/NSE_Report_{report_date.replace(' ', '_').replace('-', '_')}.png"
    generate_image(df, report_date, out_path)

    # 4. Send to Telegram
    send_to_telegram(out_path, report_date, df)

    print("[DONE] NSE Telegram Image Report complete")


if __name__ == "__main__":
    main()
