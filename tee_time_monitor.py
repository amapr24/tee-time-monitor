"""
Tee Time Monitor -- Miami-area courses (CPS + Chronogolf)
Checks multiple golf courses and sends email + Pushover push notifications
when new tee times appear.

Setup:
  pip install -r requirements.txt
  playwright install chromium

To add a new course, just add an entry to the COURSES list below.
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
from datetime import date, timedelta, datetime
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo
from astral.sun import sun

try:
    from playwright.async_api import async_playwright
except ImportError:  # playwright optional when only importing parsers (e.g. tests)
    async_playwright = None  # type: ignore[assignment]

# ── Email / Pushover credentials (from GitHub secrets) ────────────────────────

SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")   # comma-separated for multiple

PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

# ── Course configuration ───────────────────────────────────────────────────────
#
# type "cpsgolf"    -- calendar-based site (Miami Lakes)
# type "chronogolf" -- date-in-URL site (Chronogolf courses; change the url slug)
#
# TEE_TIME_MIN / TEE_TIME_MAX: 24h hours. 0=midnight, 7=7AM, 18=6PM
# skip_past_dates: only needed for sites like Chronogolf that redirect past
# dates to today and would otherwise poison the cache.

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
        "name":           "Plantation Preserve",
        "address":        "7050 W Broward Blvd, Plantation",
        "phone":          "(954) 585-5020",
        "type":           "webtrac",
        "url":            "https://parks.plantation.org/webtrac/web/search.html?module=GR&display=Detail",
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_plantation.json",
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
]

# ── Constants ─────────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

DAY_NAMES = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday",  5: "Saturday", 6: "Sunday"
}

CACHE_DIR = Path(".")   # cache files live in the repo root alongside the script

# ── Sunset helper ──────────────────────────────────────────────────────────────

MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)

@lru_cache(maxsize=32)
def get_sunset_cutoff(target_date: date, fallback_hour: int) -> int:
    """
    Returns the latest hour (24h int) to start a round, defined as
    4 hours before sunset in Miami. Falls back to fallback_hour if astral fails.
    Cached so each date is only calculated once per run.
    """
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        cutoff_hour = s["sunset"].hour - 4
        print(f"  Sunset: {s['sunset'].strftime('%-I:%M %p ET')} → cutoff: {cutoff_hour:02d}:00")
        return cutoff_hour
    except Exception as e:
        print(f"  Sunset calc failed ({e}) -- using fallback {fallback_hour:02d}:00")
        return fallback_hour

# ── Date helpers ───────────────────────────────────────────────────────────────

def get_upcoming_weekend_dates() -> list[date]:
    """
    Return Fri/Sat/Sun dates within the next 6 calendar days (Eastern Time).
    Covers the usual booking window across courses.
    """
    today = datetime.now(ET).date()
    return [
        today + timedelta(days=i)
        for i in range(6)
        if (today + timedelta(days=i)).weekday() in (4, 5, 6)
    ]

# ── Time window helpers ────────────────────────────────────────────────────────

def is_within_window(time_str: str, t_min: int, t_max: int) -> bool:
    try:
        parts = time_str.strip().split()
        hour, _ = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return t_min <= hour <= t_max
    except Exception:
        return False


def is_slot_in_past(time_str: str, target_date: date) -> bool:
    """Return True if the slot time has already passed today (ET)."""
    if target_date != datetime.now(ET).date():
        return False
    try:
        parts = time_str.strip().split()
        hour, minute = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        now_et = datetime.now(ET)
        return (hour, minute) <= (now_et.hour, now_et.minute)
    except Exception:
        return False


def deduplicate_slots(slots: list[dict], t_min: int, t_max: int) -> list[dict]:
    seen = set()
    out  = []
    for slot in slots:
        if not is_within_window(slot.get("time", ""), t_min, t_max):
            continue
        key = (slot.get("time", "").strip().upper(),)
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out


def _format_hour_window_label(hour_24: int) -> str:
    """Format a whole clock hour (0-23) as '8:00 AM' / '2:00 PM' for the HTML header."""
    h = hour_24 % 24
    if h == 0:
        return "12:00 AM"
    if h < 12:
        return f"{h}:00 AM"
    if h == 12:
        return "12:00 PM"
    return f"{h - 12}:00 PM"


# ── Parsers (pure functions, testable without a browser) ─────────────────────
#
# The JS in each scraper now only *selects* elements and returns their raw
# innerText (or table cells). All regex extraction happens here so tests can
# feed saved fixtures to these parsers without launching Chromium.

_CPS_TIME_RE    = re.compile(r"(\d{1,2}:\d{2})\s*P\s*M|(\d{1,2}:\d{2})\s*A\s*M", re.I)
_CPS_HOLE_RE    = re.compile(r"\d+\s*HOLE", re.I)
_PRICE_RE       = re.compile(r"\$[\d,.]+")
_CHRONO_12H_RE  = re.compile(r"(\d{1,2}:\d{2})\s*(AM|PM)", re.I)
_CHRONO_24H_RE  = re.compile(r"\b([01]?\d|2[0-3]):(\d{2})\b")
_CHRONO_HOLE_RE = re.compile(r"(\d+)\s*hole", re.I)
_WEBTRAC_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_LEADING_INT_RE  = re.compile(r"\d+")


def _collapse(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def _normalize_time_label(time_str: str) -> str:
    """Normalize AM/PM casing so all sources render times consistently."""
    t = _collapse(time_str)
    return re.sub(r"\b(am|pm)\b", lambda m: m.group(1).upper(), t, flags=re.I)


def parse_cpsgolf_card(raw: str) -> dict | None:
    raw = _collapse(raw)
    if not raw:
        return None
    m = _CPS_TIME_RE.search(raw)
    if not m:
        return None
    time_base = m.group(1) or m.group(2)
    ampm = "PM" if m.group(1) else "AM"
    holes = _CPS_HOLE_RE.search(raw)
    price = _PRICE_RE.search(raw)
    return {
        "time":  f"{time_base} {ampm}",
        "holes": holes.group(0) if holes else "",
        "price": price.group(0) if price else "",
    }


def parse_cpsgolf(card_texts: list[str], body_text: str = "") -> list[dict]:
    out, seen = [], set()
    for raw in card_texts:
        slot = parse_cpsgolf_card(raw)
        if not slot:
            continue
        key = (slot["time"], slot["holes"])
        if key in seen:
            continue
        seen.add(key)
        out.append(slot)
    if out:
        return out
    # Fallback: scrape any time-like tokens from full body text.
    for m in _CPS_TIME_RE.finditer(_collapse(body_text)):
        time_base = m.group(1) or m.group(2)
        ampm = "PM" if m.group(1) else "AM"
        time = f"{time_base} {ampm}"
        key = (time, "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"time": time, "holes": "", "price": ""})
    return out


def parse_chronogolf_card(raw: str) -> dict | None:
    raw = _collapse(raw)
    if len(raw) < 3:
        return None
    time = ""
    m12 = _CHRONO_12H_RE.search(raw)
    if m12:
        time = f"{m12.group(1)} {m12.group(2).upper()}"
    else:
        m24 = _CHRONO_24H_RE.search(raw)
        if m24:
            h = int(m24.group(1))
            minute = m24.group(2)
            ampm = "PM" if h >= 12 else "AM"
            if h > 12: h -= 12
            if h == 0: h = 12
            time = f"{h}:{minute} {ampm}"
    if not time:
        return None
    hole = _CHRONO_HOLE_RE.search(raw)
    price = _PRICE_RE.search(raw)
    return {
        "time":  time,
        "holes": f"{hole.group(1)} holes" if hole else "",
        "price": price.group(0) if price else "",
    }


def parse_chronogolf(card_texts: list[str], body_text: str = "") -> list[dict]:
    out, seen = [], set()
    for raw in card_texts:
        slot = parse_chronogolf_card(raw)
        if not slot:
            continue
        key = (slot["time"], slot["holes"])
        if key in seen:
            continue
        seen.add(key)
        out.append(slot)
    if out:
        return out
    for m in _CHRONO_12H_RE.finditer(_collapse(body_text)):
        time = f"{m.group(1)} {m.group(2).upper()}"
        key = (time, "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"time": time, "holes": "", "price": ""})
    return out


def parse_webtrac_row(cells: list[str]) -> dict | None:
    """cells = innerText of each <td> in a results row (index-aligned)."""
    if len(cells) < 6:
        return None
    # Match JS parseInt's leniency — cell may contain icons/labels after the
    # number ("4 Open", "4\nof\n4"). Extract the first integer we see.
    m = _LEADING_INT_RE.search(cells[5] or "")
    open_slots = int(m.group(0)) if m else 0
    if open_slots == 0:
        return None
    time = _normalize_time_label(cells[1] or "")
    if not _WEBTRAC_TIME_RE.search(time):
        return None
    return {
        "time":  time,
        "price": (cells[7] if len(cells) > 7 else "").strip(),
        "holes": (cells[3] if len(cells) > 3 else "").strip() or "18 Holes",
    }


def parse_webtrac(rows: list[list[str]]) -> list[dict]:
    out = []
    for row in rows:
        slot = parse_webtrac_row(row)
        if slot:
            out.append(slot)
    return out


# ── Human-like delay helper ───────────────────────────────────────────────────

async def human_delay(page, min_ms: int = 800, max_ms: int = 2200):
    """Wait a random amount of time, like a human would between actions."""
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def goto_with_retry(page, url: str, *, attempts: int = 3, **kwargs):
    """page.goto with exponential backoff on transient network/navigation errors."""
    last_exc = None
    for i in range(attempts):
        try:
            return await page.goto(url, **kwargs)
        except Exception as e:
            last_exc = e
            if i == attempts - 1:
                break
            wait = 2 ** i
            print(f"  goto failed ({type(e).__name__}: {e}) — retry {i + 1}/{attempts - 1} in {wait}s")
            await asyncio.sleep(wait)
    raise last_exc

# ── Notifications ──────────────────────────────────────────────────────────────

def send_pushover(title: str, message: str):
    if not all([PUSHOVER_USER, PUSHOVER_TOKEN]):
        print("  Pushover credentials not set -- skipping.")
        return
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    PUSHOVER_TOKEN,
                "user":     PUSHOVER_USER,
                "title":    title,
                "message":  message,
                "sound":    "cashregister",
                "priority": 0,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            print("  Pushover notification sent.")
        else:
            print(f"  Pushover error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  Pushover exception: {e}")


def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        print("  Email credentials not set -- skipping email.")
        return
    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"  Email sent to {', '.join(recipients)}")
    except Exception as e:
        print(f"  Email error: {e}")


def notify(subject: str, body: str, push_msg: str):
#    send_email(subject, body)
    send_pushover(subject, push_msg)

# ── Browser launch helper (shared) ────────────────────────────────────────────

async def launch_browser(playwright):
    """
    Launch a single browser + context to be reused across all dates for a course.
    Shared context means cookies and cache persist between page loads, which
    looks more like a returning human visitor than a fresh bot each time.
    """
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context

# ── Scraper: CPS Golf (Miami Lakes calendar-based) ────────────────────────────

async def scrape_cpsgolf(context, course: dict, target_date: date) -> list[dict]:
    """
    Reuses the shared browser context. Opens a new page per date so each
    date is isolated, but benefits from shared cookies/cache.
    """
    base_url = course["url"]
    t_min    = course["tee_time_min"]
    t_max    = course["tee_time_max"]
    url      = f"{base_url}?TeeOffTimeMin={t_min}&TeeOffTimeMax={t_max}"

    page = await context.new_page()
    tee_times = []
    try:
        print(f"  Loading page...")
        await goto_with_retry(page, url, wait_until="networkidle", timeout=60_000)
        await human_delay(page, 2000, 4000)

        # Navigate to correct month
        target_month_str = target_date.strftime("%B %Y")
        print(f"  Looking for month: {target_month_str}")

        for _ in range(12):
            header = await page.evaluate("""
                () => {
                    const pat = /^[A-Za-z]+ \\d{4}$/;
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        const t = node.textContent.trim();
                        if (pat.test(t)) return t;
                    }
                    return '';
                }
            """)
            header = (header or "").strip()

            if target_month_str in header:
                print(f"  Month: {header}")
                break
            if not header:
                print(f"  Header not found -- proceeding anyway.")
                break

            print(f"  Advancing from '{header}'...")
            await page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const t = (el.innerText || '').trim();
                        if (t === '\u203a' || t === '>' || t === '\u25b6' || t === '\u2192') {
                            el.click(); return true;
                        }
                    }
                    for (const el of document.querySelectorAll('[aria-label]')) {
                        if ((el.getAttribute('aria-label') || '').toLowerCase().includes('next')) {
                            el.click(); return true;
                        }
                    }
                    return false;
                }
            """)
            await human_delay(page, 600, 1200)

        # Click the day
        day_num = str(target_date.day)
        print(f"  Clicking day {day_num}...")

        clicked = await page.evaluate(f"""
            () => {{
                const target = '{day_num}';
                const all = document.querySelectorAll('div, span, a, button, li');
                for (const el of all) {{
                    const text = (el.innerText || '').trim();
                    if (text !== target) continue;
                    const classes = (el.className || '').toLowerCase();
                    if (['gray','grey','disabled','prev','next','old','muted','inactive']
                            .some(c => classes.includes(c))) continue;
                    const children = el.querySelectorAll('*');
                    let childHasText = false;
                    for (const child of children) {{
                        if ((child.innerText || '').trim() === target) {{
                            childHasText = true; break;
                        }}
                    }}
                    if (childHasText) continue;
                    el.click();
                    return 'clicked: ' + el.tagName + ' class=' + el.className;
                }}
                return null;
            }}
        """)

        if not clicked:
            print(f"  Could not find day {day_num}.")
            return []

        print(f"  {clicked}")
        await human_delay(page, 3000, 5000)

        card_texts, body_text = await page.evaluate("""
            () => {
                const cardSelectors = [
                    '[class*="teetime"]', '[class*="tee-time"]',
                    '[class*="timeslot"]', '[class*="time-slot"]',
                    '[class*="booking"]',  '[class*="result-item"]',
                    '[class*="search-result"]', '[class*="tee-card"]',
                    '[data-time]', '[data-teetime]',
                    '.timeslot-item', '.tee-time-slot',
                    'div[role="button"]', 'button[aria-label*="time"]'
                ];
                let cards = [];
                for (const sel of cardSelectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0) { cards = Array.from(found); break; }
                }
                const texts = cards.map(c => (c.innerText || '')).filter(t => t.trim());
                return [texts, document.body.innerText || ''];
            }
        """)
        tee_times = parse_cpsgolf(card_texts, body_text)

    finally:
        await page.close()

    print(f"  Raw slots found: {len(tee_times)}")
    return tee_times

# ── Scraper: Chronogolf (date in URL) ─────────────────────────────────────────

async def scrape_chronogolf(context, course: dict, target_date: date) -> list[dict]:
    """
    Reuses the shared browser context. Opens a new page per date so each
    date is isolated, but benefits from shared cookies/cache.
    """
    base_url   = course["url"]
    holes      = course.get("holes", 18)
    group_size = course.get("group_size", 4)
    url = (
        f"{base_url}"
        f"?date={target_date.isoformat()}"
        f"&step=teetimes"
        f"&holes={holes}"
        f"&coursesIds="
        f"&deals=false"
        f"&groupSize={group_size}"
    )
    print(f"  URL: {url}")

    page = await context.new_page()
    tee_times = []
    try:
        print(f"  Loading page...")
        await goto_with_retry(page, url, wait_until="networkidle", timeout=60_000)
        await human_delay(page, 3000, 6000)

        try:
            await page.wait_for_selector(
                '[class*="teetime"], [class*="tee-time"], [class*="timeslot"], '
                '[class*="booking"], [class*="slot"], [class*="green-fee"]',
                timeout=10_000
            )
            print("  Tee time elements detected.")
        except Exception:
            print("  Timed out waiting for tee time elements -- scraping anyway.")

        title = await page.title()
        print(f"  Page title: {title}")

        card_texts, body_text = await page.evaluate("""
            () => {
                const cardSelectors = [
                    '[class*="teetime-card"]',
                    '[class*="teetime"]',
                    '[class*="tee-time"]',
                    '[class*="green-fee"]',
                    '[class*="timeslot"]',
                    '[class*="time-slot"]',
                    '[class*="booking-slot"]',
                    '[class*="slot-item"]',
                    '[class*="availability"]',
                    '[class*="tee-card"]',
                    'app-teetime', 'app-tee-time', 'app-slot'
                ];
                let cards = [];
                for (const sel of cardSelectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0) { cards = Array.from(found); break; }
                }
                const texts = cards.map(c => (c.innerText || '')).filter(t => t.trim());
                return [texts, document.body.innerText || ''];
            }
        """)
        tee_times = parse_chronogolf(card_texts, body_text)

        if not tee_times and os.environ.get("DEBUG_SCRAPE"):
            print(f"  DEBUG page text:\n{body_text[:200]}\n")

    finally:
        await page.close()

    print(f"  Raw slots found: {len(tee_times)}")
    return tee_times

# ── Scraper: WebTrac (Plantation Preserve) ────────────────────────────────────

async def scrape_webtrac(context, course: dict, target_date: date) -> list[dict]:
    """
    WebTrac (Plantation Preserve) scraper.

    The form is a plain GET form. We load the base URL to acquire a session
    cookie and CSRF token, then navigate directly to the results URL with all
    parameters pre-set. This bypasses unreliable JS injection into the hidden
    Vue-managed date/time pickers and avoids the default 11:00 pm begintime
    that would filter out all morning tee times.

    Row structure confirmed from live DOM inspection (2026-04-16):
      td[0]  cart button
      td[1]  time  ("7:00 am")
      td[2]  date  ("04/18/2026")
      td[3]  holes ("18 (Front)")
      td[4]  course name
      td[5]  open slots count
      td[6]  per-slot status labels
      td[7]  cost (often empty)
    """
    page = await context.new_page()
    tee_times = []
    try:
        print(f"  Loading WebTrac base page to acquire session...")
        base_url = course["url"].split("?")[0]
        await goto_with_retry(page, base_url + "?module=GR&display=Detail", wait_until="networkidle", timeout=60_000)
        await human_delay(page, 1000, 2000)

        csrf = await page.evaluate("() => document.querySelector('#_csrf_token')?.value || ''")
        if not csrf:
            print("  WARNING: could not find CSRF token — search may fail.")

        date_str = target_date.strftime("%m/%d/%Y")
        print(f"  Navigating to results for {date_str}...")

        # begintime="12:00 am" returns all tee times from opening; the caller's
        # tee_time_min/max window filtering trims them to the desired range.
        params = {
            "Action":                 "Start",
            "SubAction":              "",
            "_csrf_token":            csrf,
            "secondarycode":          "",
            "numberofplayers":        "1",
            "begindate":              date_str,
            "begintime":              "12:00 am",
            "numberofholes":          "18",
            "reservee":               "",
            "display":                "Detail",
            "module":                 "GR",
            "multiselectlist_value":  "",
            "grwebsearch_buttonsearch": "yes",
        }
        await goto_with_retry(page, base_url + "?" + urlencode(params), wait_until="networkidle", timeout=60_000)

        try:
            await page.wait_for_selector("#grwebsearch_output_table", timeout=15_000)
        except Exception:
            if os.environ.get("DEBUG_SCRAPE"):
                snippet = await page.evaluate("() => document.body.innerText.slice(0, 300)")
                print(f"  DEBUG page text:\n{snippet}\n")
            print("  No results table — no tee times available for this date.")
            return []

        await human_delay(page, 500, 1000)

        rows = await page.evaluate("""
            () => {
                const rows = document.querySelectorAll('#grwebsearch_output_table tbody tr');
                return Array.from(rows).map(row =>
                    Array.from(row.querySelectorAll('td')).map(td => (td.innerText || '').trim())
                );
            }
        """)
        tee_times = parse_webtrac(rows)

    finally:
        await page.close()

    print(f"  Raw slots found: {len(tee_times)}")
    return tee_times


# ── Cache ──────────────────────────────────────────────────────────────────────

def load_cache(cache_file: Path, d: date) -> list[dict]:
    key = d.isoformat()
    try:
        if cache_file.exists():
            return json.loads(cache_file.read_text()).get(key, [])
    except Exception:
        pass
    return []


def save_cache(cache_file: Path, d: date, slots: list[dict]):
    all_cache = {}
    try:
        if cache_file.exists():
            content = cache_file.read_text().strip()
            if content:
                data = json.loads(content)
                all_cache = data if isinstance(data, dict) else {}
    except Exception:
        pass
    all_cache[d.isoformat()] = slots
    cache_file.write_text(json.dumps(all_cache, indent=2))
    print(f"  Cache updated for {d.strftime('%A, %B %-d')}")


def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]

# ── Per-course, per-day check ──────────────────────────────────────────────────

async def check_day(context, course: dict, target_date: date):
    """Check a single date for a course, reusing the shared browser context."""
    name       = course["name"]
    t_min      = course["tee_time_min"]
    t_max      = get_sunset_cutoff(target_date, course["tee_time_max"])
    cache_file = CACHE_DIR / course["cache_file"]
    day_name   = DAY_NAMES.get(target_date.weekday(), "Unknown")

    print(f"\n{'='*60}")
    print(f"{name} -- {day_name}, {target_date.strftime('%B %-d, %Y')}")
    print(f"  Time window: {t_min:02d}:00 - {t_max:02d}:59")
    print(f"{'='*60}\n")

    # Skip past dates for sites that redirect them (e.g. Chronogolf)
    if course.get("skip_past_dates") and target_date < datetime.now(ET).date():
        print(f"  Skipping past date to avoid redirect.\n")
        return

    # Skip today if we're already past the monitoring window
    now_et = datetime.now(ET)
    if target_date == now_et.date() and now_et.hour >= t_max:
        print(f"  Skipping today -- already past {t_max:02d}:00 ET.\n")
        return

    # Scrape based on site type, passing the shared context
    if course["type"] == "cpsgolf":
        raw = await scrape_cpsgolf(context, course, target_date)
    elif course["type"] == "chronogolf":
        raw = await scrape_chronogolf(context, course, target_date)
    elif course["type"] == "webtrac":
        raw = await scrape_webtrac(context, course, target_date)
    else:
        print(f"  Unknown site type: {course['type']}")
        return

    current_slots = deduplicate_slots(raw, t_min, t_max)
    print(f"  Found {len(current_slots)} unique slot(s) after dedup.")

    cached_slots   = load_cache(cache_file, target_date)
    new_slots      = find_new_slots(cached_slots, current_slots)
    new_slot_times = {s.get("time", "").strip().upper() for s in new_slots}
    for s in current_slots:
        s["is_new"] = s.get("time", "").strip().upper() in new_slot_times

    save_cache(cache_file, target_date, current_slots)

    if not current_slots:
        print("  No slots found -- skipping.\n")
        return

    if new_slots:
        print(f"  {len(new_slots)} NEW slot(s)!")

        date_label = f"{day_name}, {target_date.strftime('%B %-d, %Y')}"
        subject    = f"Tee Time Alert - {name} {target_date.strftime('%a %b %-d')}"

        if course["type"] == "cpsgolf":
            book_url = f"{course['url']}?TeeOffTimeMin={t_min}&TeeOffTimeMax={t_max}"
        else:
            book_url = (
                f"{course['url']}?date={target_date.isoformat()}"
                f"&step=teetimes&holes={course.get('holes', 18)}"
                f"&coursesIds=&deals=false&groupSize={course.get('group_size', 4)}"
            )

        lines = [f"New tee time(s) just opened at {name}", f"for {date_label}:\n"]
        for slot in new_slots:
            line = f"  - {slot.get('time', '?')}"
            if slot.get("holes"): line += f"  |  {slot['holes']}"
            if slot.get("price"): line += f"  |  {slot['price']}"
            lines.append(line)
        lines.append(f"\nBook here:\n{book_url}")
        body = "\n".join(lines)

        slot_list = ", ".join(s.get("time", "?") for s in new_slots)
        push_msg  = f"{len(new_slots)} new slot(s) on {date_label}:\n{slot_list}\n\nBook: {book_url}"

        notify(subject, body, push_msg)
    else:
        print("  No new slots since last check.")

# ── Per-course runner (one browser for all dates) ─────────────────────────────

async def check_course(playwright, course: dict, dates: list[date]):
    """
    Launch ONE browser for the entire course, reuse the context across all
    dates, then close when done. Human-like: same session, persistent cookies.
    Each date is wrapped in its own try/except so a single timeout or crash
    skips that date rather than aborting the remaining ones.
    """
    print(f"\n{'#'*60}")
    print(f"  {course['name']}  ({len(dates)} date(s))")
    print(f"{'#'*60}")

    browser, context = await launch_browser(playwright)
    try:
        for i, d in enumerate(dates):
            try:
                await check_day(context, course, d)
            except Exception as e:
                print(f"  ⚠️  Error on {course['name']} {d} -- skipping date. ({type(e).__name__}: {e})")
            # Small random pause between dates — looks like a human thinking
            if i < len(dates) - 1:
                delay = random.uniform(1.5, 3.5)
                print(f"  Waiting {delay:.1f}s before next date...")
                await asyncio.sleep(delay)
    finally:
        await browser.close()
        print(f"\n  Browser closed for {course['name']}.")

# ── HTML generator ────────────────────────────────────────────────────────────

def _slot_time_class(time_str: str) -> str:
    """Return CSS class based on time of day: early (<9 AM), midday (9–11:59), afternoon (12+)."""
    try:
        t = time_str.strip().upper()
        is_pm = t.endswith("PM")
        digits = t.replace("AM", "").replace("PM", "").strip()
        h = int(digits.split(":")[0])
        if is_pm and h != 12:
            h += 12
        elif not is_pm and h == 12:
            h = 0
        if h < 9:
            return "slot slot--early"
        if h < 12:
            return "slot slot--midday"
        return "slot slot--afternoon"
    except Exception:
        return "slot"


def generate_html():
    dates = get_upcoming_weekend_dates()
    now_str = datetime.now(ET).strftime("%-I:%M %p ET, %A %B %-d, %Y")
    now_ts  = int(datetime.now(ET).timestamp())

    # Build data structure: course -> date -> slots
    course_data = []
    for course in COURSES:
        cache_file = CACHE_DIR / course["cache_file"]
        days = []
        for d in dates:
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            raw_slots = load_cache(cache_file, d)
            slots = [
                s for s in deduplicate_slots(raw_slots, course["tee_time_min"], t_max_day)
                if not is_slot_in_past(s.get("time", ""), d)
            ]
            if course["type"] == "cpsgolf":
                book_url = f"{course['url']}?TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={t_max_day}"
            else:
                book_url = (
                    f"{course['url']}?date={d.isoformat()}"
                    f"&step=teetimes&holes={course.get('holes', 18)}"
                    f"&coursesIds=&deals=false&groupSize={course.get('group_size', 4)}"
                )
            days.append({
                "date":     d,
                "label":    d.strftime("%A, %b %-d"),
                "slots":    slots,
                "book_url": book_url,
            })
        course_data.append({"course": course, "days": days})

    # Sunset info for header
    first_date = dates[0]
    s = sun(MIAMI.observer, date=first_date, tzinfo=ET)
    actual_sunset = s["sunset"].strftime("%-I:%M %p")

    min_start_hour = min(c["tee_time_min"] for c in COURSES)
    max_fallback_hour = max(c["tee_time_max"] for c in COURSES)
    repr_cutoff = get_sunset_cutoff(first_date, max_fallback_hour)
    end_hour = repr_cutoff + 1
    end_ampm = _format_hour_window_label(end_hour)
    start_ampm = _format_hour_window_label(min_start_hour)

    # Build course cards HTML
    cards_html = ""
    for cd in course_data:
        course = cd["course"]
        days_html = ""
        for day in cd["days"]:
            if day["slots"]:
                items_html = ""
                for s in day["slots"]:
                    t    = s.get("time", "?")
                    cls  = _slot_time_class(t)
                    if s.get("is_new"):
                        cls += " slot--new"
                    badge = '<span class="new-badge" aria-label="New">NEW</span>' if s.get("is_new") else ""
                    price = (" · " + s["price"]) if s.get("price") else ""
                    items_html += f'<li class="{cls}" role="listitem">{badge}{t}{price}</li>'
                day_body = f'<ul class="slots" role="list" aria-label="Available tee times">{items_html}</ul>'
            else:
                day_body = '<p class="no-times">No times available</p>'

            days_html += f"""
            <div class="day-block" aria-label="Times for {day['label']}">
              <div class="day-header">
                <span class="day-name">{day["label"]}</span>
                <a class="book-btn" href="{day["book_url"]}" target="_blank" aria-label="Book tee time for {day['label']}">Book →</a>
              </div>
              {day_body}
            </div>"""

        cards_html += f"""
        <div class="course-card" aria-label="{course['name']}">
          <div class="card-header">
            <div class="course-name">{course["name"]}</div>
            <div class="course-meta">{course.get("address","")}</div>
            <div class="course-meta"><a href="tel:{course.get('phone','')}">{course.get('phone','')}</a></div>
          </div>
          {days_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <meta name="format-detection" content="telephone=no">
  <title>Tee Time Watch</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⛳</text></svg>">
  <link rel="manifest" href="manifest.json">
  <meta name="theme-color" content="#0d2b1a">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Tee Time Watch">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,400&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    /* ── Design tokens (light mode) ── */
    :root {{
      --green-deep:   #0d2b1a;
      --green-mid:    #1a5c32;
      --green-light:  #2e8b4f;
      --green-pale:   #d4eddc;
      --fairway:      #3a7d44;
      --white:        #ffffff;
      --cream:        #f5f0e8;
      --gold:         #e8b94a;
      --gold-dark:    #c49a28;
      --text-dark:    #0d2b1a;
      --text-mid:     #2e4d38;
      --text-light:   #5a7a66;
      --text-on-brand:#ffffff;
      --text-on-brand-muted: rgba(255,255,255,0.8);
      --text-on-brand-subtle: rgba(255,255,255,0.78);
      --text-on-brand-faint: rgba(255,255,255,0.5);
      --divider-on-brand: rgba(255,255,255,0.15);
      /* time-of-day slot palettes */
      --early-bg:     #fff8e6;
      --early-border: #f0c060;
      --early-text:   #5c3a00;
      --midday-bg:    #1e3828;
      --midday-border:rgba(46,139,79,0.4);
      --midday-text:  #a8e0b8;
      --afternoon-bg: #ddeeff;
      --afternoon-border:#7aaedd;
      --afternoon-text:#0d2448;
      /* surfaces */
      --surface:      #ffffff;
      --bg:           #f5f0e8;
      --border-soft:  #edf5f0;
    }}

    /* ── Dark mode overrides ── */
    [data-theme="dark"] {{
      --cream:           #0f1a13;
      --bg:              #0f1a13;
      --surface:         #1a2e20;
      --text-dark:       #e8f0eb;
      --text-mid:        #a8c4b0;
      --text-light:      #6a9470;
      --text-on-brand:   #f3f8f4;
      --text-on-brand-muted: rgba(243,248,244,0.84);
      --text-on-brand-subtle: rgba(243,248,244,0.8);
      --text-on-brand-faint: rgba(243,248,244,0.58);
      --divider-on-brand: rgba(243,248,244,0.18);
      --green-pale:      #1e3828;
      --border-soft:     #243d2c;
      --early-bg:        #2e2410;
      --early-border:    #7a5a10;
      --early-text:      #f0c060;
      --midday-bg:       #173322;
      --midday-border:   rgba(104, 184, 130, 0.55);
      --midday-text:     #e6f5ea;
      --afternoon-bg:    #0d1f2e;
      --afternoon-border:#3a6a9e;
      --afternoon-text:  #90c0ee;
    }}

    body {{
      font-family: 'DM Sans', sans-serif;
      background: var(--bg);
      color: var(--text-dark);
      min-height: 100vh;
      overflow-x: hidden;
      transition: background 0.25s, color 0.25s;
    }}

    /* ── Screen-reader only utility ── */
    .sr-only {{
      position: absolute; width: 1px; height: 1px;
      padding: 0; margin: -1px; overflow: hidden;
      clip: rect(0,0,0,0); white-space: nowrap; border: 0;
    }}

    /* ── Scrolling ticker ── */
    .ticker {{
      background: var(--gold);
      color: var(--green-deep);
      font-family: 'Bebas Neue', sans-serif;
      font-size: 1rem;
      letter-spacing: 0.15em;
      padding: 6px 0;
      overflow: hidden;
      white-space: nowrap;
    }}
    .ticker-inner {{
      display: inline-block;
      animation: ticker 36s linear infinite;
      will-change: transform;
    }}
    .ticker:hover .ticker-inner,
    .ticker:focus-within .ticker-inner {{ animation-play-state: paused; }}
    .ticker-inner span {{ margin: 0 48px; }}
    @keyframes ticker {{
      0%   {{ transform: translateX(0); }}
      100% {{ transform: translateX(-50%); }}
    }}

    /* ── Header ── */
    header {{
      background: var(--green-deep);
      padding: 32px 30px;
      text-align: center;
      position: relative;
      overflow: hidden;
    }}
    header::after {{
      content: '';
      position: absolute;
      bottom: 0; left: 0; right: 0;
      height: 12px;
      background: repeating-linear-gradient(
        90deg,
        var(--fairway) 0px, var(--fairway) 8px,
        var(--green-mid) 8px, var(--green-mid) 16px
      );
    }}
    header::before {{
      content: '';
      position: absolute;
      inset: 0;
      background-image: radial-gradient(rgba(255,255,255,0.04) 1px, transparent 1px);
      background-size: 24px 24px;
    }}
    .header-inner {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
      align-items: center;
      position: relative;
      width: 100%;
      max-width: 760px;
      margin: 0 auto;
    }}
    .header-flag {{
      font-size: 4.5rem;
      animation: flagwave 3s ease-in-out infinite;
      transform-origin: bottom center;
      justify-self: end;
      padding-right: 20px;
    }}
    .header-golfer {{
      font-size: 4.5rem;
      animation: flagwave 3s ease-in-out infinite;
      transform-origin: bottom center;
      animation-direction: reverse;
      justify-self: start;
      padding-left: 20px;
    }}
    @keyframes flagwave {{
      0%, 100% {{ transform: rotate(-3deg); }}
      50%       {{ transform: rotate(3deg); }}
    }}
    h1 {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: clamp(3.5rem, 10vw, 6rem);
      color: var(--text-on-brand);
      letter-spacing: 0.08em;
      padding-left: 0.08em;
      line-height: 0.9;
      position: relative;
      white-space: nowrap;
    }}
    h1 br {{ display: none; }}
    h1 em {{
      color: var(--gold);
      font-style: normal;
    }}
    .subtitle {{
      font-size: 0.9rem;
      color: var(--text-on-brand-muted);
      margin-top: 12px;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      position: relative;
    }}
    .window-tag {{
      color: var(--gold);
      font-size: 0.8rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      padding: 14px 0 0px;
      display: block;
    }}

    /* ── Sunset pill ── */
    .sunset-pill {{
      background: var(--gold);
      color: var(--green-deep);
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 800;
      margin-left: 5px;
      display: inline-block;
      vertical-align: baseline;
    }}

    /* ── Updated bar ── */
    .updated-bar {{
      background: var(--green-mid);
      text-align: center;
      padding: 10px 24px;
      font-size: 0.88rem;
      color: var(--text-on-brand-muted);
      letter-spacing: 0.04em;
      border-bottom: 3px solid var(--gold);
    }}
    .updated-bar strong {{ color: var(--text-on-brand); font-weight: 500; }}

    /* ── Main grid ── */
    main {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px 20px 20px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 24px;
      align-items: start;
    }}

    /* ── Course card ── */
    .course-card {{
      background: var(--surface);
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 4px 24px rgba(13,43,26,0.12), 0 1px 4px rgba(13,43,26,0.08);
      transition: transform 0.2s, box-shadow 0.2s;
      border: 1px solid rgba(13,43,26,0.06);
    }}
    .course-card:hover {{
      transform: translateY(-4px);
      box-shadow: 0 12px 40px rgba(13,43,26,0.18), 0 2px 8px rgba(13,43,26,0.1);
    }}
    .card-header {{
      background: linear-gradient(135deg, var(--green-deep) 0%, var(--green-mid) 100%);
      padding: 20px 24px 12px;
      position: relative;
      overflow: hidden;
    }}
    .card-header::before {{
      content: '⛳';
      position: absolute;
      right: 16px;
      top: 12px;
      font-size: 2.5rem;
      opacity: 0.15;
    }}
    .course-name {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: 1.8rem;
      color: var(--text-on-brand);
      letter-spacing: 0.06em;
      line-height: 1;
    }}
    .course-meta {{
      font-size: 0.78rem;
      color: var(--text-on-brand-subtle);
      letter-spacing: 0.04em;
      margin-top: 4px;
    }}
    .card-header a {{
      color: var(--text-on-brand-subtle);
      text-decoration: none;
    }}

    /* ── Day block ── */
    .day-block {{
      padding: 14px 20px;
      border-bottom: 1px solid var(--border-soft);
      background: var(--surface);
      transition: background 0.25s;
    }}
    .day-block:last-child {{ border-bottom: none; }}
    .day-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 15px;
    }}
    .day-name {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: 1.05rem;
      font-weight: 400;
      letter-spacing: 0.08em;
      color: var(--text-dark);
    }}
    .book-btn {{
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--gold-dark);
      background: rgba(232, 185, 74, 0.12);
      text-decoration: none;
      padding: 5px 12px;
      border-radius: 20px;
      border: 1px solid rgba(232, 185, 74, 0.35);
      transition: background 0.15s, color 0.15s, transform 0.1s, box-shadow 0.15s;
      letter-spacing: 0.04em;
    }}
    .book-btn:hover {{
      background: var(--gold);
      color: var(--green-deep);
      border-color: var(--gold);
      transform: translateY(-1px);
    }}
    .book-btn:focus-visible {{
      outline: 2.5px solid var(--gold);
      outline-offset: 2px;
    }}

    /* ── Slots list ── */
    .slots {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      list-style: none;
      padding: 0;
    }}
    .slot {{
      font-size: 0.82rem;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 6px;
      border: 1px solid;
      font-variant-numeric: tabular-nums;
      min-width: 82px;
      text-align: center;
      transition: background 0.15s, color 0.15s, transform 0.1s, box-shadow 0.15s;
      cursor: default;
    }}
    .slot:hover {{
      transform: translateY(-1px);
      box-shadow: 0 3px 8px rgba(0,0,0,0.15);
      filter: brightness(0.94);
    }}
    .slot:focus-visible {{
      outline: 2.5px solid var(--gold);
      outline-offset: 2px;
    }}

    /* time-of-day variants */
    .slot--early {{
      background: var(--early-bg);
      border-color: var(--early-border);
      color: var(--early-text);
    }}
    .slot--midday {{
      background: var(--midday-bg);
      border-color: var(--midday-border);
      color: var(--midday-text);
    }}
    .slot--afternoon {{
      background: var(--afternoon-bg);
      border-color: var(--afternoon-border);
      color: var(--afternoon-text);
    }}

    /* new-slot highlight */
    .slot--new {{
      box-shadow: 0 0 0 1.5px var(--gold);
      animation: pulse-new 2s ease-in-out 3;
    }}
    .new-badge {{
      font-size: 0.6rem;
      background: var(--gold);
      color: var(--green-deep);
      border-radius: 3px;
      padding: 0 4px;
      margin-right: 5px;
      font-weight: 800;
      vertical-align: middle;
    }}
    @keyframes pulse-new {{
      0%, 100% {{ box-shadow: 0 0 0 1.5px var(--gold); }}
      50%       {{ box-shadow: 0 0 0 5px rgba(232,185,74,0.35); }}
    }}

    .no-times {{
      font-size: 0.82rem;
      color: var(--text-light);
      font-style: italic;
    }}

    /* ── Callout strip ── */
    .callout-strip {{
      background: var(--green-deep);
      padding: 14px 24px;
      margin: 20px 20px 0;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 16px;
      max-width: 1060px;
      margin-left: auto;
      margin-right: auto;
    }}
    .callout-quote {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: clamp(1rem, 3vw, 1.5rem);
      color: var(--gold);
      letter-spacing: 0.1em;
      text-align: center;
      flex: 1;
    }}
    .callout-quote span {{
      color: var(--text-on-brand-faint);
      font-size: 0.55em;
      display: block;
      letter-spacing: 0.2em;
      font-family: 'DM Sans', sans-serif;
      font-weight: 300;
      margin-top: 2px;
    }}
    .callout-divider {{
      width: 1px;
      height: 28px;
      background: var(--divider-on-brand);
      flex-shrink: 0;
    }}

    /* ── Minutes ago ── */
    .mins-ago {{
      font-size: 0.72rem;
      color: var(--gold);
      margin-left: 6px;
      font-weight: 500;
    }}

    /* ── Toast ── */
    .toast {{
      position: fixed;
      bottom: 32px;
      left: 50%;
      transform: translateX(-50%) translateY(80px);
      background: var(--green-deep);
      color: var(--text-on-brand);
      padding: 12px 28px;
      border-radius: 40px;
      font-size: 0.85rem;
      font-weight: 500;
      letter-spacing: 0.04em;
      border: 1px solid var(--gold);
      box-shadow: 0 8px 32px rgba(0,0,0,0.25);
      transition: transform 0.3s ease, opacity 0.3s ease;
      opacity: 0;
      z-index: 999;
      white-space: nowrap;
    }}
    .toast.show {{
      transform: translateX(-50%) translateY(0);
      opacity: 1;
    }}

    /* ── Dark mode toggle button ── */
    #theme-toggle {{
      position: fixed;
      bottom: 24px;
      right: 20px;
      background: var(--green-deep);
      border: 1.5px solid var(--gold);
      color: var(--gold);
      border-radius: 50%;
      width: 44px;
      height: 44px;
      font-size: 1.15rem;
      cursor: pointer;
      z-index: 200;
      transition: transform 0.2s, background 0.2s;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 2px 12px rgba(0,0,0,0.25);
    }}
    #theme-toggle:hover {{ transform: scale(1.12) rotate(15deg); }}
    #theme-toggle:focus-visible {{
      outline: 2.5px solid var(--gold);
      outline-offset: 3px;
    }}

    /* ── Footer ── */
    footer {{
      text-align: center;
      padding: 32px 24px;
      font-size: 0.75rem;
      color: var(--text-light);
      border-top: 2px solid rgba(13,43,26,0.08);
      margin-top: 24px;
    }}
    .footer-fore {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: 1.4rem;
      color: var(--green-light);
      letter-spacing: 0.2em;
      display: block;
      margin-bottom: 8px;
      opacity: 0.4;
    }}

    /* ════════════════════════════════════════════
       MOBILE OVERRIDES  (≤ 767px)
       ════════════════════════════════════════════ */
    @media (max-width: 767px) {{

      /* Ticker */
      .ticker {{ font-size: 0.82rem; }}

      /* Header */
      header {{ padding: 14px 0; }}
      header::after {{ height: 6px; }}

      .header-inner {{
        display: block;
        position: relative;
        text-align: center;
        padding: 0 55px;
        max-width: 100%;
      }}

      .header-flag {{
        position: absolute;
        left: 10px;
        top: 28%;
        font-size: 2.4rem;
        padding: 0;
        margin: 0;
        animation: flagwave-mobile 3s ease-in-out infinite;
      }}
      .header-golfer {{
        position: absolute;
        right: 10px;
        top: 28%;
        font-size: 2.4rem;
        padding: 0;
        margin: 0;
        animation: flagwave-mobile 3s ease-in-out infinite;
        animation-direction: reverse;
      }}

      @keyframes flagwave-mobile {{
        0%, 100% {{ transform: translateY(-28%) rotate(-3deg); }}
        50%       {{ transform: translateY(-28%) rotate(3deg); }}
      }}

      .header-inner > div {{
        width: 100%;
        text-align: center;
        display: block;
      }}

      header .subtitle {{
        white-space: nowrap !important;
        text-align: center !important;
        width: 100% !important;
        display: block !important;
        font-size: 0.75rem;
        letter-spacing: 0.08em;
      }}

      h1 {{ font-size: 3rem; letter-spacing: 0.12em; white-space: normal; }}
      h1 br {{ display: inline; }}
      .window-tag {{ font-size: 0.78rem; letter-spacing: 0.05em; padding: 3px 0 4px; white-space: nowrap; }}
      .sunset-pill {{ font-size: 0.68rem; padding: 1px 5px; }}

      .updated-bar {{ font-size: 0.78rem; padding: 6px 15px; }}

      main {{ margin: 15px auto 0; padding: 16px 12px 0; gap: 15px; }}

      .course-card {{ border-radius: 12px; }}
      .course-card:hover {{ transform: none; }}
      .card-header {{ padding: 12px 15px; }}
      .card-header::before {{ font-size: 1.8rem; opacity: 0.12; top: 8px; right: 12px; }}
      .course-name {{ font-size: 1.5rem; }}
      .course-meta {{ font-size: 0.68rem; }}

      .day-block {{ padding: 10px 15px; min-height: 58px; position: relative; }}
      .day-header {{ margin-bottom: 6px; }}
      .day-name {{ font-size: 0.88rem; }}

      .book-btn {{
        position: absolute;
        top: 10px;
        right: 12px;
        font-size: 0.6rem;
        font-weight: 700;
        background: var(--gold);
        color: var(--green-deep);
        padding: 3px 10px;
        border-radius: 20px;
        border: none;
        width: auto;
      }}
      .book-btn:hover {{
        background: var(--gold-dark);
        color: var(--green-deep);
      }}

      .slots {{ gap: 4px; padding-right: 50px; }}
      .slot {{
        font-size: 0.75rem;
        padding: 4px 8px;
        border-radius: 4px;
        min-width: auto;
      }}
      .no-times {{ font-size: 0.75rem; }}

      .callout-strip {{ display: none; }}

      footer {{ padding: 20px; font-size: 0.7rem; margin-top: 12px; }}
      .footer-fore {{ display: none; }}

      .toast {{ bottom: 80px; font-size: 0.8rem; padding: 10px 20px; }}

      #theme-toggle {{ bottom: 20px; right: 14px; width: 40px; height: 40px; font-size: 1rem; }}
    }}
  </style>
</head>
<body>

  <span class="sr-only">Tee Time Watch — Miami area golf weekend availability</span>
  <div class="ticker" aria-hidden="true">
    <div class="ticker-inner">
      <span>FORE! ⛳</span>
      <span>TEE TIME WATCH 🏌️</span>
      <span>MIAMI AREA GOLF ⛳</span>
      <span>BOOK BEFORE THEY'RE GONE 🏌️</span>
      <span>DON'T THREE PUTT ⛳</span>
      <span>WEEKEND AVAILABILITY 🏌️</span>
      <span>AVOID THREE PUTTS! ⛳</span>
      <span>JUST BOOK THE TEE TIME 🏌️</span>
      <span>FORE! ⛳</span>
      <span>TEE TIME WATCH 🏌️</span>
      <span>MIAMI AREA GOLF ⛳</span>
      <span>BOOK BEFORE THEY'RE GONE 🏌️</span>
      <span>DON'T THREE PUTT ⛳</span>
      <span>WEEKEND AVAILABILITY 🏌️</span>
      <span>AVOID THREE PUTTS! ⛳</span>
      <span>JUST BOOK THE TEE TIME 🏌️</span>
    </div>
  </div>

  <header>
    <div class="header-inner">
      <span class="header-flag">⛳</span>
      <div>
        <h1>TEE <em>TIME</em><br> WATCH</h1>
        <p class="subtitle">Miami Area Golf &nbsp;·&nbsp; Weekend Availability</p>
        <span class="window-tag">⏱ {start_ampm} – {end_ampm} <span class="sunset-pill">SUNSET: {actual_sunset}</span></span>
      </div>
      <span class="header-golfer">🏌️</span>
    </div>
  </header>

  <div class="updated-bar">
    Checked every 15 minutes <br>Last run: <strong>{now_str}</strong><span class="mins-ago" id="mins-ago"></span>
  </div>

  <main aria-label="Course tee times">
    {cards_html}
  </main>

  <div class="callout-strip">
    <div class="callout-quote">FORE!<span>heads up</span></div>
    <div class="callout-divider"></div>
    <div class="callout-quote">BOOK FAST<span>they go quick</span></div>
    <div class="callout-divider"></div>
    <div class="callout-quote">TEE IT UP<span>weekend's calling</span></div>
  </div>

  <footer>
    <span class="footer-fore">⛳ 🏌️ ⛳</span>
    Monitoring {len(COURSES)} courses · Fri–Sun · Times shown in ET<br>
    © {datetime.now(ET).year} Tee Time Watch
  </footer>

  <button id="theme-toggle" aria-label="Toggle dark mode" title="Toggle dark mode">🌙</button>

  <script>
    // ── Dark mode ──
    (function() {{
      const btn  = document.getElementById('theme-toggle');
      const root = document.documentElement;
      const saved = localStorage.getItem('ttw-theme');
      if (saved === 'dark') {{ root.dataset.theme = 'dark'; btn.textContent = '☀️'; }}
      btn.addEventListener('click', function() {{
        const isDark = root.dataset.theme === 'dark';
        root.dataset.theme = isDark ? '' : 'dark';
        btn.textContent = isDark ? '🌙' : '☀️';
        localStorage.setItem('ttw-theme', isDark ? '' : 'dark');
      }});
    }})();

    // ── Minutes ago counter ──
    (function() {{
      const el = document.getElementById('mins-ago');
      if (!el) return;
      const lastRun = new Date({now_ts} * 1000);
      function update() {{
        const mins = Math.floor((Date.now() - lastRun) / 60000);
        if (mins < 1)       el.textContent = ' · just now';
        else if (mins < 60) el.textContent = ' · ' + mins + ' min' + (mins === 1 ? '' : 's') + ' ago';
        else                el.textContent = ' · ' + Math.floor(mins/60) + 'h ago';
      }}
      update();
      setInterval(update, 30000);
    }})();
  </script>

</body>
</html>"""

    Path("index.html").write_text(html)
    print("  index.html generated.")

# ── Main ───────────────────────────────────────────────────────────────────────

def _select_courses(filter_terms: list[str]) -> list[dict]:
    """Match case-insensitive substrings against course names. Empty = all."""
    if not filter_terms:
        return COURSES
    terms = [t.lower() for t in filter_terms]
    picked = [c for c in COURSES if any(t in c["name"].lower() for t in terms)]
    if not picked:
        names = ", ".join(c["name"] for c in COURSES)
        sys.exit(f"No course matched {filter_terms!r}. Known: {names}")
    return picked


async def main(courses: list[dict]):
    dates = get_upcoming_weekend_dates()

    print(f"\nTee Time Monitor")
    print(f"  Courses: {', '.join(c['name'] for c in courses)}")
    print(f"  Dates:   {', '.join(d.strftime('%a %b %-d') for d in dates)}\n")

    async with async_playwright() as playwright:
        results = await asyncio.gather(
            *[check_course(playwright, course, dates) for course in courses],
            return_exceptions=True,
        )
        for course, result in zip(courses, results):
            if isinstance(result, Exception):
                print(f"\n  ⚠️  {course['name']} failed entirely: {type(result).__name__}: {result}")

    print("\n" + "="*60)
    # Skip HTML regen on a filtered local run so the user can't accidentally
    # commit a stale index.html built from only a subset of caches.
    if len(courses) == len(COURSES):
        print("Generating index.html...")
        generate_html()
    else:
        print("Filtered run — skipping index.html regeneration.")
    print("Done.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tee Time Monitor")
    parser.add_argument(
        "--course", "-c", action="append", default=[],
        help="Filter to courses whose name contains this substring (case-insensitive). "
             "Repeat to select multiple. Default: all courses.",
    )
    args = parser.parse_args()
    asyncio.run(main(_select_courses(args.course)))
