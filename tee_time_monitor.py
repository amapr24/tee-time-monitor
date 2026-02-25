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

# ── Configuration ─────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

COURSES = [
    {
        "name": "Miami Lakes (The Senator)",
        "type": "cpsgolf",
        "url": "https://mgl.cps.golf/miamilakes_booking/Search/Index",
        "tee_time_min": 7,
        "tee_time_max": 18,
        "cache_file": "miami_lakes.json",
    },
    {
        "name": "Normandy Shores",
        "type": "chronogolf",
        "url": "https://www.chronogolf.com/marketplace/clubs/415-normandy-shores-golf-club",
        "tee_time_min": 7,
        "tee_time_max": 18,
        "cache_file": "normandy_shores.json",
        "holes": 18,
        "group_size": 4,
    },
    {
        "name": "Miami Shores",
        "type": "chronogolf",
        "url": "https://www.chronogolf.com/marketplace/clubs/1169-miami-shores-country-club",
        "tee_time_min": 7,
        "tee_time_max": 18,
        "cache_file": "miami_shores.json",
        "holes": 18,
        "group_size": 4,
    },
]

# ── Helper Functions ──────────────────────────────────────────────────────────

def get_upcoming_weekend_dates():
    today = datetime.now(ET).date()
    days_until_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_until_friday)
    return [friday, friday + timedelta(days=1), friday + timedelta(days=2)]

def get_sunset_cutoff(target_date, max_hour):
    city = LocationInfo("Miami", "USA", ET.key, 25.7617, -80.1918)
    s = sun(city.observer, date=target_date, tzinfo=ET)
    sunset = s["sunset"]
    cutoff = sunset - timedelta(hours=4)
    if cutoff.hour > max_hour:
        return max_hour
    return cutoff.hour

def load_cache(path, target_date):
    if not path.exists(): return []
    try:
        data = json.loads(path.read_text())
        return data.get(target_date.isoformat(), [])
    except: return []

def save_cache(path, target_date, slots):
    data = {}
    if path.exists():
        try: data = json.loads(path.read_text())
        except: pass
    data[target_date.isoformat()] = slots
    path.write_text(json.dumps(data, indent=2))

# ── Scrapers ──────────────────────────────────────────────────────────────────

async def scrape_cpsgolf(url, target_date, min_h, max_h):
    # logic for cpsgolf... (Implementation truncated for brevity, keep your existing logic here)
    return [] 

async def scrape_chronogolf(url, target_date, min_h, max_h):
    # logic for chronogolf... (Implementation truncated for brevity, keep your existing logic here)
    return []

# ── UI Generation ──────────────────────────────────────────────────────────────

def generate_html():
    dates = get_upcoming_weekend_dates()
    now = datetime.now(ET)
    now_iso = now.isoformat()
    now_str = now.strftime("%-I:%M %p ET")

    course_data = []
    for course in COURSES:
        cache_file = CACHE_DIR / course["cache_file"]
        days = []
        for d in dates:
            slots = load_cache(cache_file, d)
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            
            if course["type"] == "cpsgolf":
                book_url = f"{course['url']}?TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={t_max_day}"
            else:
                book_url = f"{course['url']}?date={d.isoformat()}&step=teetimes"
            
            days.append({"label": d.strftime("%A, %b %-d"), "slots": slots, "book_url": book_url})
        course_data.append({"course": course, "days": days})

    cards_html = ""
    for cd in course_data:
        course = cd["course"]
        days_html = ""
        for day in cd["days"]:
            slots_list = []
            if day["slots"]:
                for s in day["slots"]:
                    t_str = s.get("time", "12:00 PM")
                    try:
                        hour = datetime.strptime(t_str, "%I:%M %p").hour
                        if hour < 10: time_class = "morning"
                        elif hour < 14: time_class = "prime"
                        else: time_class = "afternoon"
                    except: time_class = ""
                    
                    slots_list.append(
                        f'<span class="slot {time_class}">'
                        f'<span class="t-time">{t_str}</span>'
                        f'{("<span class=\"t-price\">" + s["price"] + "</span>") if s.get("price") else ""}'
                        f'</span>'
                    )
                day_body = f'<div class="slots">{"".join(slots_list)}</div>'
            else:
                day_body = '<p class="no-times">No times available</p>'

            days_html += f"""
            <div class="day-block">
              <div class="day-header"><span class="day-name">{day["label"]}</span><a class="book-btn" href="{day["book_url"]}" target="_blank">Book →</a></div>
              {day_body}
            </div>"""

        cards_html += f"""<div class="course-card"><div class="card-header"><div class="course-name">{course["name"]}</div><span class="window-tag">⏱ {course["tee_time_min"]}:00 AM – Sunset</span></div>{days_html}</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>Tee Time Watch</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --green-deep: #0d2b1a; --green-mid: #1a5c32; --green-light: #2e8b4f; --green-pale: #d4eddc;
      --cream: #f5f0e8; --gold: #e8b94a; --gold-dark: #c49a28; --white: #ffffff;
      --morning: #e6f4ea; --prime: #fff8e1; --afternoon: #f5f5f5;
    }}
    body {{ font-family: 'DM Sans', sans-serif; background: var(--cream); color: var(--green-deep); padding-bottom: 80px; }}
    header {{ background: var(--green-deep); padding: 40px 20px; text-align: center; color: white; }}
    h1 {{ font-family: 'Bebas Neue', sans-serif; font-size: 4rem; letter-spacing: 0.05em; }}
    h1 em {{ color: var(--gold); font-style: normal; }}
    .updated-bar {{ background: var(--green-mid); color: rgba(255,255,255,0.8); text-align: center; padding: 10px; font-size: 0.8rem; border-bottom: 4px solid var(--gold); }}
    main {{ max-width: 1200px; margin: 30px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 25px; }}
    .course-card {{ background: white; border-radius: 16px; overflow: hidden; box-shadow: 0 8px 30px rgba(0,0,0,0.08); }}
    .card-header {{ background: var(--green-deep); padding: 20px; color: white; }}
    .course-name {{ font-family: 'Bebas Neue', sans-serif; font-size: 1.8rem; }}
    .window-tag {{ color: var(--gold); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; }}
    .day-block {{ padding: 15px 20px; border-bottom: 1px solid #eee; }}
    .day-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
    .day-name {{ font-size: 0.75rem; font-weight: 700; text-transform: uppercase; color: var(--green-mid); }}
    .book-btn {{ background: var(--gold); color: var(--green-deep); padding: 4px 12px; border-radius: 20px; text-decoration: none; font-size: 0.7rem; font-weight: 700; }}
    .slots {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .slot {{ padding: 8px; border-radius: 8px; min-width: 85px; text-align: center; display: flex; flex-direction: column; border: 1px solid rgba(0,0,0,0.05); }}
    .slot.morning {{ background: var(--morning); border-color: #c3e6cb; }}
    .slot.prime {{ background: var(--prime); border-color: #ffeeba; }}
    .slot.afternoon {{ background: var(--afternoon); border-color: #ddd; }}
    .t-time {{ font-weight: 700; font-size: 0.85rem; }}
    .t-price {{ font-size: 0.65rem; color: #666; }}
    .check-now-wrap {{ display: flex; justify-content: center; gap: 16px; padding: 40px 20px; }}
    .check-now-btn {{ width: 200px; padding: 15px 0; border-radius: 40px; border: none; background: var(--gold); color: var(--green-deep); font-family: 'Bebas Neue', sans-serif; font-size: 1.2rem; cursor: pointer; transition: 0.2s; box-shadow: 0 4px 15px rgba(232,185,74,0.4); }}
    .check-now-btn:hover {{ transform: translateY(-2px); background: var(--gold-dark); }}
    .toast {{ position: fixed; bottom: 30px; left: 50%; transform: translateX(-50%) translateY(100px); background: var(--green-deep); color: white; padding: 12px 24px; border-radius: 50px; display: flex; align-items: center; gap: 10px; transition: 0.5s cubic-bezier(0.18, 0.89, 0.32, 1.28); opacity: 0; z-index: 1000; box-shadow: 0 10px 30px rgba(0,0,0,0.3); border: 2px solid var(--gold); }}
    .toast.show {{ transform: translateX(-50%) translateY(0); opacity: 1; }}
  </style>
</head>
<body>
  <header><h1>TEE TIME <em>WATCH</em></h1></header>
  <div class="updated-bar">Last run: <strong id="last-run-display">{now_str}</strong> (<span id="time-ago">just now</span>)</div>
  <main>{cards_html}</main>
  <div class="check-now-wrap">
    <button class="check-now-btn" id="checkBtn" onclick="triggerCheck()">CHECK NOW ⛳</button>
    <button class="check-now-btn" onclick="this.textContent='RELOADING...'; location.reload()">REFRESH ↺</button>
  </div>
  <div id="toast" class="toast">⛳ <span>Check Triggered! Updating...</span></div>
  <script>
    const lastRun = new Date("{now_iso}");
    function updateTimeAgo() {{
      const diff = Math.floor((new Date() - lastRun) / 60000);
      document.getElementById('time-ago').textContent = diff <= 0 ? 'just now' : diff + 'm ago';
    }}
    setInterval(updateTimeAgo, 10000);
    updateTimeAgo();
    function showToast() {{
      const t = document.getElementById('toast');
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 4000);
    }}
    async function triggerCheck() {{
      const btn = document.getElementById('checkBtn');
      btn.disabled = true;
      btn.textContent = 'WORKING...';
      try {{
        const resp = await fetch('/api/trigger', {{ method: 'POST' }});
        if (resp.ok) {{
          btn.textContent = '✓ QUEUED';
          showToast();
        }}
      }} catch (e) {{
        btn.disabled = false;
        btn.textContent = 'ERROR - TRY AGAIN';
      }}
    }}
  </script>
</body>
</html>"""
    Path("index.html").write_text(html)

# ── Concurrency & Main ────────────────────────────────────────────────────────

async def main():
    dates = get_upcoming_weekend_dates()
    semaphore = asyncio.Semaphore(3) # Max 3 concurrent scraping tabs

    async def throttled_scrape(course, d):
        async with semaphore:
            print(f"  Scraping {course['name']} for {d.strftime('%a %b %d')}...")
            cache_file = CACHE_DIR / course["cache_file"]
            if course["type"] == "cpsgolf":
                t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
                slots = await scrape_cpsgolf(course["url"], d, course["tee_time_min"], t_max_day)
            else:
                slots = await scrape_chronogolf(course["url"], d, course["tee_time_min"], course["tee_time_max"])
            save_cache(cache_file, d, slots)

    tasks = [throttled_scrape(course, d) for course in COURSES for d in dates]
    print(f"Starting parallel scrape of {len(tasks)} course/date combinations...")
    await asyncio.gather(*tasks)
    
    generate_html()
    print("All tasks complete.")

if __name__ == "__main__":
    asyncio.run(main())
