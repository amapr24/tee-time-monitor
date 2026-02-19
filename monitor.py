"""
Miami Lakes Tee Time Monitor
Checks CPS Golf for available tee times on specified days
and sends an email AND push notification when new slots appear.

Setup:
  pip install playwright requests
  playwright install chromium
"""

import asyncio
import json
import os
import smtplib
import requests  # Added for Pushover
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

# ── Configuration ────────────────────────────────────────────────────────────

# Golf course base URL
BASE_URL = "https://miamilakes.cps.golf/onlineresweb/search-teetime"

# Days to monitor: 0=Monday, ... 4=Friday, 5=Saturday, 6=Sunday
DAYS_TO_MONITOR = [4, 5, 6]

# Tee time window: 0=Midnight, 23=11 PM. 
# UPDATED to 23 so you see the 3 PM slots.
TEE_TIME_MIN = 0      
TEE_TIME_MAX = 23     

# Email configuration
SMTP_SERVER    = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")

# Pushover Configuration (New)
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

# Cache file
CACHE_FILE     = Path("last_teetimes.json")

# Day name mapping
DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 
             4: "Friday", 5: "Saturday", 6: "Sunday"}


# ── Notifications ────────────────────────────────────────────────────────────

def send_push_notification(title, message):
    """Sends a high-priority notification via Pushover."""
    if PUSHOVER_USER and PUSHOVER_TOKEN:
        try:
            payload = {
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title,
                "message": message,
                "priority": 1 
            }
            requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
            print("  ✅ Push notification sent.")
        except Exception as e:
            print(f"  ❌ Pushover Error: {e}")

def send_email(subject, body):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        print("  ⚠ Email credentials not configured — printing alert to console.")
        return
    
    recipients = [email.strip() for email in EMAIL_TO.split(',')]
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"  ✅ Email sent to {len(recipients)} recipient(s).")
    except Exception as e:
        print(f"  ❌ Email Error: {e}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_next_occurrences(days_to_check: list[int], num_weeks: int = 4) -> list[date]:
    today = date.today()
    target_dates = []
    for i in range(1, num_weeks * 7 + 1):
        check_date = today + timedelta(days=i)
        if check_date.weekday() in days_to_check:
            target_dates.append(check_date)
    return target_dates


def is_within_time_window(time_str: str) -> bool:
    try:
        time_part = time_str.split()[0]
        am_pm = time_str.split()[1] if len(time_str.split()) > 1 else "AM"
        hour, minute = map(int, time_part.split(":"))
        
        if am_pm.upper() == "PM" and hour != 12: hour += 12
        elif am_pm.upper() == "AM" and hour == 12: hour = 0
        
        return TEE_TIME_MIN <= hour <= TEE_TIME_MAX
    except Exception:
        return True


def deduplicate_slots(slots: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for slot in slots:
        time = slot.get("time", "").strip().upper()
        holes = slot.get("holes", "").strip().upper()
        
        if not holes: continue
        if not is_within_time_window(slot.get("time", "")): continue
        
        key = (time, holes)
        if key not in seen:
            seen.add(key)
            deduped.append(slot)
    return deduped


def get_cache_key(target_date: date) -> str:
    return f"teetimes_{target_date.isoformat()}"


def load_cache(target_date: date) -> list[dict]:
    try:
        if CACHE_FILE.exists():
            all_cache = json.loads(CACHE_FILE.read_text())
            return all_cache.get(get_cache_key(target_date), [])
    except Exception:
        pass
    return []


def save_cache(target_date: date, data: list[dict]):
    try:
        all_cache = {}
        if CACHE_FILE.exists():
            try:
                content = CACHE_FILE.read_text().strip()
                if content:
                    all_cache = json.loads(content)
                    if isinstance(all_cache, list): all_cache = {}
            except Exception:
                all_cache = {}
        
        key = get_cache_key(target_date)
        all_cache[key] = data
        
        with open(CACHE_FILE, 'w') as f:
            json.dump(all_cache, f, indent=2)
        print(f"  ✅ Cache saved for {target_date.strftime('%A, %B %-d')}")
    except Exception as e:
        print(f"  ⚠ Error saving cache: {e}")


def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]


# ── Core Scraper (Your proven working logic) ─────────────────────────────────

async def select_day_and_scrape(target_date: date) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        # Build full URL with time parameters
        url = f"{BASE_URL}?TeeOffTimeMin={TEE_TIME_MIN}&TeeOffTimeMax={TEE_TIME_MAX}"
        
        print(f"  Loading page...")
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

        # ── Step 1: Navigate to the correct month ────────────────────────────
        target_month_str = target_date.strftime("%B %Y")
        print(f"  Looking for month: {target_month_str}")

        for attempt in range(12):
            header = await page.evaluate("""
                () => {
                    const pat = /^[A-Za-z]+ \\d{4}$/;
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        const t = node.textContent.trim();
                        if (pat.test(t)) return t;
                    }
                    return '';
                }
            """)
            header
