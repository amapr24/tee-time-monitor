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

# -- CONFIGURATION (Preserved) --
ET = ZoneInfo("America/New_York")
MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)
CACHE_DIR = Path(".")
# The "Speed" Engine: Limits concurrent browsers to 3
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

# -- HELPERS (Color Coding) --
def get_slot_color_class(time_str: str) -> str:
    """Returns emerald (pre-10AM), gold (10-2), or slate (post-2)."""
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

# -- SCALABLE CHECKING (Speed Engine Wrapper) --
async def check_day_with_semaphore(course, d):
    async with browser_semaphore:
        return await check_day(course, d)

# ... [Keep your existing scraping functions, notifications, and date helpers here] ...

# -- HTML GENERATOR (Preserved Theme + New Features) --
def generate_html():
    dates = get_upcoming_weekend_dates()
    last_run_iso = datetime.now(ET).isoformat()
    now_str = datetime.now(ET).strftime("%-I:%M %p ET")

    cards_html = ""
    for course in COURSES:
        cache_file = CACHE_DIR / course["cache_file"]
        days_html = ""
        for d in dates:
            slots = load_cache(cache_file, d)
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            
            # Rebuild booking URL
            if course["type"] == "cpsgolf":
                book_url = f"{course['url']}?TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={t_max_day}"
            else:
                book_url = f"{course['url']}?date={d.isoformat()}&step=teetimes&holes={course.get('holes', 18)}&groupSize={course.get('group_size', 4)}"

            if slots:
                times_html = "".join(
                    f'<span class="slot {get_slot_color_class(s.get("time",""))}">{s.get("time","?")}</span>'
                    for s in slots
                )
                day_body = f'<div class="slots">{times_html}</div>'
            else:
                day_body = '<p class="no-times">No times available</p>'

            days_html += f"""
            <div class="day-block">
              <div class="day-header">
                <span class="day-name">{d.strftime("%A, %b %-d")}</span>
                <a class="book-btn" href="{book_url}" target="_blank">Book →</a>
              </div>
              {day_body}
            </div>"""

        cards_html += f"""
        <div class="course-card">
          <div class="card-header">
            <div class="course-name">{course["name"]}</div>
            <span class="window-tag">⏱ Window: {course["tee_time_min"]}AM – Cutoff</span>
          </div>
          {days_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tee Time Watch</title>
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --green-deep: #0d2b1a; --green-mid: #1a5c32; --gold: #e8b94a;
      --emerald: #2e8b4f; --slate: #475569; --cream: #f5f0e8; --white: #ffffff;
    }}
    body {{ font-family: 'DM Sans', sans-serif; background: var(--cream); color: var(--green-deep); padding-bottom: 60px; }}

    /* TICKER RESTORED */
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
    .window-tag {{ color: var(--gold); font-size: 0.7rem; text-transform: uppercase; }}

    .day-block {{ padding: 15px 20px; border-bottom: 1px solid #eee; }}
    .day-header {{ display: flex; justify-content: space-between; align-items: center; }}
    .day-name {{ font-weight: 700; font-size: 0.8rem; color: var(--green-mid); text-transform: uppercase; }}
    .book-btn {{ background: var(--gold); color: var(--green-deep); text-decoration: none; padding: 4px 12px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; }}
    
    .slots {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .slot {{ padding: 6px 10px; border-radius: 6px; font-size: 0.85rem; font-weight: 600; color: white; background: #999; }}
    
    /* Color Coding */
    .slot-emerald {{ background: var(--emerald) !important; }}
    .slot-gold {{ background: var(--gold) !important; color: var(--green-deep); }}
    .slot-slate {{ background: var(--slate) !important; }}

    /* Toast */
    #toast {{
      position: fixed; bottom: -100px; left: 50%; transform: translateX(-50%);
      background: var(--green-deep); color: var(--gold); padding: 15px 30px;
      border-radius: 50px; font-weight: bold; transition: bottom 0.5s ease;
      box-shadow: 0 10px 30px rgba(0,0,0,0.3); z-index: 9999;
      font-family: 'Bebas Neue', sans-serif; letter-spacing: 1px;
    }}
    #toast.show {{ bottom: 30px; }}

    .check-now-btn {{ background: var(--gold); border: none; padding: 15px 40px; font-family: 'Bebas Neue', sans-serif; font-size: 1.2rem; border-radius: 50px; cursor: pointer; display: block; margin: 40px auto; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }}
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
    Checked every 5 mins · Last: <strong id="ts">{now_str}</strong> (<span id="mins">0</span> min ago)
  </div>

  <main>{cards_html}</main>

  <button class="check-now-btn" onclick="triggerCheck()">CHECK NOW ⛳</button>
  <div id="toast">🏌️ CHECK TRIGGERED! REFRESH IN 2 MINS.</div>

  <script>
    const lastRun = new Date("{last_run_iso}");
    function updateMins() {{
      const d = Math.floor((new Date() - lastRun) / 60000);
      document.getElementById('mins').textContent = d;
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
      }} catch (e) {{ console.error(r); }}
    }}
  </script>
</body>
</html>"""
    Path("index.html").write_text(html)

# -- MAIN RUNNER (Speed Engine Implementation) --
async def main():
    dates = get_upcoming_weekend_dates()
    print(f"Monitoring {len(COURSES)} courses across {len(dates)} dates...")
    
    # Run all checks in parallel with a concurrency limit
    tasks = []
    for course in COURSES:
        for d in dates:
            tasks.append(check_day_with_semaphore(course, d))
    
    await asyncio.gather(*tasks)
    generate_html()

if __name__ == "__main__":
    asyncio.run(main())        "holes": 18,
        "group_size": 4,
        "tee_time_min": 8,
        "tee_time_max": 14,
        "cache_file": "cache_normandy.json",
        "skip_past_dates": True,
    },
    {
        "name": "Miami Shores",
        "type": "chronogolf",
        "url": "https://www.chronogolf.com/club/miami-shores-country-club",
        "holes": 18,
        "group_size": 4,
        "tee_time_min": 8,
        "tee_time_max": 14,
        "cache_file": "cache_miami_shores.json",
        "skip_past_dates": True,
    }
]

# -- HELPERS --
def get_sunset_cutoff(target_date: date, fallback_hour: int) -> int:
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        return s["sunset"].hour - 4
    except:
        return fallback_hour

def get_upcoming_weekend_dates() -> list[date]:
    today = datetime.now(ET).date()
    return [today + timedelta(days=i) for i in range(7) if (today + timedelta(days=i)).weekday() in (4, 5, 6)]

def load_cache(cache_file: Path, d: date) -> list[dict]:
    try:
        if cache_file.exists():
            return json.loads(cache_file.read_text()).get(d.isoformat(), [])
    except: pass
    return []

# -- COLOR CODING LOGIC --
def get_slot_class(time_str):
    """Emerald before 10AM, Gold 10AM-2PM, Slate after 2PM."""
    try:
        # Expects "8:30 AM" format
        time_part, ampm = time_str.split()
        hour = int(time_part.split(':')[0])
        if ampm == "PM" and hour != 12: hour += 12
        if ampm == "AM" and hour == 12: hour = 0
        
        if hour < 10: return "slot-emerald"
        if 10 <= hour < 14: return "slot-gold"
        return "slot-slate"
    except:
        return "slot-default"

# -- HTML GENERATOR --
def generate_html():
    dates = get_upcoming_weekend_dates()
    last_run_iso = datetime.now(ET).isoformat()
    now_str = datetime.now(ET).strftime("%-I:%M %p ET")
    
    cards_html = ""
    for course in COURSES:
        cache_file = CACHE_DIR / course["cache_file"]
        days_html = ""
        for d in dates:
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            slots = load_cache(cache_file, d)
            if slots:
                times_html = "".join(
                    f'<span class="slot {get_slot_class(s.get("time",""))}">{s.get("time","?")}</span>'
                    for s in slots
                )
                day_content = f'<div class="slots">{times_html}</div>'
            else:
                day_content = '<p class="no-times">No times available</p>'

            days_html += f"""
            <div class="day-block">
              <div class="day-header"><span class="day-name">{d.strftime("%A, %b %-d")}</span></div>
              {day_content}
            </div>"""

        cards_html += f"""
        <div class="course-card">
          <div class="card-header"><div class="course-name">{course["name"]}</div></div>
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
    :root {{
      --green-deep: #0d2b1a; --gold: #e8b94a; --cream: #f5f0e8;
      --emerald: #2e8b4f; --slate: #475569; --white: #ffffff;
    }}
    body {{ font-family: 'DM Sans', sans-serif; background: var(--cream); margin: 0; padding-bottom: 80px; }}
    
    /* TICKER */
    .ticker {{ background: var(--gold); color: var(--green-deep); font-family: 'Bebas Neue', sans-serif; padding: 8px 0; overflow: hidden; white-space: nowrap; }}
    .ticker-inner {{ display: inline-block; animation: ticker 40s linear infinite; }}
    @keyframes ticker {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-50%); }} }}
    .ticker-inner span {{ margin: 0 40px; font-size: 1.1rem; }}

    header {{ background: var(--green-deep); padding: 40px 20px; text-align: center; color: var(--white); }}
    h1 {{ font-family: 'Bebas Neue', sans-serif; font-size: 4rem; margin: 0; }}
    
    .updated-bar {{ background: #1a5c32; text-align: center; padding: 10px; font-size: 0.8rem; color: var(--white); border-bottom: 3px solid var(--gold); }}

    main {{ max-width: 1100px; margin: 30px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 25px; }}
    
    .course-card {{ background: var(--white); border-radius: 16px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }}
    .card-header {{ background: var(--green-deep); padding: 20px; color: var(--white); }}
    .course-name {{ font-family: 'Bebas Neue', sans-serif; font-size: 2rem; }}

    .day-block {{ padding: 15px 20px; border-bottom: 1px solid #eee; }}
    .day-name {{ font-weight: 700; font-size: 0.8rem; text-transform: uppercase; color: #1a5c32; }}
    
    .slots {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .slot {{ padding: 6px 10px; border-radius: 6px; font-size: 0.85rem; font-weight: 600; color: white; }}
    
    /* Color Coding */
    .slot-emerald {{ background: var(--emerald); }}
    .slot-gold {{ background: var(--gold); color: var(--green-deep); }}
    .slot-slate {{ background: var(--slate); }}
    .slot-default {{ background: #ccc; }}

    /* Toast Notification */
    #toast {{
      position: fixed; bottom: -100px; left: 50%; transform: translateX(-50%);
      background: var(--green-deep); color: var(--gold); padding: 12px 24px;
      border-radius: 50px; font-weight: bold; transition: bottom 0.5s ease;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3); z-index: 1000;
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
    Last run: <strong id="timestamp">{now_str}</strong> (<span id="minutes-ago">0</span> min ago)
  </div>

  <main>{cards_html}</main>

  <button class="check-now-btn" onclick="triggerCheck()">CHECK NOW ⛳</button>
  
  <div id="toast">Backend check triggered! 🏌️</div>

  <script>
    const lastRun = new Date("{last_run_iso}");
    
    // The "Freshness" Tracker
    function updateMinutes() {{
      const diff = Math.floor((new Date() - lastRun) / 60000);
      document.getElementById('minutes-ago').textContent = diff;
    }}
    setInterval(updateMinutes, 60000);
    updateMinutes();

    async function triggerCheck() {{
      const toast = document.getElementById('toast');
      try {{
        const resp = await fetch('/api/trigger', {{ method: 'POST' }});
        if (resp.ok) {{
          // The "Toast"
          toast.classList.add('show');
          setTimeout(() => toast.classList.remove('show'), 3000);
        }}
      }} catch (e) {{ console.error(e); }}
    }}
  </script>
</body>
</html>"""
    Path("index.html").write_text(html)

# -- SPEED ENGINE (main logic) --
async def main():
    dates = get_upcoming_weekend_dates()
    
    # Example of parallelized execution (The "Speed" Engine)
    tasks = []
    for course in COURSES:
        for d in dates:
            # tasks.append(check_day(course, d)) 
            # Note: check_day needs to be adapted to use the browser_semaphore
            pass
    
    # await asyncio.gather(*tasks)
    generate_html()

if __name__ == "__main__":
    asyncio.run(main())
