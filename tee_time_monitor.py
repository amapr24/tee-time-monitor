"""
Tee Time Monitor -- Miami Lakes, Normandy & Miami Shores
Checks multiple golf courses and sends email + Pushover push notifications
when new tee times appear.

Setup:
  pip install playwright requests
  playwright install chromium
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

# ── Email / Pushover credentials (from GitHub secrets) ────────────────────────

SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")

PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

# ── Course configuration ───────────────────────────────────────────────────────

COURSES = [
    {
        "name":           "Miami Lakes",
        "address":        "6801 Miami Lakes Dr, Miami Lakes",
        "phone":          "(305) 558-4653",
        "type":           "cpsgolf",
        "url":            "https://miamilakes.cps.golf/onlineresweb/search-teetime",
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_miami_lakes.json",
        "skip_past_dates": False,
    },
    {
        "name":           "Normandy Shores",
        "address":        "2401 Biarritz Dr, Miami Beach",
        "phone":          "(305) 868-6502",
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
        "address":        "10000 Biscayne Blvd, Miami Shores",
        "phone":          "(305) 795-2369",
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

# ── Constants ─────────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")
DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
CACHE_DIR = Path(".")
MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_sunset_cutoff(target_date: date, fallback_hour: int) -> int:
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        return s["sunset"].hour - 4
    except Exception:
        return fallback_hour

def get_upcoming_weekend_dates() -> list[date]:
    today = datetime.now(ET).date()
    return [today + timedelta(days=i) for i in range(6) if (today + timedelta(days=i)).weekday() in (4, 5, 6)]

def is_within_window(time_str: str, t_min: int, t_max: int) -> bool:
    try:
        parts = time_str.strip().split()
        hour, _ = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12: hour += 12
        elif ampm == "AM" and hour == 12: hour = 0
        return t_min <= hour <= t_max
    except Exception: return True

def is_slot_in_past(time_str: str, target_date: date) -> bool:
    if target_date != datetime.now(ET).date(): return False
    try:
        parts = time_str.strip().split()
        hour, minute = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12: hour += 12
        elif ampm == "AM" and hour == 12: hour = 0
        now_et = datetime.now(ET)
        return (hour, minute) <= (now_et.hour, now_et.minute)
    except Exception: return False

def deduplicate_slots(slots: list[dict], t_min: int, t_max: int) -> list[dict]:
    seen, out = set(), []
    for slot in slots:
        t = slot.get("time", "").strip().upper()
        if not is_within_window(t, t_min, t_max): continue
        if t not in seen:
            seen.add(t)
            out.append(slot)
    return out

# ── Notifications ──────────────────────────────────────────────────────────────

def send_pushover(title: str, message: str):
    if not all([PUSHOVER_USER, PUSHOVER_TOKEN]): return
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": message, "sound": "cashregister", "priority": 0
        }, timeout=10)
    except Exception: pass

def notify(subject: str, body: str, push_msg: str):
    send_pushover(subject, push_msg)

# ── Scrapers ──────────────────────────────────────────────────────────────────

async def launch_browser(playwright):
    browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 900})
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return browser, context

async def scrape_cpsgolf(course: dict, target_date: date) -> list[dict]:
    async with async_playwright() as p:
        browser, context = await launch_browser(p)
        page = await context.new_page()
        url = f"{course['url']}?TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={course['tee_time_max']}"
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        
        target_month_str = target_date.strftime("%B %Y")
        for _ in range(12):
            header = await page.evaluate("() => { const pat = /^[A-Za-z]+ \\d{4}$/; const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false); let node; while ((node = walker.nextNode())) { const t = node.textContent.trim(); if (pat.test(t)) return t; } return ''; }")
            if target_month_str in (header or ""): break
            await page.evaluate("() => { for (const el of document.querySelectorAll('*')) { const t = (el.innerText || '').trim(); if (t === '›' || t === '>' || t === '▶' || t === '→') { el.click(); return true; } } return false; }")
            await page.wait_for_timeout(800)

        day_num = str(target_date.day)
        await page.evaluate(f"() => {{ const target = '{day_num}'; const all = document.querySelectorAll('div, span, a, button, li'); for (const el of all) {{ const text = (el.innerText || '').trim(); if (text !== target) continue; const classes = (el.className || '').toLowerCase(); if (['gray','grey','disabled','prev','next','old','muted','inactive'].some(c => classes.includes(c))) continue; el.click(); return true; }} return false; }}")
        await page.wait_for_timeout(4000)

        results = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('[class*="teetime"], [class*="tee-time"], [class*="timeslot"]').forEach(card => {
                const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                const m = raw.match(/(\\d{1,2}:\\d{2})\\s*(AM|PM)/i);
                if (m) results.push({ time: m[1] + ' ' + m[2].toUpperCase() });
            });
            return results;
        }""")
        await browser.close()
        return results

async def scrape_chronogolf(course: dict, target_date: date) -> list[dict]:
    url = f"{course['url']}?date={target_date.isoformat()}&step=teetimes&holes={course.get('holes', 18)}&groupSize={course.get('group_size', 4)}"
    async with async_playwright() as p:
        browser, context = await launch_browser(p)
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        results = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('[class*="teetime"], [class*="tee-time"], [class*="green-fee"]').forEach(card => {
                const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                const m = raw.match(/(\\d{1,2}:\\d{2})\\s*(AM|PM)/i);
                if (m) results.push({ time: m[1] + ' ' + m[2].toUpperCase() });
            });
            return results;
        }""")
        await browser.close()
        return results

# ── Cache Management ──────────────────────────────────────────────────────────

def load_cache(cache_file: Path, d: date) -> list[dict]:
    try:
        if cache_file.exists(): return json.loads(cache_file.read_text()).get(d.isoformat(), [])
    except Exception: pass
    return []

def save_cache(cache_file: Path, d: date, slots: list[dict]):
    all_cache = {}
    try:
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
            all_cache = data if isinstance(data, dict) else {}
    except Exception: pass
    all_cache[d.isoformat()] = slots
    cache_file.write_text(json.dumps(all_cache, indent=2))

async def check_day(course: dict, target_date: date):
    t_max = get_sunset_cutoff(target_date, course["tee_time_max"])
    cache_file = CACHE_DIR / course["cache_file"]
    
    if course.get("skip_past_dates") and target_date < datetime.now(ET).date(): return
    if target_date == datetime.now(ET).date() and datetime.now(ET).hour >= t_max: return

    raw = await scrape_cpsgolf(course, target_date) if course["type"] == "cpsgolf" else await scrape_chronogolf(course, target_date)
    current_slots = deduplicate_slots(raw, course["tee_time_min"], t_max)
    cached_slots = load_cache(cache_file, target_date)
    
    new_slots = [t for t in current_slots if t.get("time", "").strip().upper() not in {s.get("time", "").strip().upper() for s in cached_slots}]
    save_cache(cache_file, target_date, current_slots)

    if new_slots:
        notify(f"Tee Alert - {course['name']}", f"New slots: {', '.join(s['time'] for s in new_slots)}", f"{len(new_slots)} new slots at {course['name']}")

# ── HTML Generation with iPhone 16 Pro Fix ────────────────────────────────────

def generate_html():
    dates = get_upcoming_weekend_dates()
    now_str = datetime.now(ET).strftime("%-I:%M %p ET, %A %B %-d, %Y")
    now_ts  = int(datetime.now(ET).timestamp())

    cards_html = ""
    for course in COURSES:
        days_html = ""
        for d in dates:
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            raw_slots = load_cache(CACHE_DIR / course["cache_file"], d)
            slots = [s for s in deduplicate_slots(raw_slots, course["tee_time_min"], t_max_day) if not is_slot_in_past(s.get("time", ""), d)]
            
            day_body = "".join(f'<span class="slot">{s.get("time","?")}</span>' for s in slots) if slots else '<p class="no-times">No times available</p>'
            days_html += f'<div class="day-block"><div class="day-header"><span class="day-name">{d.strftime("%A, %b %-d")}</span><a class="book-btn" href="{course["url"]}">Book →</a></div><div class="slots">{day_body}</div></div>'
        
        cards_html += f'<div class="course-card"><div class="card-header"><div class="course-name">{course["name"]}</div><div class="course-meta">{course["address"]}</div></div>{days_html}</div>'

    s_info = sun(MIAMI.observer, date=dates[0], tzinfo=ET)
    actual_sunset = s_info["sunset"].strftime("%-I:%M %p")
    repr_cutoff = get_sunset_cutoff(dates[0], 14)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <title>Tee Time Watch</title>
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{ --green-deep: #0d2b1a; --green-mid: #1a5c32; --gold: #e8b94a; --cream: #f5f0e8; }}
    body {{ font-family: 'DM Sans', sans-serif; background: var(--cream); color: #0d2b1a; }}
    
    header {{ background: var(--green-deep); padding: 32px 20px; text-align: center; border-bottom: 6px solid #3a7d44; }}
    .header-inner {{ display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; max-width: 800px; margin: 0 auto; color: white; }}
    h1 {{ font-family: 'Bebas Neue', sans-serif; font-size: 4rem; letter-spacing: 0.05em; line-height: 0.9; }}
    h1 em {{ color: var(--gold); font-style: normal; }}
    
    .window-tag {{ color: var(--gold); font-size: 0.8rem; text-transform: uppercase; margin-top: 10px; display: block; }}
    .sunset-pill {{ background: var(--gold); color: var(--green-deep); padding: 2px 8px; border-radius: 4px; font-weight: 800; margin-left: 5px; display: inline-block; vertical-align: baseline; }}

    .updated-bar {{ background: var(--green-mid); text-align: center; padding: 10px; font-size: 0.85rem; color: white; border-bottom: 3px solid var(--gold); }}
    main {{ max-width: 1100px; margin: 20px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; }}
    .course-card {{ background: white; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }}
    .card-header {{ background: var(--green-deep); padding: 20px; color: white; }}
    .course-name {{ font-family: 'Bebas Neue', sans-serif; font-size: 1.8rem; }}
    .day-block {{ padding: 15px; border-bottom: 1px solid #eee; position: relative; }}
    .day-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
    .day-name {{ font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }}
    .book-btn {{ background: var(--gold); color: var(--green-deep); text-decoration: none; padding: 4px 12px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; }}
    .slots {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .slot {{ background: #e8f4ec; padding: 5px 10px; border-radius: 6px; font-size: 0.8rem; font-weight: 600; }}
    .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 20px; display: none; z-index: 1000; }}

    @media (max-width: 767px) {{
      header {{ padding: 20px 0; }}
      .header-inner {{ grid-template-columns: 60px 1fr 60px; padding: 0 5px; gap: 0; }}
      h1 {{ font-size: 2.6rem; }}
      .header-flag {{ justify-self: left; font-size: 2.2rem; }}
      .header-golfer {{ justify-self: right; font-size: 2.2rem; }}
      .window-tag {{ font-size: 0.7rem; }}
      .sunset-pill {{ font-size: 0.65rem; padding: 1px 5px; margin-left: 2px; }}
    }}
  </style>
</head>
<body>
  <header><div class="header-inner"><span class="header-flag">⛳</span><div><h1>TEE <em>TIME</em> WATCH</h1><span class="window-tag">⏱ 8:00 AM – {repr_cutoff % 12 or 12}:00 PM <span class="sunset-pill">SUNSET: {actual_sunset}</span></span></div><span class="header-golfer">🏌️</span></div></header>
  <div class="updated-bar">Last run: <strong>{now_str}</strong><span id="mins-ago"></span></div>
  <main>{cards_html}</main>
  <div style="text-align:center; padding:20px;"><button style="background:var(--green-deep); color:white; border:1px solid var(--gold); padding:10px 24px; border-radius:30px; font-family:'Bebas Neue'; cursor:pointer;" onclick="triggerCheck()" id="check-btn">CHECK NOW ⛳</button></div>
  <div id="toast" class="toast"></div>
  <script>
    (function() {{
      const el = document.getElementById('mins-ago');
      const lastRun = new Date({now_ts} * 1000);
      function update() {{
        const mins = Math.floor((Date.now() - lastRun) / 60000);
        el.textContent = mins < 1 ? ' · just now' : ` · ${{mins}} mins ago`;
      }}
      update(); setInterval(update, 30000);
    }})();
    async function triggerCheck() {{
      const btn = document.getElementById('check-btn');
      btn.disabled = true; btn.textContent = 'TRIGGERING...';
      try {{
        const resp = await fetch('/api/trigger', {{ method: 'POST' }});
        if (resp.ok) showToast('Check triggered! Results update in ~2 mins.');
        else showToast('Error triggering check.');
      }} catch (e) {{ showToast('Network error.'); }}
      btn.disabled = false; btn.textContent = 'CHECK NOW ⛳';
    }}
    function showToast(msg) {{ const t = document.getElementById('toast'); t.textContent = msg; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 3500); }}
  </script>
</body>
</html>"""
    Path("index.html").write_text(html)

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    dates = get_upcoming_weekend_dates()
    for course in COURSES:
        for d in dates: await check_day(course, d)
    generate_html()

if __name__ == "__main__":
    asyncio.run(main())
