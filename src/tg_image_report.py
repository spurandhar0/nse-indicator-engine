"""
tg_image_report.py — NSE Indicator Engine
Generates 3 separate professional white-background images (Bullish / Bearish / Neutral)
each showing Top 10 EQ Series NSE Symbols ranked by signal count.
Sends all 3 images to Telegram.
"""

import os
import sys
import glob
import warnings
import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import matplotlib.gridspec as gridspec
from datetime import datetime
import pytz

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
OUTPUT_DIR   = os.environ.get("OUTPUT_DIR", "output_data")
IST          = pytz.timezone("Asia/Kolkata")

# ── Trend styling ─────────────────────────────────────────────────────────────
TREND_CONFIG = {
    "Bullish": {
        "accent":     "#1a7a4a",   # deep green
        "header_bg":  "#e8f5ee",   # soft green tint
        "row_even":   "#f5fdf8",
        "row_odd":    "#ffffff",
        "badge_bg":   "#1a7a4a",
        "badge_fg":   "#ffffff",
        "chg_pos":    "#1a7a4a",
        "chg_neg":    "#c0392b",
        "arrow_up":   "▲",
        "arrow_dn":   "▼",
        "label":      "BULLISH TREND",
        "emoji":      "BULLISH",
        "icon_char":  "+",
        "icon_color": "#1a7a4a",
    },
    "Bearish": {
        "accent":     "#c0392b",
        "header_bg":  "#fdf0ee",
        "row_even":   "#fff8f7",
        "row_odd":    "#ffffff",
        "badge_bg":   "#c0392b",
        "badge_fg":   "#ffffff",
        "chg_pos":    "#1a7a4a",
        "chg_neg":    "#c0392b",
        "arrow_up":   "▲",
        "arrow_dn":   "▼",
        "label":      "BEARISH TREND",
        "emoji":      "BEARISH",
        "icon_char":  "-",
        "icon_color": "#c0392b",
    },
    "Neutral": {
        "accent":     "#2471a3",
        "header_bg":  "#eaf2fb",
        "row_even":   "#f4f9fe",
        "row_odd":    "#ffffff",
        "badge_bg":   "#2471a3",
        "badge_fg":   "#ffffff",
        "chg_pos":    "#1a7a4a",
        "chg_neg":    "#c0392b",
        "arrow_up":   "▲",
        "arrow_dn":   "▼",
        "label":      "NEUTRAL TREND",
        "emoji":      "NEUTRAL",
        "icon_char":  "~",
        "icon_color": "#2471a3",
    },
}

# ── Column widths (fractions of 0..1) ─────────────────────────────────────────
COL_X      = [0.04, 0.12, 0.52, 0.68, 0.85]
COL_W      = ["#", "NSE Symbol", "Scanner / Signal", "Close (Rs.)", "Change %"]
COL_ALIGN  = ["center", "left", "left", "right", "right"]
COL_WIDTH  = [0.06, 0.20, 0.35, 0.18, 0.17]   # approx widths for fill rects


def load_report():
    files = sorted(glob.glob(f"{OUTPUT_DIR}/NSE_Indicator_Report_*.csv"))
    if not files:
        print(f"[ERROR] No report CSV found in {OUTPUT_DIR}/")
        sys.exit(1)
    path = files[-1]
    print(f"[DATA] Loading: {path}")
    df = pd.read_csv(path, dtype=str)
    return df, path


def prepare_top10(df, trend):
    """Return top-10 EQ symbols for a given trend, ranked by signal count."""
    t = df.copy()
    t = t[t["SERIES"].str.upper().str.strip() == "EQ"]
    t = t[t["Trend"].str.strip() == trend]
    t["NSE_SYMBOL"] = t["NSE_SYMBOL"].str.strip()
    t["CLOSE"]      = pd.to_numeric(t["CLOSE"], errors="coerce")
    t["PREV_CLOSE"] = pd.to_numeric(t["PREV_CLOSE"], errors="coerce")
    t["Change_Pct"] = ((t["CLOSE"] - t["PREV_CLOSE"]) / t["PREV_CLOSE"] * 100).round(2)

    # Rank by signal frequency per symbol
    counts = t.groupby("NSE_SYMBOL").size().rename("SigCount")
    agg = (
        t.groupby("NSE_SYMBOL")
         .agg(
             Close=("CLOSE", "last"),
             Change_Pct=("Change_Pct", "last"),
             Signal=("Signal", lambda x: x.iloc[0] if len(x) else ""),
         )
         .join(counts)
         .sort_values("SigCount", ascending=False)
         .head(10)
         .reset_index()
    )
    return agg


def get_report_date(filepath):
    """Extract date string from filename."""
    base = os.path.basename(filepath)   # NSE_Indicator_Report_08-Apr-2026.csv
    parts = base.replace(".csv", "").split("_")
    return parts[-1] if parts else datetime.now(IST).strftime("%d-%b-%Y")


def abbreviate_signal(sig, max_len=38):
    """Truncate signal text cleanly."""
    if not isinstance(sig, str):
        return ""
    sig = sig.strip()
    return sig[:max_len] + "..." if len(sig) > max_len else sig


def draw_trend_image(df, trend, report_date, out_path):
    cfg  = TREND_CONFIG[trend]
    rows = prepare_top10(df, trend)
    total_count = len(df[(df["SERIES"].str.upper().str.strip() == "EQ") &
                         (df["Trend"].str.strip() == trend)])

    # ── Canvas ────────────────────────────────────────────────────────────────
    fig_w, fig_h = 11, 7.6
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor="white")
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor("white")

    # Outer card shadow (subtle)
    shadow = FancyBboxPatch((0.012, 0.008), 0.976, 0.982,
                             boxstyle="round,pad=0.01",
                             linewidth=0, facecolor="#e0e0e0", zorder=0)
    ax.add_patch(shadow)

    # Main white card
    card = FancyBboxPatch((0.010, 0.012), 0.980, 0.978,
                           boxstyle="round,pad=0.01",
                           linewidth=1.2, edgecolor=cfg["accent"],
                           facecolor="white", zorder=1)
    ax.add_patch(card)

    # Top accent bar
    ax.add_patch(plt.Rectangle((0.010, 0.878), 0.980, 0.112,
                                facecolor=cfg["accent"], zorder=2,
                                transform=ax.transData, clip_on=False))
    # Round top corners of accent bar via card already drawn — just paint over
    top_card = FancyBboxPatch((0.010, 0.878), 0.980, 0.112,
                               boxstyle="round,pad=0.01",
                               linewidth=0, facecolor=cfg["accent"], zorder=2)
    ax.add_patch(top_card)

    # ── Header text ───────────────────────────────────────────────────────────
    # Icon circle
    circ = plt.Circle((0.072, 0.929), 0.038, color="white", zorder=3)
    ax.add_patch(circ)
    ax.text(0.072, 0.929, cfg["icon_char"],
            ha="center", va="center", fontsize=22, fontweight="bold",
            color=cfg["accent"], zorder=4)

    # Trend title
    ax.text(0.135, 0.942, cfg["label"],
            ha="left", va="center", fontsize=18, fontweight="bold",
            color="white", zorder=3)

    # Sub-line: date + total signals
    ax.text(0.135, 0.910, f"Date: {report_date}   |   Total EQ Signals: {total_count:,}",
            ha="left", va="center", fontsize=10, color="#dce9ff", zorder=3)

    # Watermark right
    ax.text(0.965, 0.929, "NSE Indicator Engine",
            ha="right", va="center", fontsize=9, color="white",
            style="italic", alpha=0.85, zorder=3)

    # ── Column headers ────────────────────────────────────────────────────────
    header_y_top = 0.868
    header_h     = 0.052
    ax.add_patch(plt.Rectangle((0.020, header_y_top), 0.960, header_h,
                                facecolor=cfg["header_bg"], zorder=2))
    # Bottom border of header
    ax.plot([0.020, 0.980], [header_y_top, header_y_top],
            color=cfg["accent"], lw=1.2, zorder=3)

    header_labels = ["#", "NSE Symbol", "Scanner / Signal", "Close (Rs.)", "Change %"]
    col_x         = [0.040, 0.105, 0.385, 0.745, 0.910]
    col_align     = ["center", "left", "left", "right", "right"]
    header_mid_y  = header_y_top + header_h / 2

    for lbl, cx, align in zip(header_labels, col_x, col_align):
        ax.text(cx, header_mid_y, lbl,
                ha=align, va="center", fontsize=9.5, fontweight="bold",
                color=cfg["accent"], zorder=3)

    # ── Data rows ─────────────────────────────────────────────────────────────
    row_h   = 0.068
    start_y = header_y_top - row_h  # top of first data row

    for i, rec in rows.iterrows():
        ry = start_y - i * row_h
        bg = cfg["row_even"] if i % 2 == 0 else cfg["row_odd"]
        ax.add_patch(plt.Rectangle((0.020, ry), 0.960, row_h,
                                    facecolor=bg, zorder=2))
        # Thin separator
        ax.plot([0.020, 0.980], [ry, ry],
                color="#e8e8e8", lw=0.6, zorder=3)

        mid_y = ry + row_h / 2

        # Rank badge
        ax.add_patch(FancyBboxPatch((0.026, ry + 0.012), 0.030, row_h - 0.024,
                                     boxstyle="round,pad=0.003",
                                     linewidth=0, facecolor=cfg["badge_bg"],
                                     alpha=0.15, zorder=3))
        ax.text(0.040, mid_y, str(i + 1),
                ha="center", va="center", fontsize=9, fontweight="bold",
                color=cfg["accent"], zorder=4)

        # NSE Symbol (bold)
        sym = str(rec["NSE_SYMBOL"]) if pd.notna(rec["NSE_SYMBOL"]) else "—"
        ax.text(0.105, mid_y, sym,
                ha="left", va="center", fontsize=10.5, fontweight="bold",
                color="#1a1a2e", zorder=4)

        # Signal count pill right of symbol
        sig_cnt = int(rec["SigCount"])
        pill_x  = 0.105 + len(sym) * 0.0078 + 0.018
        ax.add_patch(FancyBboxPatch((pill_x, mid_y - 0.013), 0.052, 0.026,
                                     boxstyle="round,pad=0.003",
                                     linewidth=0.8, edgecolor=cfg["accent"],
                                     facecolor=cfg["badge_bg"], alpha=0.15, zorder=3))
        ax.text(pill_x + 0.026, mid_y, f"{sig_cnt}",
                ha="center", va="center", fontsize=7.5, fontweight="bold",
                color=cfg["accent"], zorder=4)

        # Scanner / Signal text
        sig_text = abbreviate_signal(str(rec["Signal"]))
        ax.text(0.230, mid_y, sig_text,
                ha="left", va="center", fontsize=8.0,
                color="#555555", zorder=4)

        # Close price
        close_val = rec["Close"]
        close_str = f"{close_val:,.2f}" if pd.notna(close_val) else "—"
        ax.text(0.745, mid_y, close_str,
                ha="right", va="center", fontsize=9.5, fontweight="bold",
                color="#1a1a2e", zorder=4)

        # Change %
        chg = rec["Change_Pct"]
        if pd.notna(chg):
            chg_str   = f"{chg:+.2f}%"
            chg_color = cfg["chg_pos"] if chg >= 0 else cfg["chg_neg"]
            arrow     = cfg["arrow_up"] if chg >= 0 else cfg["arrow_dn"]
            # Color pill for change
            pill_color = "#e8f5ee" if chg >= 0 else "#fdf0ee"
            pill_border = cfg["chg_pos"] if chg >= 0 else cfg["chg_neg"]
            ax.add_patch(FancyBboxPatch((0.850, mid_y - 0.016), 0.110, 0.032,
                                         boxstyle="round,pad=0.003",
                                         linewidth=0.8, edgecolor=pill_border,
                                         facecolor=pill_color, zorder=3))
            ax.text(0.910, mid_y, f"{arrow} {chg_str}",
                    ha="center", va="center", fontsize=8.5, fontweight="bold",
                    color=chg_color, zorder=4)
        else:
            ax.text(0.910, mid_y, "—",
                    ha="center", va="center", fontsize=9, color="#999999", zorder=4)

    # ── Footer ────────────────────────────────────────────────────────────────
    footer_y = 0.030
    ax.plot([0.020, 0.980], [footer_y + 0.022, footer_y + 0.022],
            color="#e0e0e0", lw=0.8, zorder=3)
    gen_time = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
    ax.text(0.030, footer_y, f"Generated: {gen_time}",
            ha="left", va="bottom", fontsize=7.5, color="#aaaaaa", zorder=3)
    ax.text(0.970, footer_y, "Data: MoneyControl + NSE BhavCopy   |   Series: EQ",
            ha="right", va="bottom", fontsize=7.5, color="#aaaaaa", zorder=3)

    plt.savefig(out_path, dpi=180, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    print(f"[IMAGE] Saved -> {out_path}")


def send_telegram(image_path, caption):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[TG] TG_BOT_TOKEN or TG_CHAT_ID not set — skipping Telegram send")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": TG_CHAT_ID, "caption": caption,
                                         "parse_mode": "HTML"}, files={"photo": f}, timeout=30)
    if resp.status_code == 200:
        print(f"[TG] Sent: {os.path.basename(image_path)}")
    else:
        print(f"[TG] FAILED ({resp.status_code}): {resp.text[:200]}")


def main():
    print("[START] NSE Telegram Image Report — 3 separate images")
    df, report_path = load_report()
    report_date     = get_report_date(report_path)

    # Normalize columns
    df.columns = [c.strip() for c in df.columns]
    for col in ["SERIES", "Trend", "NSE_SYMBOL", "CLOSE", "PREV_CLOSE", "Signal"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    print(f"[DATA] Total rows: {len(df):,}")
    eq = df[df["SERIES"].str.upper() == "EQ"]
    print(f"[DATA] EQ rows: {len(eq):,}")
    for trend in ["Bullish", "Bearish", "Neutral"]:
        cnt = len(eq[eq["Trend"] == trend])
        print(f"       {trend}: {cnt:,}")

    # Generate 3 images + send each
    for trend in ["Bullish", "Bearish", "Neutral"]:
        safe_date = report_date.replace(" ", "_").replace("-", "_")
        out_path  = f"/tmp/NSE_{trend}_{safe_date}.png"
        draw_trend_image(df, trend, report_date, out_path)

        caption = (
            f"<b>NSE Indicator Engine</b>\n"
            f"<b>{trend.upper()} TREND — Top 10 EQ Signals</b>\n"
            f"Date: {report_date}\n"
            f"Series: EQ  |  Ranked by Signal Count"
        )
        send_telegram(out_path, caption)

    print("[DONE] All 3 images generated and sent.")


if __name__ == "__main__":
    main()
