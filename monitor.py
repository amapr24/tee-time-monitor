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
TEE_TIME_MAX = 18     

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE = Path("last_teetimes.json")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

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
        with open(CACHE_FILE, "r") as f: return json.load(f)
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
        # GO BACK TO BASICS: Just go to the URL and wait for the specific container
        await page.goto(f"{BASE_URL}?searchDate={date_str}")
        
        # We'll wait up to 20 seconds. If it's not there, we'll log it.
        await page.wait_for_selector(".tee-time-item-container", timeout=20000)
        
        items = await page.query_selector_all(".tee-time-item-container")
        current_slots = []

        for item in items:
            time_el = await item.query_selector(".tee-time-time")
            price_el = await item.query_selector(".tee-time-price-amount")
            
            if time_el and price_el:
                time_text = (await time_el.inner_text()).strip()
                price_text = (await price_el.inner_text()).strip()
                
                # Simple hour filter
                hour = int(time_text.split(":")[0])
                if "PM" in time_text and hour != 12: hour += 12
                if "AM" in time_text and hour == 12: hour = 0
                
                if TEE_TIME_MIN <= hour <= TEE_TIME_MAX:
                    current_slots.append({"time": time_text, "price": price_text})

        # Compare with last run
        cache = load_cache()
        old_slots = cache.get(date_str, [])
        new_slots = [s for s in current_slots if s not in old_slots]

        if new_slots:
            msg = f"Found {len(new_slots)} new slots for {date_str}!"
            send_push_notification("⛳ New Tee Time!", msg)
            send_email(f"⛳ Alert: {date_str}", f"{msg}\n\n" + "\n".join([f"{s['time']} @ {s['price']}" for s in new_slots]))
            print(f"  ✨ {msg}")
        else:
            print(f"  No new slots. (Found {len(current_slots)} total)")

        save_cache(target_date, current_slots)
    except Exception as e:
        print(f"  ⚠️ Could not find tee times for {date_str}. (Check if dates are open yet)")
    finally:
        await page.close()

async def main():
    # Reduced to 2 weeks to stay within typical "booking windows"
    target_dates = []
    today = date.today()
    for i in range(14):
        d = today + timedelta(days=i)
        if d.weekday() in DAYS_TO_MONITOR: target_dates.append(d)
        
    async with async_playwright() as p:
        # Removed custom User-Agent to let Playwright use its default "Headless" identity
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        for d in target_dates:
            await check_date(context, d)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
