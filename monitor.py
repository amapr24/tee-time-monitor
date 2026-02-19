"""
Miami Lakes Tee Time Monitor - Stabilized Version
Fixed: Added extra waiting time for slow-loading golf site elements.
"""

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

# Notification keys
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE = Path("last_teetimes.json")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ── Notifications ────────────────────────────────────────────────────────────

def send_push_notification(title, message):
    if PUSHOVER_USER and PUSHOVER_TOKEN:
        try:
            requests.post("https://api.pushover.net/1/messages.json", data={
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title,
                "message": message,
                "priority": 1 
            }, timeout=10)
        except Exception as e:
            print(f"  ❌ Push failed: {e}")

def send_email(subject, body):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"  ❌ Email failed: {e}")

# ── Core Functions ───────────────────────────────────────────────────────────

def get_next_occurrences(day_indices, num_weeks=2):
    target_dates = []
    today = date.today()
    for i in range(num_weeks * 7):
        current_date = today + timedelta(days=i)
        if current_date.weekday() in day_indices:
            target_dates.append(current_date)
    return target_dates

def load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_cache(target_date, slots):
    cache = load_cache()
    cache[target_date.isoformat()] = slots
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

async def check_date(context, target_date):
    day_name = DAY_NAMES[target_date.weekday()]
    date_str = target_date.strftime("%Y-%m-%d")
    print(f"🔎 Checking {day_name}, {date_str}...")

    url = f"{BASE_URL}?searchDate={date_str}"
    page = await context.new_page()
    
    try:
        # 1. Wait for the page to stop sending network traffic
        await page.goto(url, wait_until="networkidle", timeout=60000)
        
        # 2. INCREASED TIMEOUT: Wait up to 30 seconds for the tee times to appear
        # Also added a check to see if the page says "No tee times found"
        try:
            await page.wait_for_selector(".tee-time-item-container", timeout=30000)
        except:
            print(f"  ℹ️ No tee times visible for {date_str} (or site is slow).")
            return

        items = await page.query_selector_all(".tee-time-item-container")
        current_slots = []

        for item in items:
            time_el = await item.query_selector(".tee-time-time")
            price_el = await item.query_selector(".tee-time-price-amount")
            
            if time_el and price_el:
                time_text = (await time_el.inner_text()).strip()
                price_text = (await price_el.inner_text()).strip()
                
                # Hour filter logic
                try:
                    hour_part = time_text.split(":")[0]
                    hour = int(hour_part)
                    if "PM" in time_text and hour != 12: hour += 12
                    if "AM" in time_text and hour == 12: hour = 0
                    
                    if TEE_TIME_MIN <= hour <= TEE_TIME_MAX:
                        current_slots.append({"time": time_text, "price": price_text})
                except:
                    continue

        # 3. Cache Check
        cache = load_cache()
        old_slots = cache.get(date_str, [])
        new_slots = [s for s in current_slots if s not in old_slots]

        if new_slots:
            summary = f"Found {len(new_slots)} new slots for {day_name}, {target_date.strftime('%b %d')}!"
            send_push_notification("⛳ New Tee Time!", summary)
            
            body = f"New slots for {day_name}, {target_date.strftime('%B %d')}:\n"
            body += "\n".join([f"• {s['time']} | {s['price']}" for s in new_slots])
            body += f"\n\nBook here: {url}"
            send_email(f"⛳ Tee Time Alert – {day_name} {target_date.strftime('%b %d')}", body)
            print(f"  ✨ {summary}")
        else:
            print("  No new slots found.")

        save_cache(target_date, current_slots)

    except Exception as e:
        print(f"  ❌ Error on {date_str}: {e}")
    finally:
        await page.close()

async def main():
    target_dates = get_next_occurrences(DAYS_TO_MONITOR, num_weeks=2)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        for d in target_dates:
            await check_date(context, d)
            await asyncio.sleep(2) # Brief pause between dates to be "polite" to the server
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())

send_push_notification("Test Alert", "If you see this, your GitHub Action and Pushover are linked!")
