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
DAYS_TO_MONITOR = [4, 5, 6]  # Fri, Sat, Sun
TEE_TIME_MIN = 0      
TEE_TIME_MAX = 23     # 11 PM: Let's see everything to be safe

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE = Path("last_teetimes.json")

def send_push_notification(title, message):
    if PUSHOVER_USER and PUSHOVER_TOKEN:
        try:
            requests.post("https://api.pushover.net/1/messages.json", data={
                "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": message, "priority": 1 
            }, timeout=10)
        except: pass

def send_email(subject, body):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]): return
    msg = MIMEText(body); msg["Subject"] = subject; msg["From"] = EMAIL_SENDER; msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls(); server.login(EMAIL_SENDER, EMAIL_PASSWORD); server.send_message(msg)
    except: pass

def load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f: return json.load(f)
        except: return {}
    return {}

def save_cache(target_date, slots):
    cache = load_cache()
    cache[target_date.isoformat()] = slots
    with open(CACHE_FILE, "w") as f: json.dump(cache, f, indent=2)

async def check_date(context, target_date):
    date_str = target_date.strftime("%Y-%m-%d")
    print(f"🔎 Checking {date_str}...")
    page = await context.new_page()
    
    try:
        # 1. Navigate and wait for the base page to load
        await page.goto(f"{BASE_URL}?searchDate={date_str}", wait_until="domcontentloaded")
        await asyncio.sleep(5) # Give it 5 seconds to load the scripts
        
        # 2. Click Search using the exact text-based selector from your original
        try:
            await page.click('button:has-text("Search")', timeout=5000)
            print("  🖱️ Clicked Search button...")
        except:
            print("  ⚠️ Search button not found or already clicked.")

        # 3. Wait up to 20 seconds for the tee times to appear
        await page.wait_for_selector(".tee-time-item-container", timeout=20000)
        
        items = await page.query_selector_all(".tee-time-item-container")
        print(f"  👀 Found {len(items)} total items on page.")

        current_slots = []
        for item in items:
            time_el = await item.query_selector(".tee-time-time")
            price_el = await item.query_selector(".tee-time-price-amount")
            if time_el and price_el:
                time_text = (await time_el.inner_text()).strip()
                price_text = (await price_el.inner_text()).strip()
                
                # Hour filter
                hour = int(time_text.split(":")[0])
                if "PM" in time_text and hour != 12: hour += 12
                if "AM" in time_text and hour == 12: hour = 0
                
                if TEE_TIME_MIN <= hour <= TEE_TIME_MAX:
                    current_slots.append({"time": time_text, "price": price_text})

        # Cache Comparison
        cache = load_cache()
        old_slots = cache.get(date_str, [])
        new_slots = [s for s in current_slots if s not in old_slots]

        if new_slots:
            msg = f"Found {len(new_slots)} new slots for {date_str}!"
            send_push_notification("⛳ New Tee Time!", msg)
            send_email(f"⛳ Alert: {date_str}", f"{msg}\n\n" + "\n".join([f"{s['time']} @ {s['price']}" for s in new_slots]))
            print(f"  ✨ SUCCESS: {msg}")
        else:
            print(f"  Done. {len(current_slots)} slots match your window (0 new).")

        save_cache(target_date, current_slots)
    except Exception as e:
        print(f"  ❌ Error: Could not find tee time containers. (Details: {str(e)[:50]}...)")
    finally:
        await page.close()

async def main():
    today = date.today()
    target_dates = []
    found_days = set()
    for i in range(14):
        d = today + timedelta(days=i)
        if d.weekday() in DAYS_TO_MONITOR and d.weekday() not in found_days:
            target_dates.append(d)
            found_days.add(d.weekday())

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Use a real-looking user agent to prevent blocking
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        for d in target_dates:
            await check_date(context, d)
            await asyncio.sleep(2)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
