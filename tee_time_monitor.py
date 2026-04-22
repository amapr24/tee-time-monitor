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

def _slot_time_class(time_str: str, target_date: date, sunset_dt: datetime) -> str:
    """
    Returns the CSS class based on time of day and proximity to sunset.
    Priority: Twilight (within 4.5h of sunset) > Afternoon > Midday > Early.

    CSS class → colour mapping:
      slot--early      gold/yellow   pre-10 AM
      slot--midday     green         10 AM – noon
      slot--afternoon  blue          noon+
      slot--twilight   purple        within 4.5 h of sunset
    """
    try:
        t_str = time_str.strip().upper()
        is_pm = t_str.endswith("PM")
        time_parts = t_str.replace("AM", "").replace("PM", "").strip().split(":")
        h = int(time_parts[0])
        m = int(time_parts[1]) if len(time_parts) > 1 else 0

        h_24 = h
        if is_pm and h != 12:   h_24 += 12
        elif not is_pm and h == 12: h_24 = 0

        slot_dt = datetime.combine(target_date, datetime.min.time()).replace(
            hour=h_24, minute=m, tzinfo=ET
        )

        # 1. Twilight: within 4.5 hours (16 200 s) of sunset
        if (sunset_dt - slot_dt).total_seconds() <= 16_200:
            return "slot--twilight"

        # 2. Standard time-of-day buckets
        if h_24 < 10: return "slot--early"
        if h_24 < 12: return "slot--midday"
        return "slot--afternoon"
    except Exception:
        return ""


def generate_html():
    dates   = get_upcoming_weekend_dates()
    now_dt  = datetime.now(ET)
    now_str = now_dt.strftime("%-I:%M %p ET, %a %b %-d")
    now_ts  = int(now_dt.timestamp())

    course_data = []
    for course in COURSES:
        days_for_course = []
        for d in dates:
            cache_file = CACHE_DIR / course["cache_file"]

            s_info    = sun(MIAMI.observer, date=d, tzinfo=ET)
            sunset_dt = s_info["sunset"]

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

            days_for_course.append({
                "date_obj":  d,
                "sunset_dt": sunset_dt,
                "label":     d.strftime("%a %b %-d"),
                "weekday":   d.strftime("%A"),
                "slots":     slots,
                "book_url":  book_url,
            })

        course_data.append({"name": course["name"], "days": days_for_course})

    actual_sunset = sun(MIAMI.observer, date=dates[0], tzinfo=ET)["sunset"].strftime("%-I:%M %p")

    # ── Build course cards HTML ───────────────────────────────────────────────
    cards_html = ""
    for c in course_data:
        name      = c["name"]
        safe_id   = name.replace(" ", "-").lower()
        any_slots = any(day["slots"] for day in c["days"])

        display_name    = name if any_slots else f"{name} (Fully Booked)"
        collapsed_state = "is-collapsed" if not any_slots else ""

        cards_html += f'''
        <div class="course-card {collapsed_state}" id="card-{safe_id}">
          <div class="card-header collapsible-header">
            <div class="header-title-group">
              <span class="collapse-icon">▼</span>
              <span class="course-name">{display_name}</span>
            </div>
          </div>
          <div class="card-body">'''

        if any_slots:
            for day in c["days"]:
                slots = day["slots"]
                cards_html += f'''
                <div class="day-row" data-day="{day['weekday']}">
                  <div class="day-row-header">
                    <span class="day-label">{day["label"]}</span>
                    <a class="book-btn" href="{day["book_url"]}" target="_blank">Book</a>
                  </div>'''

                if slots:
                    items_html = ""
                    for s in slots:
                        time_val = s.get("time", "?")
                        cls      = _slot_time_class(time_val, day["date_obj"], day["sunset_dt"])
                        if s.get("is_new"):
                            cls += " slot--new"
                        badge      = '<span class="new-badge">NEW</span>' if s.get("is_new") else ""
                        items_html += f'<li class="{cls}">{badge}{time_val}</li>'
                    cards_html += f'<ul class="slots">{items_html}</ul>'
                else:
                    cards_html += '<div class="no-slots">No times available</div>'

                cards_html += '</div>'  # .day-row
        else:
            cards_html += '<div class="day-row"><div class="no-slots">Fully booked for the weekend.</div></div>'

        cards_html += '</div></div>'  # .card-body  .course-card

    # ── Full HTML document ────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.15">
  <title>Tee Time Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    /* ── Design tokens ─────────────────────────────────────────────── */
    :root {{
      --brand-green:      #4a7c59;
      --gold:             #ffb703;
      --sunset-orange:    #fb8500;
      --text-main:        #1a1a1a;
      --text-sub:         #4b5563;
      --slot-text:        #111111;
      --surface:          #ffffff;
      --bg:               #f3f4f6;
      --border:           #e5e7eb;

      /* Early  = gold/yellow  (pre-10 AM)   */
      --early-bg:         #fef9c3;
      --early-brd:        #facc15;
      --early-text:       #111111;

      /* Midday = green        (10 AM–noon)  */
      --midday-bg:        #dcfce7;
      --midday-brd:       #4ade80;
      --midday-text:      #111111;

      /* Afternoon = blue      (noon+)       */
      --afternoon-bg:     #dbeafe;
      --afternoon-brd:    #60a5fa;
      --afternoon-text:   #111111;

      /* Twilight  = purple    (near sunset) */
      --twilight-bg:      #f3e8ff;
      --twilight-brd:     #c084fc;
      --twilight-text:    #111111;
    }}

    [data-theme="dark"] {{
      /* Backgrounds */
      --bg:               #141c25;
      --surface:          #1c2733;
      --border:           #2e3f50;

      /* Text */
      --text-main:        #dde6ef;
      --text-sub:         #7e96ad;
      --slot-text:        #dde6ef;

      /* Brand */
      --brand-green:      #5fa374;

      /* Early  = warm amber-sand wash */
      --early-bg:         #2a2410;
      --early-brd:        #c9a030;
      --early-text:       #e8d5a0;

      /* Midday = sage-forest wash     */
      --midday-bg:        #0f2318;
      --midday-brd:       #57a875;
      --midday-text:      #8fd4a8;

      /* Afternoon = slate-blue wash   */
      --afternoon-bg:     #152030;
      --afternoon-brd:    #5b91cc;
      --afternoon-text:   #a8caed;

      /* Twilight = dusty mauve wash   */
      --twilight-bg:      #1e1630;
      --twilight-brd:     #9b6ec8;
      --twilight-text:    #c4a0e8;
    }}

    /* ── Reset & base ──────────────────────────────────────────────── */
    * {{ box-sizing: border-box; }}
    body {{
      background: var(--bg);
      color: var(--text-main);
      font-family: 'Inter', sans-serif;
      margin: 0;
      padding-bottom: 50px;
      transition: background 0.3s, color 0.3s;
    }}

    /* ── Header ────────────────────────────────────────────────────── */
    header {{
      background: var(--brand-green);
      padding: 12px 15px;
      text-align: center;
      color: white;
      border-bottom: 3px solid var(--gold);
      transition: background 0.3s;
    }}
    [data-theme="dark"] header {{
      background: #0d1720;
      border-bottom: 3px solid var(--gold);
    }}
    h1 {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: 2.8rem;
      margin: 0;
      letter-spacing: 1.5px;
      line-height: 1;
      color: white;
      transition: color 0.3s, text-shadow 0.3s;
    }}
    [data-theme="dark"] h1 {{
      color: var(--brand-green);
      text-shadow: 0 0 20px rgba(95, 163, 116, 0.4);
    }}
    .sunset-box {{
      background: var(--sunset-orange);
      display: inline-flex;
      padding: 3px 10px;
      border-radius: 6px;
      font-weight: 800;
      font-size: 0.75rem;
      margin-top: 5px;
      align-items: center;
      gap: 5px;
      color: white;
    }}
    .header-status {{
      margin-top: 8px;
      font-size: 0.7rem;
      font-weight: 600;
      opacity: 0.9;
    }}
    .header-status b {{ color: var(--gold); }}

    /* ── Colour legend ──────────────────────────────────────────────── */
    .legend {{
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 7px 12px;
      display: flex;
      justify-content: center;
      gap: 14px;
      flex-wrap: wrap;
      transition: background 0.3s;
    }}
    .legend-item {{
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 0.65rem;
      font-weight: 700;
      color: var(--text-sub);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .legend-dot {{
      width: 12px;
      height: 12px;
      border-radius: 3px;
      border: 2px solid transparent;
      flex-shrink: 0;
    }}
    .legend-dot--early {{
      background: var(--early-bg);
      border-color: var(--early-brd);
    }}
    .legend-dot--midday {{
      background: var(--midday-bg);
      border-color: var(--midday-brd);
    }}
    .legend-dot--afternoon {{
      background: var(--afternoon-bg);
      border-color: var(--afternoon-brd);
    }}
    .legend-dot--twilight {{
      background: var(--twilight-bg);
      border-color: var(--twilight-brd);
    }}

    /* ── Filter bar ─────────────────────────────────────────────────── */
    .filter-bar {{
      position: sticky;
      top: 0;
      z-index: 50;
      background: var(--surface);
      padding: 10px;
      display: flex;
      justify-content: center;
      gap: 6px;
      border-bottom: 1px solid var(--border);
      transition: background 0.3s;
    }}
    .filter-btn {{
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text-sub);
      padding: 5px 12px;
      border-radius: 15px;
      font-size: 0.7rem;
      font-weight: 700;
      cursor: pointer;
      transition: background 0.2s, color 0.2s;
    }}
    .filter-btn.active {{
      background: var(--brand-green);
      color: white;
      border-color: var(--brand-green);
    }}

    /* ── Course grid ────────────────────────────────────────────────── */
    main {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
      gap: 12px;
      padding: 12px;
      max-width: 1400px;
      margin: 0 auto;
    }}

    /* ── Course card ────────────────────────────────────────────────── */
    .course-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      height: fit-content;
      transition: background 0.3s, border-color 0.3s;
    }}
    .card-header {{
      padding: 10px 14px;
      cursor: pointer;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .course-name {{
      font-weight: 800;
      font-size: 0.95rem;
      color: var(--brand-green);
    }}
    .collapse-icon {{
      font-size: 0.6rem;
      transition: transform 0.3s;
      color: var(--text-sub);
    }}
    .course-card.is-collapsed .card-body {{ display: none; }}
    .course-card.is-collapsed .collapse-icon {{ transform: rotate(-90deg); }}

    /* ── Day rows ───────────────────────────────────────────────────── */
    .day-row {{
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
    }}
    .day-row:last-child {{ border-bottom: none; }}
    .day-row-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}
    .day-label {{ font-weight: 700; font-size: 0.8rem; }}
    .book-btn {{
      color: var(--brand-green);
      font-size: 0.65rem;
      font-weight: 800;
      text-decoration: none;
      text-transform: uppercase;
      border: 1px solid var(--brand-green);
      padding: 2px 6px;
      border-radius: 4px;
    }}

    /* ── Slots ──────────────────────────────────────────────────────── */
    .slots {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
      gap: 5px;
      list-style: none;
      padding: 0;
      margin: 0;
    }}
    .slots li {{
      position: relative;        /* needed for .new-badge absolute child */
      padding: 3px 4px;
      text-align: center;
      border-radius: 5px;
      border: 2px solid transparent;
      font-size: 0.75rem;
      font-weight: 650;
    }}

    /* Light-mode slot colours */
    .slot--early     {{ background: var(--early-bg);     border-color: var(--early-brd);     color: var(--early-text);     }}
    .slot--midday    {{ background: var(--midday-bg);    border-color: var(--midday-brd);    color: var(--midday-text);    }}
    .slot--afternoon {{ background: var(--afternoon-bg); border-color: var(--afternoon-brd); color: var(--afternoon-text); }}
    .slot--twilight  {{ background: var(--twilight-bg);  border-color: var(--twilight-brd);  color: var(--twilight-text);  }}

    /* Dark-mode slot text overrides (backgrounds already set via vars) */
    [data-theme="dark"] .slot--early     {{ color: var(--early-text);     }}
    [data-theme="dark"] .slot--midday    {{ color: var(--midday-text);    }}
    [data-theme="dark"] .slot--afternoon {{ color: var(--afternoon-text); }}
    [data-theme="dark"] .slot--twilight  {{ color: var(--twilight-text);  }}

    /* New-slot highlight */
    .slot--new {{
      box-shadow: 0 0 0 2px var(--gold);
      animation: pulse-new 2s ease-in-out 15;
    }}
    .new-badge {{
      position: absolute;
      top: -8px;
      left: 50%;
      transform: translateX(-50%);
      font-size: 0.5rem;
      background: var(--gold);
      color: #000;
      padding: 1px 4px;
      border-radius: 2px;
      box-shadow: 0 2px 4px rgba(0,0,0,0.2);
    }}
    @keyframes pulse-new {{
      0%, 100% {{ box-shadow: 0 0 0 2px var(--gold); }}
      50%       {{ box-shadow: 0 0 0 5px rgba(255,183,3,0.4); }}
    }}

    .no-slots {{
      font-size: 0.7rem;
      color: var(--text-sub);
      font-style: italic;
      text-align: center;
      padding: 5px 0;
    }}

    /* ── Theme toggle button ────────────────────────────────────────── */
    #theme-toggle {{
      position: fixed;
      bottom: 15px;
      right: 15px;
      width: 40px;
      height: 40px;
      border-radius: 50%;
      background: var(--brand-green);
      color: var(--gold);
      border: none;
      cursor: pointer;
      z-index: 100;
      box-shadow: 0 4px 6px rgba(0,0,0,0.2);
      transition: background 0.3s;
    }}

    /* ── Responsive ─────────────────────────────────────────────────── */
    @media (max-width: 480px) {{
      main {{ grid-template-columns: 1fr; padding: 8px; }}
      h1   {{ font-size: 2.2rem; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>TEE TIME MONITOR</h1>
    <div class="sunset-box">☀️ SUNSET: {actual_sunset}</div>
    <div class="header-status">
      <div style="margin-bottom:4px"><b>Checked every 15 minutes</b></div>
      <div>Last updated: <span id="time-ago">just now</span> (<span id="last-ts">{now_str}</span>)</div>
    </div>
  </header>

  <!-- Colour legend -->
  <div class="legend">
    <div class="legend-item"><div class="legend-dot legend-dot--early"></div>Early (pre-10 AM)</div>
    <div class="legend-item"><div class="legend-dot legend-dot--midday"></div>Midday (10 AM–noon)</div>
    <div class="legend-item"><div class="legend-dot legend-dot--afternoon"></div>Afternoon (noon+)</div>
    <div class="legend-item"><div class="legend-dot legend-dot--twilight"></div>Twilight</div>
  </div>

  <!-- Day filter -->
  <div class="filter-bar">
    <button class="filter-btn active" data-day="Friday">Friday</button>
    <button class="filter-btn active" data-day="Saturday">Saturday</button>
    <button class="filter-btn active" data-day="Sunday">Sunday</button>
  </div>

  <main id="course-grid">
    {cards_html}
  </main>

  <button id="theme-toggle">🌙</button>

  <script>
    // ── "X mins ago" counter ─────────────────────────────────────────
    const updateTs = {now_ts};
    function updateTime() {{
      const diff = Math.floor(Date.now() / 1000) - updateTs;
      const mins = Math.floor(diff / 60);
      document.getElementById('time-ago').textContent =
        mins <= 0 ? 'just now' : mins + 'm ago';
    }}
    setInterval(updateTime, 30_000);
    updateTime();

    // ── Day filter ───────────────────────────────────────────────────
    document.querySelectorAll('.filter-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        btn.classList.toggle('active');
        const day = btn.dataset.day;
        document.querySelectorAll(`.day-row[data-day="${{day}}"]`).forEach(r =>
          r.style.display = btn.classList.contains('active') ? 'block' : 'none'
        );
      }});
    }});

    // ── Card collapse ────────────────────────────────────────────────
    document.querySelectorAll('.collapsible-header').forEach(h => {{
      h.addEventListener('click', () => {{
        h.closest('.course-card').classList.toggle('is-collapsed');
      }});
    }});

    // ── Theme toggle ─────────────────────────────────────────────────
    const themeBtn = document.getElementById('theme-toggle');
    themeBtn.addEventListener('click', () => {{
      const isDark = document.documentElement.dataset.theme === 'dark';
      document.documentElement.dataset.theme = isDark ? '' : 'dark';
      themeBtn.textContent = isDark ? '🌙' : '☀️';
    }});

    // ── Auto-reload when a new scrape runs ───────────────────────────
    setInterval(async () => {{
      try {{
        const r    = await fetch('version.json?_=' + Date.now());
        const data = await r.json();
        if (data.ts > updateTs) location.reload();
      }} catch(e) {{}}
    }}, 30_000);
  </script>
</body>
</html>"""

    Path("index.html").write_text(html)
    Path("version.json").write_text(json.dumps({"ts": now_ts}))
  
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
