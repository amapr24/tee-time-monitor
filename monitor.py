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
TEE_TIME_MIN = 6      
TEE_TIME_MAX = 18     # 3 PM Limit as requested

# Secrets from Environment
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE = Path("last_teetimes.json")

# ── Notifications ────────────────────────────────────────────────────────────

def send_notifications(target_date, new_slots):
    day_str = target_date.strftime('%a %b %d')
    summary = f"⛳ {len(new_slots)} new slots for {day_str}!"
    
    if PUSHOVER_USER and PUSHOVER_TOKEN:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
            "title": "Tee Time Alert", "message": summary, "priority": 1
        })

    if EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_TO:
        body = f"New slots found for {day_str} (Before 3PM):\n\n" + \
               "\n".join([f"• {s['time']} | {s['holes']} | {s['price']}" for s in new_slots]) + \
               f"\n\nBook here: {BASE_URL}"
        msg = MIMEText(body)
        msg["Subject"] = f"⛳ Alert: {day_str}"
        msg["From"], msg["To"] = EMAIL_SENDER, EMAIL_TO
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_SENDER, EMAIL_TO.split(','), msg.as_string())
        except Exception as e:
            print(f"Email failed: {e}")

# ── Scraper Logic ───────────────────────────────────────────────────────────

async def scrape_date(page, target_date):
    print(f"🔍 Checking {target_date.strftime('%Y-%m-%d')}...")
    await page.goto(BASE_URL, wait_until="networkidle")
    await asyncio.sleep(3)

    # 1. Handle Month Navigation
    current_month = (await page.locator(".datepicker-switch").first.inner_text()).split()[0]
    target_month = target_date.strftime("%B")
    
    if current_month != target_month:
        print(f"➡️ Navigating to {target_month}...")
        await page.locator(".next").first.click()
        await asyncio.sleep(1)

    # 2. Click Day (specifically looking for active days in the current month view)
    day_val = str(target_date.day)
    # This selector avoids clicking '20' if it appears as a muted day from the previous month
    day_selector = f"td.day:not(.old):not(.new) >> text=/^{day_val}$/"
    
    try:
        await page.locator(day_selector).click()
        await asyncio.sleep(4) # Allow results to load
    except Exception as e:
        print(f"❌ Failed to click day {day_val}: {e}")
        return []

    # 3. Extract and Filter by Time
    slots = await page.evaluate(f"""
        () => {{
            const results = [];
            const minH = {TEE_TIME_MIN};
            const maxH = {TEE_TIME_MAX};
            
            document.querySelectorAll('.teetime-card, .tee-time-item, [class*="slot"]').forEach(el => {{
                const text = el.innerText.toUpperCase();
                const timeMatch = text.match(/(\\d{{1,2}}):(\\d{{2}})\\s*([AP]M)/);
                
                if (timeMatch) {{
                    let hour = parseInt(timeMatch[1]);
                    const ampm = timeMatch[3];
                    if (ampm === 'PM' && hour !== 12) hour += 12;
                    if (ampm === 'AM' && hour === 12) hour = 0;
                    
                    if (hour >= minH && hour <= maxH) {{
                        results.push({{
                            time: timeMatch[0],
                            holes: text.includes("18") ? "18 Holes" : "9 Holes",
                            price: (text.match(/\\$\\d+/) || [""])[0]
                        }});
                    }}
                }}
            }});
            return results;
        }}
    """)
    return slots

# ── Cache & Main ────────────────────────────────────────────────────────────

def get_new_slots(target_date, current_slots):
    cache = {}
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f: cache = json.load(f)
        except: pass
    
    date_key = target_date.isoformat()
    old_times = {s['time'] for s in cache.get(date_key, [])}
    new_found = [s for s in current_slots if s['time'] not in old_times]
    
    cache[date_key] = current_slots
    with open(CACHE_FILE, 'w') as f: json.dump(cache, f)
    return new_found

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()

        today = date.today()
        # Check specific days for the next 2 weeks
        for i in range(14):
            check_date = today + timedelta(days=i)
            if check_date.weekday() in DAYS_TO_MONITOR:
                found = await scrape_date(page, check_date)
                new_slots = get_new_slots(check_date, found)
                if new_slots:
                    print(f"✨ {len(new_slots)} NEW SLOTS FOUND")
                    send_notifications(check_date, new_slots)
                else:
                    print(f"✅ {len(found)} slots total (0 new).")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
