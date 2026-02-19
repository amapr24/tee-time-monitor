"""
Miami Lakes Tee Time Monitor
Checks CPS Golf for available tee times on specified days
and sends Email + Pushover alerts when new slots appear.
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

BASE_URL = "https://miamilakes.cps.golf/onlineresweb/search-teetime"
DAYS_TO_MONITOR = [4, 5, 6]
TEE_TIME_MIN = 0      
TEE_TIME_MAX = 23     # Increased to 23 to ensure afternoon slots are seen

# Notification configuration
SMTP_SERVER    = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")

# Pushover configuration
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE     = Path("last_teetimes.json")
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
            print("  ✅ Pushover notification sent.")
        except Exception as e:
            print(f"  ❌ Pushover Error: {e}")

def send_email(subject, body):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        print(f"  ⚠ Email not configured. Console output:\n{body}")
        return
    recipients = [email.strip() for email in EMAIL_TO.split(',')]
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)
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
        parts = time_str.split()
        time_part = parts[0]
        am_pm = parts[1] if len(parts) > 1 else "AM"
        hour = int(time_part.split(":")[0])
        if am_pm.upper() == "PM" and hour != 12: hour += 12
        elif am_pm.upper() == "AM" and hour == 12: hour = 0
        return TEE_TIME_MIN <= hour <= TEE_TIME_MAX
    except: return True

def deduplicate_slots(slots: list[dict]) -> list[dict]:
    seen, deduped = set(), []
    for slot in slots:
        time = slot.get("time", "").strip().upper()
        holes = slot.get("holes", "").strip().upper()
        if not holes or not is_within_time_window(slot.get("time", "")): continue
        if (time, holes) not in seen:
            seen.add((time, holes))
            deduped.append(slot)
    return deduped

def get_cache_key(target_date: date) -> str:
    return f"teetimes_{target_date.isoformat()}"

def load_cache(target_date: date) -> list[dict]:
    if CACHE_FILE.exists():
        try:
            all_cache = json.loads(CACHE_FILE.read_text())
            return all_cache.get(get_cache_key(target_date), [])
        except: pass
    return []

def save_cache(target_date: date, data: list[dict]):
    all_cache = {}
    if CACHE_FILE.exists():
        try:
            content = CACHE_FILE.read_text().strip()
            if content: 
                all_cache = json.loads(content)
                if isinstance(all_cache, list): all_cache = {}
        except: pass
    all_cache[get_cache_key(target_date)] = data
    with open(CACHE_FILE, 'w') as f:
        json.dump(all_cache, f, indent=2)

def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]

# ── Scraper ──────────────────────────────────────────────────────────────────

async def select_day_and_scrape(target_date: date) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        url = f"{BASE_URL}?TeeOffTimeMin={TEE_TIME_MIN}&TeeOffTimeMax={TEE_TIME_MAX}"
        
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        # Nav Month
        target_month_str = target_date.strftime("%B %Y")
        for _ in range(12):
            header = await page.evaluate("() => Array.from(document.querySelectorAll('*')).map(e => e.textContent).find(t => /^[A-Za-z]+ \\d{4}$/.test(t.trim()))")
            if target_month_str in (header or ""): break
            await page.evaluate("() => { for (const el of document.querySelectorAll('*')) { if (['›','>','▶'].includes(el.innerText.trim())) { el.click(); return true; } } return false; }")
            await page.wait_for_timeout(800)

        # Click Day
        day_num = str(target_date.day)
        await page.evaluate(f"() => {{ const tags = document.querySelectorAll('span, div, button'); for (const el of tags) {{ if (el.innerText.trim() === '{day_num}' && !el.className.includes('muted')) {{ el.click(); return; }} }} }}")
        await page.wait_for_timeout(4000)

        # Extract
        tee_times = await page.evaluate("""() => {
            const results = [];
            const cards = document.querySelectorAll('[class*="tee-time"], [class*="teetime"], .timeslot-item');
            cards.forEach(card => {
                const text = card.innerText.replace(/\\s+/g, ' ');
                const timeMatch = text.match(/(\\d{1,2}:\\d{2})\\s*(AM|PM)/i);
                if (timeMatch) {
                    results.append({
                        time: timeMatch[0].toUpperCase(),
                        holes: text.match(/\\d+\\s*HOLE/i)?.[0] || '',
                        price: text.match(/\\$[\\d.]+/)?.[0] || ''
                    });
                }
            });
            return results;
        }""")
        await browser.close()
    return tee_times

async def check_day(target_date: date):
    day_name = DAY_NAMES.get(target_date.weekday(), "Unknown")
    print(f"\n🏌️ Checking {day_name}, {target_date.strftime('%B %-d')}")
    
    current_slots = await select_day_and_scrape(target_date)
    current_slots = deduplicate_slots(current_slots)
    
    cached_slots = load_cache(target_date)
    new_slots = find_new_slots(cached_slots, current_slots)

    if new_slots:
        summary = f"Found {len(new_slots)} new slot(s) for {day_name}, {target_date.strftime('%b %d')}!"
        send_push_notification("⛳ Tee Time Alert", summary)
        
        body = f"New slots detected:\n" + "\n".join([f"• {s['time']} | {s['holes']} | {s['price']}" for s in new_slots])
        send_email(f"⛳ Tee Time Alert – {target_date.strftime('%a %b %-d')}", body + f"\n\nBook: {BASE_URL}")
    else:
        print(f"  No new slots found. (Total visible: {len(current_slots)})")

    save_cache(target_date, current_slots)

async def main():
    target_dates = get_next_occurrences(DAYS_TO_MONITOR, num_weeks=1)
    for d in target_dates[:3]: # Only check the immediate upcoming Fri, Sat, Sun
        await check_day(d)

if __name__ == "__main__":
    asyncio.run(main())
