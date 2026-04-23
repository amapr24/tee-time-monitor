"""
Tee Time Monitor -- Miami-area courses (CPS + Chronogolf)
Checks multiple golf courses and sends email + Pushover push notifications
when new tee times appear.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import smtplib
import sys
import logging
from datetime import date, timedelta, datetime
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo
from astral.sun import sun
from jinja2 import Template

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

# ── Logging Configuration ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── Email / Pushover credentials ───────────────────────────────────────────────

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
        "tee_time_min":   6,
        "tee_time_max":   15,
        "cache_file":     "cache_miami_lakes.json",
    },
    {
        "name":           "Miami Beach",
        "address":        "2301 Alton Rd, Miami Beach",
        "phone":          "(305) 532-3350",
        "type":           "chronogolf",
        "url":            "https://www.chronogolf.com/club/miami-beach-golf-club",
        "holes":          18,
        "group_size":     4,
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_miami_beach.json",
        "skip_past_dates": True,
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
    },
    {
        "name":           "Plantation Preserve",
        "address":        "7050 W Broward Blvd, Plantation",
        "phone":          "(954) 585-5020",
        "type":           "webtrac",
        "url":            "https://parks.plantation.org/webtrac/web/search.html?module=GR&display=Detail",
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_plantation.json",
    },
]

ET = ZoneInfo("America/New_York")
DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
CACHE_DIR = Path(".")
MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)

@lru_cache(maxsize=32)
def get_sunset_cutoff(target_date: date, fallback_hour: int) -> int:
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        return s["sunset"].hour - 4
    except Exception as e:
        logger.error(f"Sunset calc failed for {target_date}: {e}")
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
    except Exception: return False

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
        if not is_within_window(slot.get("time", ""), t_min, t_max): continue
        key = (slot.get("time", "").strip().upper(),)
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out

_CPS_TIME_RE    = re.compile(r"(\d{1,2}:\d{2})\s*P\s*M|(\d{1,2}:\d{2})\s*A\s*M", re.I)
_CPS_HOLE_RE    = re.compile(r"\d+\s*HOLE", re.I)
_PRICE_RE       = re.compile(r"\$[\d,.]+")
_CHRONO_12H_RE  = re.compile(r"(\d{1,2}:\d{2})\s*(AM|PM)", re.I)
_CHRONO_24H_RE  = re.compile(r"\b([01]?\d|2[0-3]):(\d{2})\b")
_CHRONO_HOLE_RE = re.compile(r"(\d+)\s*hole", re.I)
_WEBTRAC_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_LEADING_INT_RE  = re.compile(r"\d+")

def _collapse(raw: str) -> str: return re.sub(r"\s+", " ", raw or "").strip()
def _normalize_time_label(time_str: str) -> str:
    t = _collapse(time_str)
    return re.sub(r"\b(am|pm)\b", lambda m: m.group(1).upper(), t, flags=re.I)

def parse_cpsgolf_card(raw: str) -> dict | None:
    raw = _collapse(raw)
    if not raw: return None
    m = _CPS_TIME_RE.search(raw)
    if not m: return None
    time_base = m.group(1) or m.group(2)
    ampm = "PM" if m.group(1) else "AM"
    holes, price = _CPS_HOLE_RE.search(raw), _PRICE_RE.search(raw)
    return {"time": f"{time_base} {ampm}", "holes": holes.group(0) if holes else "", "price": price.group(0) if price else ""}

def parse_cpsgolf(card_texts: list[str], body_text: str = "") -> list[dict]:
    out, seen = [], set()
    for raw in card_texts:
        slot = parse_cpsgolf_card(raw)
        if not slot: continue
        key = (slot["time"], slot["holes"])
        if key in seen: continue
        seen.add(key)
        out.append(slot)
    if out: return out
    for m in _CPS_TIME_RE.finditer(_collapse(body_text)):
        time_base = m.group(1) or m.group(2)
        ampm = "PM" if m.group(1) else "AM"
        time = f"{time_base} {ampm}"
        key = (time, "")
        if key in seen: continue
        seen.add(key)
        out.append({"time": time, "holes": "", "price": ""})
    return out

def parse_chronogolf_card(raw: str) -> dict | None:
    raw = _collapse(raw)
    if len(raw) < 3: return None
    time = ""
    m12 = _CHRONO_12H_RE.search(raw)
    if m12: time = f"{m12.group(1)} {m12.group(2).upper()}"
    else:
        m24 = _CHRONO_24H_RE.search(raw)
        if m24:
            h, minute = int(m24.group(1)), m24.group(2)
            ampm = "PM" if h >= 12 else "AM"
            if h > 12: h -= 12
            if h == 0: h = 12
            time = f"{h}:{minute} {ampm}"
    if not time: return None
    hole, price = _CHRONO_HOLE_RE.search(raw), _PRICE_RE.search(raw)
    return {"time": time, "holes": f"{hole.group(1)} holes" if hole else "", "price": price.group(0) if price else ""}

def parse_chronogolf(card_texts: list[str], body_text: str = "") -> list[dict]:
    out, seen = [], set()
    for raw in card_texts:
        slot = parse_chronogolf_card(raw)
        if not slot: continue
        key = (slot["time"], slot["holes"])
        if key in seen: continue
        seen.add(key)
        out.append(slot)
    if out: return out
    for m in _CHRONO_12H_RE.finditer(_collapse(body_text)):
        time = f"{m.group(1)} {m.group(2).upper()}"
        key = (time, "")
        if key in seen: continue
        seen.add(key)
        out.append({"time": time, "holes": "", "price": ""})
    return out

def parse_webtrac_row(cells: list[str]) -> dict | None:
    if len(cells) < 6: return None
    m = _LEADING_INT_RE.search(cells[5] or "")
    
    # FIX: Change '== 0' to '!= 4' to ensure ONLY slots with exactly 4 players are detected
    if (int(m.group(0)) if m else 0) != 4: 
        return None
        
    time = _normalize_time_label(cells[1] or "")
    if not _WEBTRAC_TIME_RE.search(time): return None
    return {"time": time, "price": (cells[7] if len(cells) > 7 else "").strip(), "holes": (cells[3] if len(cells) > 3 else "").strip() or "18 Holes"}


def parse_webtrac(rows: list[list[str]]) -> list[dict]:
    out = []
    for row in rows:
        slot = parse_webtrac_row(row)
        if slot: out.append(slot)
    return out

async def human_delay(page, min_ms: int = 800, max_ms: int = 2200):
    await page.wait_for_timeout(random.randint(min_ms, max_ms))

async def goto_with_retry(page, url: str, *, attempts: int = 3, **kwargs):
    last_exc = None
    for i in range(attempts):
        try: return await page.goto(url, **kwargs)
        except Exception as e:
            last_exc = e
            if i == attempts - 1: break
            await asyncio.sleep(2 ** i)
    raise last_exc

def send_pushover(title: str, message: str):
    if not all([PUSHOVER_USER, PUSHOVER_TOKEN]): return
    try: requests.post("https://api.pushover.net/1/messages.json", data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": message, "sound": "cashregister", "priority": 0}, timeout=10)
    except Exception as e: logger.error(f"Pushover error: {e}")

def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]): return
    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    msg = MIMEText(body, "plain")
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_SENDER, ", ".join(recipients)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
    except Exception as e: logger.error(f"Email error: {e}")

def notify(subject: str, body: str, push_msg: str):
    send_pushover(subject, push_msg)

async def launch_browser(playwright):
    browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 900})
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return browser, context

async def scrape_cpsgolf(context, course: dict, target_date: date) -> list[dict]:
    base_url, t_min, t_max = course["url"], course["tee_time_min"], course["tee_time_max"]
    url = f"{base_url}?TeeOffTimeMin={t_min}&TeeOffTimeMax={t_max}"
    page = await context.new_page()
    try:
        await goto_with_retry(page, url, wait_until="networkidle", timeout=60_000)
        await human_delay(page, 2000, 4000)
        target_month_str = target_date.strftime("%B %Y")
        for _ in range(12):
            header = await page.evaluate("() => { const pat = /^[A-Za-z]+ \\d{4}$/; const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false); let node; while ((node = walker.nextNode())) { const t = node.textContent.trim(); if (pat.test(t)) return t; } return ''; }")
            if target_month_str in (header or "").strip(): break
            if not header: break
            await page.evaluate("() => { for (const el of document.querySelectorAll('*')) { const t = (el.innerText || '').trim(); if (t === '\\u203a' || t === '>' || t === '\\u25b6' || t === '\\u2192') { el.click(); return true; } } return false; }")
            await human_delay(page, 600, 1200)
        day_num = str(target_date.day)
        clicked = await page.evaluate(f"() => {{ const target = '{day_num}'; const all = document.querySelectorAll('div, span, a, button, li'); for (const el of all) {{ if ((el.innerText || '').trim() !== target) continue; const classes = (el.className || '').toLowerCase(); if (['gray','grey','disabled','prev','next','old','muted','inactive'].some(c => classes.includes(c))) continue; el.click(); return true; }} return null; }}")
        if not clicked: return []
        await human_delay(page, 3000, 5000)
        card_texts, body_text = await page.evaluate("() => { const selectors = ['[class*=\"teetime\"]', '[class*=\"tee-time\"]', '[class*=\"timeslot\"]', '[class*=\"time-slot\"]', '[class*=\"booking\"]', '[class*=\"result-item\"]', '[class*=\"search-result\"]', '[class*=\"tee-card\"]']; let cards = []; for (const sel of selectors) { const found = document.querySelectorAll(sel); if (found.length > 0) { cards = Array.from(found); break; } } return [cards.map(c => c.innerText), document.body.innerText]; }")
        return parse_cpsgolf(card_texts, body_text)
    finally: await page.close()

async def scrape_chronogolf(context, course: dict, target_date: date) -> list[dict]:
    base_url = course["url"]
    url = f"{base_url}?date={target_date.isoformat()}&step=teetimes&holes={course.get('holes', 18)}&coursesIds=&deals=false&groupSize={course.get('group_size', 4)}"
    page = await context.new_page()
    try:
        await goto_with_retry(page, url, wait_until="networkidle", timeout=60_000)
        await human_delay(page, 3000, 6000)
        card_texts, body_text = await page.evaluate("() => { const selectors = ['[class*=\"teetime-card\"]', '[class*=\"teetime\"]', '[class*=\"tee-time\"]', '[class*=\"green-fee\"]']; let cards = []; for (const sel of selectors) { const found = document.querySelectorAll(sel); if (found.length > 0) { cards = Array.from(found); break; } } return [cards.map(c => c.innerText), document.body.innerText]; }")
        return parse_chronogolf(card_texts, body_text)
    finally: await page.close()

async def scrape_webtrac(context, course: dict, target_date: date) -> list[dict]:
    page = await context.new_page()
    try:
        base_url = course["url"].split("?")[0]
        await goto_with_retry(page, base_url + "?module=GR&display=Detail", wait_until="networkidle", timeout=60_000)
        csrf = await page.evaluate("() => document.querySelector('#_csrf_token')?.value || ''")
        params = {"Action": "Start", "_csrf_token": csrf, "numberofplayers": "1", "begindate": target_date.strftime("%m/%d/%Y"), "begintime": "12:00 am", "numberofholes": "18", "display": "Detail", "module": "GR", "grwebsearch_buttonsearch": "yes"}
        await goto_with_retry(page, base_url + "?" + urlencode(params), wait_until="networkidle", timeout=60_000)
        rows = await page.evaluate("() => Array.from(document.querySelectorAll('#grwebsearch_output_table tbody tr')).map(row => Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()))")
        return parse_webtrac(rows)
    finally: await page.close()

def load_cache(cache_file: Path, d: date) -> list[dict]:
    try:
        if cache_file.exists(): return json.loads(cache_file.read_text()).get(d.isoformat(), [])
    except Exception as e: logger.error(f"Cache load error for {d}: {e}")
    return []

def save_cache(cache_file: Path, d: date, slots: list[dict]):
    all_cache = {}
    try:
        if cache_file.exists():
            content = cache_file.read_text().strip()
            if content: all_cache = json.loads(content)
    except Exception as e: logger.error(f"Cache read error for {d}: {e}")
    all_cache[d.isoformat()] = slots
    try: cache_file.write_text(json.dumps(all_cache, indent=2))
    except Exception as e: logger.error(f"Cache write error for {d}: {e}")

def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]

async def check_day(context, course: dict, target_date: date):
    """Check a single date and return new slots found."""
    name, day_name = course["name"], DAY_NAMES.get(target_date.weekday(), "Unknown")
    t_min, t_max = course["tee_time_min"], get_sunset_cutoff(target_date, course["tee_time_max"])
    cache_file = CACHE_DIR / course["cache_file"]
    
    if course.get("skip_past_dates") and target_date < datetime.now(ET).date(): return []
    
    if course["type"] == "cpsgolf": raw = await scrape_cpsgolf(context, course, target_date)
    elif course["type"] == "chronogolf": raw = await scrape_chronogolf(context, course, target_date)
    elif course["type"] == "webtrac": raw = await scrape_webtrac(context, course, target_date)
    else: return []
    
    current_slots = deduplicate_slots(raw, t_min, t_max)
    cached_slots = load_cache(cache_file, target_date)
    new_slots = find_new_slots(cached_slots, current_slots) if cached_slots else []
    
    new_slot_times = {s.get("time", "").strip().upper() for s in new_slots}
    for s in current_slots: s["is_new"] = s.get("time", "").strip().upper() in new_slot_times
    save_cache(cache_file, target_date, current_slots)

    if not current_slots:
        print("  No slots found -- skipping.\n")
        return new_slots

    if new_slots:
        print(f"  {len(new_slots)} NEW slot(s)!")
        date_label = f"{day_name}, {target_date.strftime('%B %-d, %Y')}"
        subject = f"Tee Time Alert - {name} {target_date.strftime('%a %b %-d')}"
        book_url = f"{course['url']}?TeeOffTimeMin={t_min}&TeeOffTimeMax={t_max}" if course["type"] == "cpsgolf" else \
                   f"{course['url']}?date={target_date.isoformat()}&step=teetimes&holes={course.get('holes', 18)}&coursesIds=&deals=false&groupSize={course.get('group_size', 4)}"

        lines = [f"New tee time(s) just opened at {name}", f"for {date_label}:\n"]
        for s in new_slots:
            line = f"  - {s.get('time', '?')}"
            if s.get("holes"): line += f"  |  {s['holes']}"
            if s.get("price"): line += f"  |  {s['price']}"
            lines.append(line)
        lines.append(f"\nBook here:\n{book_url}")
        
        slot_list = ", ".join(s.get("time", "?") for s in new_slots)
        push_msg = f"{len(new_slots)} new slot(s) on {date_label}:\n{slot_list}\n\nBook: {book_url}"
        notify(subject, "\n".join(lines), push_msg)
    else:
        print("  No new slots since last check.")

    return new_slots

async def check_course(playwright, course: dict, dates: list[date]):
    """Manage browser for course and group notifications by course."""
    browser, context = await launch_browser(playwright)
    course_new_slots = {} # date_str -> list of slots

    try:
        for d in dates:
            try:
                new_slots = await check_day(context, course, d)
                if new_slots:
                    course_new_slots[d.strftime("%a %b %-d")] = new_slots
            except Exception as e: logger.exception(f"Error checking {course['name']} on {d}: {e}")
            await asyncio.sleep(random.uniform(1.5, 3.5))

        if course_new_slots:
            name = course["name"]
            subject = f"Tee Time Alert - {name}"
            
            # Construct a single detailed message
            lines = [f"New tee time(s) opened at {name}:"]
            for date_label, slots in course_new_slots.items():
                lines.append(f"\n📅 {date_label}:")
                for s in slots:
                    lines.append(f"  - {s.get('time', '?')}")
            
            book_url = course["url"]
            lines.append(f"\nBook here: {book_url}")
            body = "\n".join(lines)
            
            # NOW: Pass the full grouped list into the Pushover message instead of a short summary
            push_msg = body
            
            notify(subject, body, push_msg)
            
    finally: await browser.close()

def _slot_time_class(time_str: str, target_date: date, sunset_dt: datetime) -> str:
    try:
        t_str = time_str.strip().upper()
        is_pm = t_str.endswith("PM")
        time_parts = t_str.replace("AM", "").replace("PM", "").strip().split(":")
        h, m = int(time_parts[0]), int(time_parts[1]) if len(time_parts) > 1 else 0
        h_24 = h + 12 if is_pm and h != 12 else 0 if not is_pm and h == 12 else h
        slot_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=h_24, minute=m, tzinfo=ET)
        if (sunset_dt - slot_dt).total_seconds() <= 16_200: return "slot--twilight"
        if h_24 < 10: return "slot--early"
        if h_24 < 12: return "slot--midday"
        return "slot--afternoon"
    except Exception: return ""

# ── Jinja2 HTML Template ───────────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tee Time Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --brand-green: #4a7c59; --gold: #ffb703; --sunset-orange: #fb8500; --text-main: #1a1a1a; --text-sub: #4b5563; --slot-text: #111111; --surface: #ffffff; --bg: #f3f4f6; --border: #e5e7eb;
      --early-bg: #fef9c3; --early-brd: #facc15; --early-text: #111111;
      --midday-bg: #dcfce7; --midday-brd: #4ade80; --midday-text: #111111;
      --afternoon-bg: #dbeafe; --afternoon-brd: #60a5fa; --afternoon-text: #111111;
      --twilight-bg: #f3e8ff; --twilight-brd: #c084fc; --twilight-text: #111111;
    }
    [data-theme="dark"] {
      --bg: #141c25; --surface: #1c2733; --border: #2e3f50; --text-main: #dde6ef; --text-sub: #7e96ad; --slot-text: #dde6ef; --brand-green: #5fa374;
      --early-bg: #2a2410; --early-brd: #c9a030; --early-text: #e8d5a0;
      --midday-bg: #0f2318; --midday-brd: #57a875; --midday-text: #8fd4a8;
      --afternoon-bg: #152030; --afternoon-brd: #5b91cc; --afternoon-text: #a8caed;
      --twilight-bg: #1e1630; --twilight-brd: #9b6ec8; --twilight-text: #c4a0e8;
    }
    * { box-sizing: border-box; }
    body { background: var(--bg); color: var(--text-main); font-family: 'Inter', sans-serif; margin: 0; padding-bottom: 50px; transition: background 0.3s, color 0.3s; }
    header { background: var(--brand-green); padding: 12px 15px; text-align: center; color: white; border-bottom: 3px solid var(--gold); }
    [data-theme="dark"] header { background: #0d1720; }
    h1 { font-family: 'Bebas Neue', sans-serif; font-size: 2.8rem; margin: 0; letter-spacing: 1.5px; line-height: 1; color: white; }
    [data-theme="dark"] h1 { color: var(--brand-green); text-shadow: 0 0 20px rgba(95, 163, 116, 0.4); }
    .sunset-box { background: var(--sunset-orange); display: inline-flex; padding: 3px 10px; border-radius: 6px; font-weight: 800; font-size: 0.75rem; margin-top: 5px; color: white; }
    .header-status { margin-top: 8px; font-size: 0.7rem; font-weight: 600; opacity: 0.9; }
    .header-status b { color: var(--gold); }
    .legend { background: var(--surface); border-bottom: 1px solid var(--border); padding: 7px 12px; display: flex; justify-content: center; gap: 14px; flex-wrap: wrap; }
    .legend-item { display: flex; align-items: center; gap: 5px; font-size: 0.65rem; font-weight: 700; color: var(--text-sub); text-transform: uppercase; }
    .legend-dot { width: 12px; height: 12px; border-radius: 3px; border: 2px solid transparent; }
    .legend-dot--early { background: var(--early-bg); border-color: var(--early-brd); }
    .legend-dot--midday { background: var(--midday-bg); border-color: var(--midday-brd); }
    .legend-dot--afternoon { background: var(--afternoon-bg); border-color: var(--afternoon-brd); }
    .legend-dot--twilight { background: var(--twilight-bg); border-color: var(--twilight-brd); }
    .filter-bar { position: sticky; top: 0; z-index: 50; background: var(--surface); padding: 10px; display: flex; justify-content: center; gap: 6px; border-bottom: 1px solid var(--border); }
    .filter-btn { background: var(--bg); border: 1px solid var(--border); color: var(--text-sub); padding: 5px 12px; border-radius: 15px; font-size: 0.7rem; font-weight: 700; cursor: pointer; transition: background 0.2s, color 0.2s; }
    .filter-btn.active { background: var(--brand-green); color: white; border-color: var(--brand-green); }
    main { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 12px; padding: 12px; max-width: 1400px; margin: 0 auto; }
    .course-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; height: fit-content; }
    .card-header { padding: 10px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
    .collapsible-header { cursor: pointer; }
    .header-title-group { display: flex; align-items: center; gap: 8px; }
    .course-name { font-weight: 800; font-size: 0.95rem; color: var(--brand-green); }
    .collapse-icon { font-size: 0.6rem; transition: transform 0.3s; color: var(--text-sub); display: inline-block; line-height: 1; transform-origin: 50% 50%; transform: rotate(0deg); }
    .course-card.is-collapsed .collapse-icon { transform: rotate(-90deg); }
    .course-card.is-collapsed .card-body { display: none; }
    .day-row { padding: 10px 14px; border-bottom: 1px solid var(--border); }
    .day-row:last-child { border-bottom: none; }
    .day-row-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .day-label { font-weight: 700; font-size: 0.8rem; }
    .book-btn { color: var(--brand-green); font-size: 0.65rem; font-weight: 800; text-decoration: none; text-transform: uppercase; border: 1px solid var(--brand-green); padding: 2px 6px; border-radius: 4px; }
    .slots { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 5px; list-style: none; padding: 0; margin: 0; }
    .slots li { position: relative; padding: 3px 4px; text-align: center; border-radius: 5px; border: 2px solid transparent; font-size: 0.75rem; font-weight: 650; }
    .slot--early { background: var(--early-bg); border-color: var(--early-brd); color: var(--early-text); }
    .slot--midday { background: var(--midday-bg); border-color: var(--midday-brd); color: var(--midday-text); }
    .slot--afternoon { background: var(--afternoon-bg); border-color: var(--afternoon-brd); color: var(--afternoon-text); }
    .slot--twilight { background: var(--twilight-bg); border-color: var(--twilight-brd); color: var(--twilight-text); }
    .slot--new { box-shadow: 0 0 0 2px var(--gold); animation: pulse-new 2s ease-in-out 15; }
    .new-badge { position: absolute; top: -8px; left: 50%; transform: translateX(-50%); font-size: 0.5rem; background: var(--gold); color: #000; padding: 1px 4px; border-radius: 2px; }
    @keyframes pulse-new { 0%, 100% { box-shadow: 0 0 0 2px var(--gold); } 50% { box-shadow: 0 0 0 5px rgba(255,183,3,0.4); } }
    .no-slots { font-size: 0.7rem; color: var(--text-sub); font-style: italic; text-align: center; padding: 5px 0; }
    .empty-state { text-align: center; padding: 40px 20px; color: var(--text-sub); font-size: 0.9rem; font-weight: 600; max-width: 600px; margin: 0 auto 40px auto; }
    #theme-toggle { position: fixed; bottom: 15px; right: 15px; width: 40px; height: 40px; border-radius: 50%; background: var(--brand-green); color: var(--gold); border: none; cursor: pointer; z-index: 100; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }
    @media (max-width: 480px) { html { font-size: 115%; } main { grid-template-columns: 1fr; padding: 8px; } h1 { font-size: 2.2rem; } .slots { grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); } }
  </style>
</head>
<body>
  <header>
    <h1>TEE TIME MONITOR</h1>
    <div class="sunset-box">☀️ SUNSET: {{ actual_sunset }}</div>
    <div class="header-status">
      <div style="margin-bottom:4px"><b>Checked every 15 minutes</b></div>
      <div>Last updated: <span id="time-ago">just now</span> (<span id="last-ts">{{ now_str }}</span>)</div>
    </div>
  </header>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot legend-dot--early"></div>Early (pre-10 AM)</div>
    <div class="legend-item"><div class="legend-dot legend-dot--midday"></div>Midday (10 AM–noon)</div>
    <div class="legend-item"><div class="legend-dot legend-dot--afternoon"></div>Afternoon (noon+)</div>
    <div class="legend-item"><div class="legend-dot legend-dot--twilight"></div>Twilight</div>
  </div>
  <div class="filter-bar">
    <button class="filter-btn active" data-day="Friday">Friday</button>
    <button class="filter-btn active" data-day="Saturday">Saturday</button>
    <button class="filter-btn active" data-day="Sunday">Sunday</button>
  </div>
  <main id="course-grid">
    {% for c in courses %}
    <div class="course-card {{ 'is-collapsed' if not c.any_slots else '' }}" id="card-{{ c.safe_id }}">
      <div class="{{ 'card-header collapsible-header' if c.any_slots else 'card-header' }}">
        <div class="header-title-group">
          {% if c.any_slots %}<span class="collapse-icon">▼</span>{% endif %}
          <span class="course-name">{{ c.display_name }}</span>
        </div>
      </div>
      <div class="card-body">
        {% if c.any_slots %}
          {% for day in c.days %}
          <div class="day-row" data-day="{{ day.weekday }}">
            <div class="day-row-header">
              <span class="day-label">{{ day.label }}</span>
              <a class="book-btn" href="{{ day.book_url }}" target="_blank">Book</a>
            </div>
            {% if day.slots %}
              <ul class="slots">
                {% for s in day.slots %}
                <li class="{{ s.cls }} {{ 'slot--new' if s.is_new else '' }}">
                  {% if s.is_new %}<span class="new-badge">NEW</span>{% endif %}
                  {{ s.time }}
                </li>
                {% endfor %}
              </ul>
            {% else %}
              <div class="no-slots">No times available</div>
            {% endif %}
          </div>
          {% endfor %}
        {% else %}
          <div class="day-row"><div class="no-slots">Fully booked for the weekend.</div></div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </main>
  <div id="empty-state-msg" style="display:none" class="empty-state">Select a day above to see available times.</div>
  <button id="theme-toggle">🌙</button>
  <script>
    const updateTs = {{ now_ts }};
    function updateTime() {
      const diff = Math.floor(Date.now() / 1000) - updateTs;
      const mins = Math.floor(diff / 60);
      document.getElementById('time-ago').textContent = mins <= 0 ? 'just now' : mins + 'm ago';
    }
    setInterval(updateTime, 30_000);
    updateTime();
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        btn.classList.toggle('active');
        const day = btn.dataset.day;
        document.querySelectorAll(`.day-row[data-day="${day}"]`).forEach(r => r.style.display = btn.classList.contains('active') ? 'block' : 'none');
        const activeBtns = document.querySelectorAll('.filter-btn.active');
        document.getElementById('empty-state-msg').style.display = activeBtns.length === 0 ? 'block' : 'none';
      });
    });
    document.querySelectorAll('.collapsible-header').forEach(h => {
      h.addEventListener('click', () => { h.closest('.course-card').classList.toggle('is-collapsed'); });
    });
    const themeBtn = document.getElementById('theme-toggle');
    themeBtn.addEventListener('click', () => {
      const isDark = document.documentElement.dataset.theme === 'dark';
      document.documentElement.dataset.theme = isDark ? '' : 'dark';
      themeBtn.textContent = isDark ? '🌙' : '☀️';
    });
    setInterval(async () => {
      try {
        const r = await fetch('version.json?_=' + Date.now());
        const data = await r.json();
        if (data.ts > updateTs) location.reload();
      } catch(e) {}
    }, 30_000);
  </script>
</body>
</html>
"""

def generate_html():
    dates = get_upcoming_weekend_dates()
    now_dt = datetime.now(ET)
    now_str, now_ts = now_dt.strftime("%-I:%M %p ET, %a %b %-d"), int(now_dt.timestamp())
    
    course_data = []
    for course in COURSES:
        days_for_course = []
        total_slots_count = 0
        for d in dates:
            cache_file = CACHE_DIR / course["cache_file"]
            sunset_dt = sun(MIAMI.observer, date=d, tzinfo=ET)["sunset"]
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            raw_slots = load_cache(cache_file, d)
            slots = [s for s in deduplicate_slots(raw_slots, course["tee_time_min"], t_max_day) if not is_slot_in_past(s.get("time", ""), d)]
            total_slots_count += len(slots)
            
            book_url = f"{course['url']}?date={d.isoformat()}" if course["type"] == "chronogolf" else course["url"]
            days_for_course.append({
                "date_obj": d, "sunset_dt": sunset_dt, "label": d.strftime("%a %b %-d"), "weekday": d.strftime("%A"),
                "slots": [{"time": s.get("time", "?"), "is_new": s.get("is_new", False), "cls": _slot_time_class(s.get("time", "?"), d, sunset_dt)} for s in slots],
                "book_url": book_url
            })

        any_slots = total_slots_count > 0
        display_name = f"{course['name']} ({total_slots_count} slots)" if any_slots else f"{course['name']} (Fully Booked)"
        course_data.append({
            "name": course["name"], "safe_id": course["name"].replace(" ", "-").lower(),
            "display_name": display_name, "any_slots": any_slots, "days": days_for_course
        })

    actual_sunset = sun(MIAMI.observer, date=dates[0], tzinfo=ET)["sunset"].strftime("%-I:%M %p")
    
    template = Template(HTML_TEMPLATE)
    html_out = template.render(
        courses=course_data, 
        actual_sunset=actual_sunset, 
        now_str=now_str, 
        now_ts=now_ts
    )

    Path("index.html").write_text(html_out)
    Path("version.json").write_text(json.dumps({"ts": now_ts}))

def _select_courses(filter_terms: list[str]) -> list[dict]:
    if not filter_terms: return COURSES
    terms = [t.lower() for t in filter_terms]
    picked = [c for c in COURSES if any(t in c["name"].lower() for t in terms)]
    if not picked: sys.exit(f"No course matched {filter_terms!r}.")
    return picked

async def main(courses: list[dict]):
    dates = get_upcoming_weekend_dates()
    async with async_playwright() as playwright:
        await asyncio.gather(*[check_course(playwright, course, dates) for course in courses], return_exceptions=True)
    if len(courses) == len(COURSES):
        generate_html()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", "-c", action="append", default=[])
    args = parser.parse_args()
    asyncio.run(main(_select_courses(args.course)))
