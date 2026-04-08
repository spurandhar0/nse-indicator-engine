"""
NSE Full Bhavcopy & Security Deliverable Data Downloader
Downloads sec_bhavdata_full_DDMMYYYY.csv and sends to Telegram
Runs Mon-Fri at 19:15 IST via GitHub Actions
"""

import os
import sys
import requests
from datetime import datetime
import pytz

# ─── CONFIG (from GitHub Secrets) ───────────────────────────────────────────
TG_BOT_TOKEN = os.environ['TG_BOT_TOKEN']
TG_CHAT_ID   = os.environ['TG_CHAT_ID']

# ─── DATE SETUP (IST) ────────────────────────────────────────────────────────
IST = pytz.timezone('Asia/Kolkata')
today = datetime.now(IST)
dd   = today.strftime('%d')
mm   = today.strftime('%m')
yyyy = today.strftime('%Y')
ddmmyyyy   = f"{dd}{mm}{yyyy}"
date_label = today.strftime('%d-%b-%Y')  # e.g. 02-Apr-2026

filename = f"sec_bhavdata_full_{ddmmyyyy}.csv"
url = f"https://nsearchives.nseindia.com/products/content/{filename}"

print(f"📅 Date: {date_label}")
print(f"📄 File: {filename}")
print(f"🔗 URL : {url}")

# ─── TELEGRAM HELPERS ────────────────────────────────────────────────────────
def tg_send_message(text):
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'},
        timeout=30
    )

def tg_send_file(filepath, caption):
    with open(filepath, 'rb') as f:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument",
            data={'chat_id': TG_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'},
            files={'document': f},
            timeout=120
        )

# ─── DOWNLOAD FILE ───────────────────────────────────────────────────────────
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Referer': 'https://www.nseindia.com/',
}

session = requests.Session()

print("🌐 Fetching NSE session cookies...")
try:
    session.get('https://www.nseindia.com', headers=headers, timeout=15)
    session.get('https://www.nseindia.com/all-reports', headers=headers, timeout=15)
except Exception as e:
    print(f"⚠️  Cookie fetch warning: {e}")

print(f"⬇️  Downloading {filename}...")
try:
    resp = session.get(url, headers=headers, timeout=60)
    
    if resp.status_code == 200 and len(resp.content) > 1000:
        # Save file
        local_path = f"/tmp/{filename}"
        with open(local_path, 'wb') as f:
            f.write(resp.content)
        
        size_kb = len(resp.content) / 1024
        # Count rows (subtract header)
        row_count = len(resp.text.strip().split('\n')) - 1
        
        print(f"✅ Downloaded: {size_kb:.1f} KB, {row_count:,} records")
        
        # Send to Telegram
        caption = (
            f"📊 *NSE Full Bhavcopy & Delivery Data*\n"
            f"📅 Date: *{date_label}*\n"
            f"📄 File: `{filename}`\n"
            f"📦 Size: {size_kb:.1f} KB\n"
            f"📈 Records: {row_count:,} securities\n"
            f"🕐 Fetched at: {today.strftime('%H:%M IST')}"
        )
        
        print("📤 Sending to Telegram...")
        tg_send_file(local_path, caption)
        print("✅ Successfully sent to Telegram!")

    # ── SAVE TO REPO ──────────────────────────────────────────
month_folder = os.path.join('bhav_data', today.strftime('%b-%Y'))  # Apr-2026
os.makedirs(month_folder, exist_ok=True)
repo_path = os.path.join(month_folder, filename)
import shutil
shutil.copy(localpath, repo_path)
print(f"Saved to repo: {repo_path}")
# ──────────────────────────────────────────────────────────
        
    elif resp.status_code == 404:
        # File not available — likely market holiday or file not yet published
        msg = (
            f"📅 NSE Bhavcopy — {date_label}\n\n"
            f"⚠️ File `{filename}` not found (404).\n"
            f"Likely a market holiday or file not yet published.\n"
            f"No data to send today."
        )
        print(f"⚠️  File not found (404) — market holiday or not yet published")
        tg_send_message(msg)
        sys.exit(0)
        
    else:
        msg = (
            f"❌ NSE Bhavcopy Download Failed — {date_label}\n\n"
            f"File: `{filename}`\n"
            f"HTTP Status: {resp.status_code}\n"
            f"Please check https://www.nseindia.com/all-reports manually."
        )
        print(f"❌ Download failed: HTTP {resp.status_code}")
        tg_send_message(msg)
        sys.exit(1)

except requests.exceptions.Timeout:
    msg = f"❌ NSE Bhavcopy — Timeout downloading {filename} on {date_label}"
    print("❌ Timeout!")
    tg_send_message(msg)
    sys.exit(1)

except Exception as e:
    msg = f"❌ NSE Bhavcopy Error on {date_label}: {str(e)}"
    print(f"❌ Error: {e}")
    tg_send_message(msg)
    sys.exit(1)
