"""Fetch the latest publication-eligible NSE bhavcopy, not the wall-clock date.

The target is fixed once per invocation. Before 18:30 IST it belongs to an
earlier session, including when a delayed evening run crosses midnight.
Only a validated, nonempty CSV for that exact date is saved. Missing data
exits nonzero so workflow_run consumers cannot mistake failure for success.
"""
import csv
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone, time as clock_time
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
PUBLICATION_CUTOFF = clock_time(18, 30)
BHAV_ROOT = "bhav_data"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.nseindia.com/",
    "Accept": "*/*",
}


def publication_date(now, holidays=()):
    """Pure IST date selection; explicit holiday dates come from NSE CM calendar."""
    if now.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    local = now.astimezone(IST)
    target = local.date()
    if local.time() < PUBLICATION_CUTOFF:
        target -= timedelta(days=1)
    while target.weekday() >= 5 or target in holidays:
        target -= timedelta(days=1)
    return target


def trading_holidays(session, year):
    """Use official NSE CM holidays when available; do not infer a holiday from 404."""
    try:
        response = session.get(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers=HEADERS, timeout=20,
        )
        response.raise_for_status()
        entries = response.json()["CM"]
        days = {datetime.strptime(item["tradingDate"], "%d-%b-%Y").date()
                for item in entries}
        if not any(day.year == year for day in days):
            raise ValueError("Calendar does not cover target year")
        return days
    except (requests.RequestException, ValueError, KeyError, TypeError):
        print("WARNING: NSE holiday calendar unavailable. Using weekday eligibility; "
              "an unavailable holiday file will FAIL safely, not publish older data as current.")
        return set()


def validate_csv(content, target):
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), skipinitialspace=True)
    reader.fieldnames = [name.strip() for name in (reader.fieldnames or [])]
    required = {"SYMBOL", "SERIES", "DATE1", "CLOSE_PRICE", "TTL_TRD_QNTY"}
    if not required.issubset(reader.fieldnames):
        raise ValueError("Response is not a valid NSE bhavcopy CSV")
    count = 0
    for row in reader:
        if not any(row.values()):
            continue
        if None in row or not row.get("SYMBOL", "").strip():
            raise ValueError("Malformed bhavcopy row")
        actual = datetime.strptime(row.get("DATE1", "").strip(), "%d-%b-%Y").date()
        if actual != target:
            raise ValueError(f"CSV trading date {actual} differs from target {target}")
        float(row["CLOSE_PRICE"].strip())
        int(row["TTL_TRD_QNTY"].strip())
        count += 1
    if not count:
        raise ValueError("Bhavcopy CSV contains no data rows")
    return count


def tg_msg(text):
    token, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=15)
    except requests.RequestException:
        print("WARNING: Telegram notification unavailable")


def tg_send(filepath, caption):
    token, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        return
    try:
        with open(filepath, "rb") as handle:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat, "caption": caption},
                files={"document": handle}, timeout=120,
            )
        if not response.ok or not response.json().get("ok"):
            print("WARNING: Telegram did not accept document")
        else:
            print("Sent to Telegram")
    except (requests.RequestException, ValueError):
        print("WARNING: Telegram document unavailable; validated CSV remains saved")


def fetch_bhav(now=None, session=None, holidays=None, sleep=time.sleep, root=BHAV_ROOT):
    now = now or datetime.now(IST)
    session = session or requests.Session()
    provisional = publication_date(now)
    if holidays is None:
        holidays = trading_holidays(session, provisional.year)
    target = publication_date(now, holidays)
    print(f"Run time: {now.astimezone(IST):%d-%b-%Y %H:%M IST}")
    print(f"Fixed bhavcopy target: {target:%d-%b-%Y} (publication cutoff 18:30 IST)")
    path = Path(root) / target.strftime("%b-%Y") / f"sec_bhavdata_full_{target:%d%m%Y}.csv"
    if path.exists():
        try:
            rows = validate_csv(path.read_bytes(), target)
            print(f"Already validated: {path} ({rows:,} rows); no duplicate download or Telegram.")
            return path
        except (ValueError, UnicodeError, TypeError):
            print("WARNING: Existing target file is invalid; fetching replacement")

    # Cookie bootstrap is best-effort: archive downloads can work without it.
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=20)
    except requests.RequestException:
        print("WARNING: Homepage cookie request failed; trying archive directly")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{target:%d%m%Y}.csv"
    last_error = "Unknown download failure"
    for attempt, delay in enumerate((0, 30, 90), start=1):
        if delay:
            sleep(delay)
        try:
            print(f"Attempt {attempt}/3: {url}")
            response = session.get(url, headers=HEADERS, timeout=60)
            if response.status_code != 200:
                raise ValueError(f"Archive HTTP {response.status_code}")
            rows = validate_csv(response.content, target)
        except (requests.RequestException, ValueError, UnicodeError, TypeError) as exc:
            # Never print request exceptions: they can contain credential-bearing URLs.
            last_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            print(f"WARNING: Attempt {attempt}/3 failed: {last_error}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".csv.tmp")
        temporary.write_bytes(response.content)
        os.replace(temporary, path)
        print(f"Saved validated bhavcopy: {path} ({rows:,} rows)")
        tg_send(path, f"NSE Bhavcopy Downloaded\nTrading date: {target:%d-%b-%Y}\n"
                      f"Rows: {rows:,}\nSaved: {path}\nReady for NSE Engine")
        return path
    raise RuntimeError(f"NSE bhavcopy {target:%d-%b-%Y} unavailable after 3 attempts: "
                       f"{last_error}. No success reported; downstream reports must not run.")


def main():
    try:
        fetch_bhav()
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        print(f"ERROR: {message}", file=sys.stderr)
        tg_msg(f"NSE Bhavcopy FAILED\n{message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
