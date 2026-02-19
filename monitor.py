import asyncio
import json
import os
import smtplib
import requests
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from playwright.async_api import async_playwright

# ── Configuration ────────────────────────────────────────────────────────────
BASE_URL = "https://miamilakes.cps.golf/onlineresweb/search-teetime"
DAYS_TO_MONITOR = [4, 5, 6] # Fri, Sat, Sun
TEE_TIME_MIN = 0      
TEE_TIME_MAX = 23     

# Secrets from Environment
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE = Path("last_teetimes.json")
DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}

# ── Notifications ────────────────────────────────────────────────────────────

def send_notifications(target_date, new_slots):
    day_str = target_date.strftime('%a %b %d')
    summary = f"⛳ {len(new_slots)} new slots for {day_str}!"
    
    # 1. Pushover
    if PUSHOVER_USER and PUSHOVER_TOKEN:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
            "title": "Tee Time Alert", "message": summary, "priority": 1
        })

    # 2. Email
    if EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_TO:
        body = f"New slots found for {day_str}:\n\n" + \
               "\n".join([f"• {s['time']} | {s['holes']} | {s['price']}" for s in new_slots]) + \
               f"\n\nBook here: {BASE_URL}"
        msg = MIMEText(body)
        msg["Subject"] = f"⛳ Alert: {day_str}"
        msg["From"], msg["To"] = EMAIL_SENDER, EMAIL_TO
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_TO.split(','), msg.as_string())

# ── Scraper Logic ───────────────────────────────────────────────────────────

async def scrape_date(page, target_date):
    print(f"🔍 Checking {target_date.strftime('%Y-%m-%d')}...")
    await page.goto(BASE_URL, wait_until="networkidle")
    await asyncio.sleep(2) # Wait for JS to settle

    # 1. Click the date picker and select day
    day_to_click = str(target_date.day)
    try:
        # This looks for the day number specifically in the calendar
        await page.locator(f"xpath=//div[contains(@class, 'day') and text()='{day_to_click}']").first.click()
        await asyncio.sleep(3) # Wait for slots to load
    except Exception as e:
        print(f"❌ Could not click day {day_to_click}: {e}")
        return []

    # 2. Extract Data
    slots = await page.evaluate("""
        () => {
            const results = [];
            document.querySelectorAll('.teetime-card, [class*="slot"]').forEach(el => {
                const text = el.innerText.toUpperCase();
                const timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*[AP]M)/);
                if (timeMatch) {
                    results.push({
                        time: timeMatch[1],
                        holes: text.includes("18") ? "18 Holes" : "9 Holes",
                        price: (text.match(/\\$\\d+/) || [""])[0]
                    });
                }
            });
            return results;
        }
    """)
    return slots

# ── Cache & Compare ────────────────────────────────────────────────────────

def get_new_slots(target_date, current_slots):
    cache = {}
    if CACHE_FILE.exists():
        with open(CACHE_FILE, 'r') as f: cache = json.load(f)
    
    date_key = target_date.isoformat()
    old_slots = cache.get(date_key, [])
    old_times = {s['time'] for s in old_slots}
    
    new_found = [s for s in current_slots if s['time'] not in old_times]
    
    # Update Cache
    cache[date_key] = current_slots
    with open(CACHE_FILE, 'w') as f: json.dump(cache, f)
    
    return new_found

# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Use a real browser fingerprint to avoid "0 visible" bot detection
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()

        # Check next 2 weeks for the days we want
        today = date.today()
        for i in range(14):
            check_date = today + timedelta(days=i)
            if check_date.weekday() in DAYS_TO_MONITOR:
                found = await scrape_date(page, check_date)
                new_slots = get_new_slots(check_date, found)
                if new_slots:
                    print(f"✨ {len(new_slots)} NEW SLOTS!")
                    send_notifications(check_date, new_slots)
                else:
                    print(f"✅ {len(found)} slots found (none new).")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
