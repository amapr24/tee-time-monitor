"""
Tee Time Monitor -- Miami Lakes, Normandy & Miami Shores
Fixed version with Speed Engine, Color Coding, and Toast UI.
"""

import asyncio
import json
import os
import smtplib
from datetime import date, timedelta, datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo
from astral.sun import sun
from playwright.async_api import async_playwright

# ── Credentials ───────────────────────────────────────────────────────────────
SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

# ── Constants & Config ────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")
MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)
CACHE_DIR = Path(".")
# The "Speed" Engine: Limit to 3 concurrent browser instances
browser_semaphore = asyncio.Semaphore(3)

COURSES = [
    {
        "name":           "Miami Lakes",
        "type":           "cpsgolf",
        "url":            "https://miamilakes.cps.golf/onlineresweb/search-teetime",
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_miami_lakes.json",
        "skip_past_dates": False,
    },
    {
        "name":           "Normandy Shores",
        "type":           "chronogolf",
        "url":            "https://www.chronogolf.com/club/normandy-shores-golf-course",
        "holes":          18,
        "group_size":     4,
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_normandy.json",
        "skip_past_dates": True,
    },
    {
        "name":           "Miami Shores",
        "type":           "chronogolf",
        "url":            "https://www.chronogolf.com/club/miami-shores-country-club",
        "holes":          18,
        "group_size":     4,
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_miami_shores.json",
        "skip_past_dates": True,
    }
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_sunset_cutoff(target_date: date, fallback_hour: int) -> int:
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        return s["sunset"].hour - 4
    except:
        return fallback_hour

def get_upcoming_weekend_dates() -> list[date]:
    today = datetime.now(ET).date()
    return [today + timedelta(days=i) for i in range(7) if (today + timedelta(days=i)).weekday() in (4, 5, 6)]

def get_slot_color_class(time_str: str) -> str:
    """Color coding: Emerald (<10AM), Gold (10AM-2PM), Slate (>2PM)"""
    try:
        parts = time_str.strip().split()
        hour, _ = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12: hour += 12
        elif ampm == "AM" and hour == 12: hour = 0
        
        if hour < 10: return "slot-emerald"
        if 10 <= hour < 14: return "slot-gold"
        return "slot-slate"
    except:
        return ""

def load_cache(cache_file: Path, d: date) -> list[dict]:
    try:
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
            return data.get(d.isoformat(), [])
    except: pass
    return []

def save_cache(cache_file: Path, d: date, slots: list[dict]):
    all_cache = {}
    if cache_file.exists():
        try: all_cache = json.loads(cache_file.read_text())
        except: pass
    all_cache[d.isoformat()] = slots
    cache_file.write_text(json.dumps(all_cache, indent=2))

def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]

def is_within_window(time_str: str, t_min: int, t_max: int) -> bool:
    try:
        parts = time_str.strip().split()
        hour, _ = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12: hour += 12
        elif ampm == "AM" and hour == 12: hour = 0
        return t_min <= hour <= t_max
    except: return True

# ── Notifications ─────────────────────────────────────────────────────────────

def notify(subject: str, body: str, push_msg: str):
    # Email
    if all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        try:
            recipients = [e.strip() for e in EMAIL_TO.split(",")]
            msg = MIMEText(body, "plain")
            msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_SENDER, ", ".join(recipients)
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        except Exception as e: print(f"Email error: {e}")
    
    # Pushover
    if all([PUSHOVER_USER, PUSHOVER_TOKEN]):
        try:
            requests.post("https://api.pushover.net/1/messages.json", data={
                "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                "title": subject, "message": push_msg, "sound": "cashregister"
            }, timeout=10)
        except Exception as e: print(f"Pushover error: {e}")

# ── Scrapers ──────────────────────────────────────────────────────────────────

async def launch_browser(playwright):
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(user_agent="Mozilla/5.0...")
    return browser, context

async def scrape_cpsgolf(course: dict, target_date: date) -> list[dict]:
    async with async_playwright() as p:
        browser, context = await launch_browser(p)
        page = await context.new_page()
        url = f"{course['url']}?TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={course['tee_time_max']}"
        await page.goto(url, wait_until="networkidle")
        # Note: In a real run, you'd include the month/day navigation logic here
        await browser.close()
        return [] # Simplified for space; keep your original logic here

async def scrape_chronogolf(course: dict, target_date: date) -> list[dict]:
    url = f"{course['url']}?date={target_date.isoformat()}&step=teetimes&holes={course.get('holes', 18)}&groupSize={course.get('group_size', 4)}"
    async with async_playwright() as p:
        browser, context = await launch_browser(p)
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle")
        # Extraction logic goes here...
        await browser.close()
        return [] # Simplified for space; keep your original logic here

# ── Check Logic ───────────────────────────────────────────────────────────────

async def check_day(course: dict, target_date: date):
    async with browser_semaphore:
        name = course["name"]
        t_min = course["tee_time_min"]
        t_max = get_sunset_cutoff(target_date, course["tee_time_max"])
        cache_file = CACHE_DIR / course["cache_file"]

        if course.get("skip_past_dates") and target_date < datetime.now(ET).date():
            return

        # Scrape
        if course["type"] == "cpsgolf": raw = await scrape_cpsgolf(course, target_date)
        else: raw = await scrape_chronogolf(course, target_date)

        current_slots = [s for s in raw if is_within_window(s.get("time",""), t_min, t_max)]
        cached_slots = load_cache(cache_file, target_date)
        new_slots = find_new_slots(cached_slots, current_slots)
        
        save_cache(cache_file, target_date, current_slots)

        if new_slots:
            subject = f"Tee Time Alert - {name}"
            msg = f"New times found for {target_date.strftime('%a %b %d')}"
            notify(subject, msg, msg)

# ── HTML Generation ───────────────────────────────────────────────────────────

def generate_html():
    dates = get_upcoming_weekend_dates()
    last_run_iso = datetime.now(ET).isoformat()
    now_str = datetime.now(ET).strftime("%-I:%M %p ET, %A %b %-d")

    cards_html = ""
    for course in COURSES:
        cache_file = CACHE_DIR / course["cache_file"]
        days_html = ""
        for d in dates:
            slots = load_cache(cache_file, d)
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            book_url = f"{course['url']}?date={d.isoformat()}" # Simplified
            
            times_html = "".join(f'<span class="slot {get_slot_color_class(s.get("time",""))}">{s.get("time","?")}</span>' for s in slots) if slots else '<p class="no-times">No times available</p>'
            
            days_html += f"""
            <div class="day-block">
              <div class="day-header">
                <span class="day-name">{d.strftime("%A, %b %-d")}</span>
                <a class="book-btn" href="{book_url}" target="_blank">Book →</a>
              </div>
              <div class="slots">{times_html}</div>
            </div>"""

        cards_html += f"""
        <div class="course-card">
          <div class="card-header">
            <div class="course-name">{course["name"]}</div>
            <span class="window-tag">⏱ Monitoring until {course["tee_time_max"]}:00</span>
          </div>
          {days_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tee Time Watch</title>
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --green-deep: #0d2b1a; --green-mid: #1a5c32; --gold: #e8b94a;
      --emerald: #2e8b4f; --slate: #475569; --cream: #f5f0e8; --white: #ffffff;
    }}
    body {{ font-family: 'DM Sans', sans-serif; background: var(--cream); color: var(--green-deep); padding-bottom: 80px; }}

    .ticker {{ background: var(--gold); color: var(--green-deep); font-family: 'Bebas Neue', sans-serif; padding: 8px 0; overflow: hidden; white-space: nowrap; }}
    .ticker-inner {{ display: inline-block; animation: ticker 40s linear infinite; }}
    .ticker-inner span {{ margin: 0 40px; font-size: 1.1rem; }}
    @keyframes ticker {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-50%); }} }}

    header {{ background: var(--green-deep); padding: 40px 20px; text-align: center; color: var(--white); }}
    h1 {{ font-family: 'Bebas Neue', sans-serif; font-size: 4rem; line-height: 0.9; }}
    h1 em {{ color: var(--gold); font-style: normal; }}
    
    .updated-bar {{ background: var(--green-mid); text-align: center; padding: 10px; font-size: 0.8rem; color: var(--white); border-bottom: 3px solid var(--gold); }}

    main {{ max-width: 1100px; margin: 30px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 25px; }}
    
    .course-card {{ background: var(--white); border-radius: 16px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }}
    .card-header {{ background: var(--green-deep); padding: 20px; color: var(--white); }}
    .course-name {{ font-family: 'Bebas Neue', sans-serif; font-size: 2rem; }}

    .day-block {{ padding: 15px 20px; border-bottom: 1px solid #eee; }}
    .day-header {{ display: flex; justify-content: space-between; align-items: center; }}
    .day-name {{ font-weight: 700; font-size: 0.8rem; color: var(--green-mid); text-transform: uppercase; }}
    .book-btn {{ background: var(--gold); color: var(--green-deep); text-decoration: none; padding: 4px 12px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; }}
    
    .slots {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .slot {{ padding: 6px 10px; border-radius: 6px; font-size: 0.85rem; font-weight: 600; color: white; background: #999; }}
    
    .slot-emerald {{ background: var(--emerald) !important; }}
    .slot-gold {{ background: var(--gold) !important; color: var(--green-deep); }}
    .slot-slate {{ background: var(--slate) !important; }}

    #toast {{
      position: fixed; bottom: -100px; left: 50%; transform: translateX(-50%);
      background: var(--green-deep); color: var(--gold); padding: 15px 30px;
      border-radius: 50px; font-weight: bold; transition: bottom 0.5s ease;
      box-shadow: 0 10px 30px rgba(0,0,0,0.3); z-index: 9999;
      font-family: 'Bebas Neue', sans-serif;
    }}
    #toast.show {{ bottom: 30px; }}

    .check-now-btn {{ background: var(--gold); border: none; padding: 15px 40px; font-family: 'Bebas Neue', sans-serif; font-size: 1.2rem; border-radius: 50px; cursor: pointer; display: block; margin: 40px auto; }}
  </style>
</head>
<body>
  <div class="ticker">
    <div class="ticker-inner">
      <span>FORE! ⛳</span><span>TEE TIME WATCH 🏌️</span><span>MIAMI AREA GOLF ⛳</span><span>BOOK FAST 🏌️</span>
      <span>FORE! ⛳</span><span>TEE TIME WATCH 🏌️</span><span>MIAMI AREA GOLF ⛳</span><span>BOOK FAST 🏌️</span>
    </div>
  </div>

  <header><h1>TEE TIME <em>WATCH</em></h1></header>

  <div class="updated-bar">
    Checked every 5 mins · Last: <strong>{now_str}</strong> (<span id="mins">0</span> min ago)
  </div>

  <main>{cards_html}</main>

  <button class="check-now-btn" onclick="triggerCheck()">CHECK NOW ⛳</button>
  <div id="toast">🏌️ CHECK TRIGGERED! REFRESH IN 2 MINS.</div>

  <script>
    const lastRun = new Date("{last_run_iso}");
    function updateMins() {{
      const d = Math.floor((new Date() - lastRun) / 60000);
      const el = document.getElementById('mins');
      if(el) el.textContent = d;
    }}
    setInterval(updateMins, 60000);
    updateMins();

    async function triggerCheck() {{
      const t = document.getElementById('toast');
      try {{
        const r = await fetch('/api/trigger', {{ method: 'POST' }});
        if (r.ok) {{
          t.classList.add('show');
          setTimeout(() => t.classList.remove('show'), 4000);
        }}
      }} catch (e) {{ console.error('Error'); }}
    }}
  </script>
</body>
</html>"""
    Path("index.html").write_text(html)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    dates = get_upcoming_weekend_dates()
    tasks = [check_day(course, d) for course in COURSES for d in dates]
    await asyncio.gather(*tasks)
    generate_html()

if __name__ == "__main__":
    asyncio.run(main())
