"""
NSE Bhavcopy Fetcher -> GitHub Storage + Telegram
=================================================
Downloads the NSE EQ bhavcopy for today and saves it to:
  - bhav_data/Mon-YYYY/sec_bhavdata_full_DDMMYYYY.csv  (repo - auto-committed)
  - Sends the file to Telegram
Runs via GitHub Actions every weekday at 7:15 PM IST.
Tokens stored as GitHub Secrets - never hardcoded here.
"""

import os, requests
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID   = os.environ["TG_CHAT_ID"]

IST     = timezone(timedelta(hours=5, minutes=30))
NOW_IST = datetime.now(tz=IST)

BHAV_ROOT = "bhav_data"

# ─── TELEGRAM HELPERS ────────────────────────────────────────────────────────

def tg_send(filepath, caption):
    try:
        with open(filepath, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"document": f}, timeout=120
            )
        print("✅ Sent to Telegram" if r.json().get("ok") else f"⚠️ {r.json()}")
    except Exception as e:
        print(f"❌ tg_send error: {e}")

def tg_msg(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}, timeout=15
        )
    except Exception as e:
        print(f"TG msg error: {e}")

# ─── MAIN FETCH ───────────────────────────────────────────────────────────────

def fetch_bhav():
    dd   = NOW_IST.strftime("%d")
    mm   = NOW_IST.strftime("%m")
    yyyy = NOW_IST.strftime("%Y")
    mon3 = NOW_IST.strftime("%b")   # Apr

    # NSE direct CSV URL (no zip needed)
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dd}{mm}{yyyy}.csv"

    # Save path: bhav_data/Apr-2026/sec_bhavdata_full_08042026.csv
    month_folder = os.path.join(BHAV_ROOT, f"{mon3}-{yyyy}")
    os.makedirs(month_folder, exist_ok=True)
    fname    = f"sec_bhavdata_full_{dd}{mm}{yyyy}.csv"
    savepath = os.path.join(month_folder, fname)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer":    "https://www.nseindia.com",
        "Accept":     "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # NSE requires a session cookie - get homepage first
    session = requests.Session()
    print("Getting NSE session cookies...")
    session.get("https://www.nseindia.com", headers=headers, timeout=20)

    print(f"Downloading bhav: {url}")
    resp = session.get(url, headers=headers, timeout=60)

    if resp.status_code != 200:
        msg = (
            f"❌ NSE Bhavcopy FAILED\n"
            f"Date: {dd}-{mon3}-{yyyy}\n"
            f"HTTP Status: {resp.status_code}\n"
            f"Reason: NSE may not have published today's bhavcopy yet.\n"
            f"The file is usually available after 6:30 PM IST."
        )
        print(msg)
        tg_msg(msg)
        return

    # Save to repo folder (auto-committed by workflow)
    with open(savepath, "wb") as f:
        f.write(resp.content)

    size_kb = os.path.getsize(savepath) / 1024
    print(f"✅ Saved: {savepath} ({size_kb:.1f} KB)")

    # Count data rows
    with open(savepath, "r") as f:
        row_count = sum(1 for _ in f) - 1  # minus header row

    caption = (
        f"📥 *NSE Bhavcopy Downloaded*\n"
        f"📅 Date: *{dd}-{mon3}-{yyyy}*\n"
        f"📊 Total Rows: {row_count:,}\n"
        f"💾 Saved to: `bhav_data/{mon3}-{yyyy}/{fname}`\n"
        f"✅ Ready for NSE Engine at 8:00 PM IST"
    )

    tg_send(savepath, caption)
    print(f"Done. {row_count:,} rows saved.")


if __name__ == "__main__":
    print("=" * 50)
    print(f"NSE Bhavcopy Fetch | {NOW_IST.strftime('%d-%b-%Y %H:%M IST')}")
    print("=" * 50)
    fetch_bhav()
