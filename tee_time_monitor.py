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
        "skip_past_dates": True,
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
        "booking_window_days": 5,
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
        "booking_window_days": 5,
    },
    {
        "name":           "Miami Shores",
        "address":        "10000 Biscayne Blvd, Miami Shores",
        "phone":          "(305) 795-2369",
        "type":           "chronogolf",
        # Marketplace slug (miami-shores-cc) returns no tee times during the
        # management transition; inventory is exposed via the club booking widget API.
        "url":            "https://www.chronogolf.com/club/19871/widget?medium=widget&source=club",
        "chronogolf_club_id": 19871,
        "chronogolf_course_id": 28341,
        "chronogolf_affiliation_type_id": 153007,
        "holes":          18,
        "group_size":     4,
        "tee_time_min":   8,
        "tee_time_max":   14,
        "cache_file":     "cache_miami_shores.json",
        "skip_past_dates": True,
        "booking_window_days": 5,
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
        "skip_past_dates": True,
        "booking_window_days": 5,
    },
]

ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    """Current instant in America/New_York; tests may monkeypatch."""
    return datetime.now(ET)


DAY_NAMES = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday",  5: "Saturday", 6: "Sunday",
}
CACHE_DIR = Path(".")
MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)

# Weekday ints Mon=0 … Sun=6. Default Fri–Sun; add e.g. (3,) for Thursday scraping + UI.
DEFAULT_SCRAPE_WEEKDAYS: tuple[int, ...] = (4, 5, 6)
EXTRA_SCRAPE_WEEKDAYS: tuple[int, ...] = ()

@lru_cache(maxsize=32)
def get_sunset_cutoff(target_date: date, fallback_hour: int) -> datetime | int:
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        return s["sunset"] - timedelta(hours=4, minutes=10)
    except Exception as e:
        logger.error(f"Sunset calc failed for {target_date}: {e}")
        return fallback_hour

def get_monitor_dates() -> list[date]:
    """Dates in the next 7 days whose weekday is included in scrape config (ET)."""
    today = _now_et().date()
    wds = set(DEFAULT_SCRAPE_WEEKDAYS) | set(EXTRA_SCRAPE_WEEKDAYS)
    return [
        today + timedelta(days=i)
        for i in range(7)
        if (today + timedelta(days=i)).weekday() in wds
    ]


def days_ahead(target_date: date) -> int:
    return (target_date - _now_et().date()).days


def is_within_booking_window(course: dict, target_date: date) -> bool:
    bw = course.get("booking_window_days")
    if bw is None:
        return True
    return days_ahead(target_date) <= bw


def visible_dates_for_course(course: dict, monitor_dates: list[date]) -> list[date]:
    return [d for d in monitor_dates if is_within_booking_window(course, d)]

def is_within_window(time_str: str, t_min: int, t_max_dt: datetime | int) -> bool:
    try:
        parts = time_str.strip().split()
        hour, minute = int(parts[0].split(":")[0]), int(parts[0].split(":")[1])
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if isinstance(t_max_dt, datetime):
            slot_minutes = hour * 60 + minute
            cutoff_minutes = t_max_dt.hour * 60 + t_max_dt.minute
            return t_min * 60 <= slot_minutes <= cutoff_minutes
        return t_min <= hour <= int(t_max_dt)
    except Exception:
        return False

def is_slot_in_past(time_str: str, target_date: date) -> bool:
    """True if this slot's clock time is before the current *minute* (ET) on target_date.

    Same-minute tee times stay visible for the whole minute (scrapes only know HH:MM).
    """
    if target_date != _now_et().date():
        return False
    try:
        parts = time_str.strip().split()
        hour, minute = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        now_et = _now_et()
        # Compare at minute resolution so a tee time in the current clock minute still shows.
        now_minute = now_et.replace(second=0, microsecond=0)
        slot_dt = datetime.combine(target_date, datetime.min.time()).replace(
            hour=hour, minute=minute, second=0, microsecond=0, tzinfo=ET
        )
        return slot_dt < now_minute
    except Exception:
        return True


_HOLE_COUNT_RE = re.compile(r"(\d+)\s*holes?\b", re.I)


def slot_is_18_holes(slot: dict) -> bool:
    """Keep only 18-hole rounds; empty/missing holes counts as 18 (site default)."""
    raw = (slot.get("holes") or "").strip()
    if not raw:
        return True
    m = _HOLE_COUNT_RE.search(raw)
    if m:
        return int(m.group(1)) == 18
    return bool(re.search(r"\b18\b", raw))


def normalize_monitor_slots(slots: list[dict]) -> list[dict]:
    """18-hole foursome focus: drop non-18 offerings and strip price (not used downstream)."""
    out: list[dict] = []
    for s in slots:
        if not slot_is_18_holes(s):
            continue
        row = {k: v for k, v in s.items() if k != "price"}
        out.append(row)
    return out

def deduplicate_slots(slots: list[dict], t_min: int, t_max: int) -> list[dict]:
    seen, out = set(), []
    for slot in slots:
        if not is_within_window(slot.get("time", ""), t_min, t_max):
            continue
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

def _collapse(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()

def _normalize_time_label(time_str: str) -> str:
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
    holes, price = _CPS_HOLE_RE.search(raw), _PRICE_RE.search(raw)
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
            h, minute = int(m24.group(1)), m24.group(2)
            ampm = "PM" if h >= 12 else "AM"
            if h > 12:
                h -= 12
            if h == 0:
                h = 12
            time = f"{h}:{minute} {ampm}"
    if not time:
        return None
    hole, price = _CHRONO_HOLE_RE.search(raw), _PRICE_RE.search(raw)
    return {
        "time":  time,
        "holes": f"{hole.group(1)} holes" if hole else "",
        "price": price.group(0) if price else "",
    }

def _format_chrono_24h(h: int, minute: str) -> str:
    ampm = "PM" if h >= 12 else "AM"
    if h > 12:
        h -= 12
    if h == 0:
        h = 12
    return f"{h}:{minute} {ampm}"


def parse_chronogolf_club_api(entries: list, *, holes: int = 18) -> list[dict]:
    """Parse /marketplace/clubs/{id}/teetimes JSON (club widget API)."""
    out, seen = [], set()
    for entry in entries or []:
        if entry.get("out_of_capacity"):
            continue
        start = entry.get("start_time") or ""
        m = _CHRONO_24H_RE.search(start)
        if not m:
            continue
        time_str = _format_chrono_24h(int(m.group(1)), m.group(2))
        key = (time_str, f"{holes} holes")
        if key in seen:
            continue
        seen.add(key)
        price = ""
        fees = entry.get("green_fees") or []
        if fees and fees[0].get("price") is not None:
            price = f"${fees[0]['price']:.2f}"
        out.append({"time": time_str, "holes": f"{holes} holes", "price": price})
    return out


def fetch_chronogolf_club_teetimes(course: dict, target_date: date) -> list[dict]:
    """Fetch tee times from the Chronogolf club widget API (not the marketplace slug)."""
    club_id = course["chronogolf_club_id"]
    course_id = course["chronogolf_course_id"]
    affiliation_id = course["chronogolf_affiliation_type_id"]
    group_size = course.get("group_size", 4)
    holes = course.get("holes", 18)
    params = [
        ("date", target_date.isoformat()),
        ("course_id", str(course_id)),
        ("nb_holes", str(holes)),
    ]
    params.extend(("affiliation_type_ids[]", str(affiliation_id)) for _ in range(group_size))
    url = f"https://www.chronogolf.com/marketplace/clubs/{club_id}/teetimes"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://www.chronogolf.com/club/{club_id}/widget",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning(f"[{course['name']}] {target_date}: unexpected club teetimes response: {type(data)}")
            return []
        return parse_chronogolf_club_api(data, holes=holes)
    except Exception as e:
        logger.error(f"[{course['name']}] {target_date}: club teetimes API error: {e}")
        return []


def chronogolf_book_url(course: dict, d: date) -> str:
    if course.get("chronogolf_club_id"):
        gs = course.get("group_size", 4)
        aff = course["chronogolf_affiliation_type_id"]
        aff_ids = ",".join([str(aff)] * gs)
        return (
            f"https://www.chronogolf.com/club/{course['chronogolf_club_id']}/widget"
            f"?medium=widget&source=club#?date={d.isoformat()}"
            f"&course_id={course['chronogolf_course_id']}"
            f"&nb_holes={course.get('holes', 18)}"
            f"&affiliation_type_ids={aff_ids}"
        )
    return (
        f"{course['url']}?date={d.isoformat()}"
        f"&step=teetimes&holes={course.get('holes', 18)}"
        f"&coursesIds=&deals=false&groupSize={course.get('group_size', 4)}"
    )


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
    # Skip the body-text fallback if Chronogolf is explicitly telling us
    # there are no tee times — otherwise we'd pick up times from notice/news
    # boxes (e.g. "The driving range will open at 9:00AM").
    if "no tee times found" in (body_text or "").lower():
        return []
    for m in _CHRONO_12H_RE.finditer(_collapse(body_text)):
        time = f"{m.group(1)} {m.group(2).upper()}"
        key = (time, "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"time": time, "holes": "", "price": ""})
    return out

def parse_webtrac_row(cells: list[str]) -> dict | None:
    if len(cells) < 6:
        return None
    m = _LEADING_INT_RE.search(cells[5] or "")
    # Only show slots with exactly 4 open spaces (the column contains available player count)
    if (int(m.group(0)) if m else 0) != 4:
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

async def human_delay(page, min_ms: int = 800, max_ms: int = 2200):
    await page.wait_for_timeout(random.randint(min_ms, max_ms))

async def goto_with_retry(page, url: str, *, attempts: int = 3, **kwargs):
    last_exc = None
    for i in range(attempts):
        try:
            return await page.goto(url, **kwargs)
        except Exception as e:
            last_exc = e
            if i == attempts - 1:
                break
            await asyncio.sleep(2 ** i)
    raise last_exc

def send_pushover(title: str, message: str):
    if not all([PUSHOVER_USER, PUSHOVER_TOKEN]):
        return
    try:
        requests.post(
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
    except Exception as e:
        logger.error(f"Pushover error: {e}")

def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
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
    except Exception as e:
        logger.error(f"Email error: {e}")

def notify(subject: str, body: str, push_msg: str):
    send_pushover(subject, push_msg)

async def launch_browser(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
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
            if target_month_str in (header or "").strip():
                break
            if not header:
                logger.warning(f"[{course['name']}] {target_date}: no calendar month header detected; aborting.")
                return []
            advanced = await page.evaluate("() => { const topbar = document.querySelector('.topbar-container'); const btns = Array.from(topbar ? topbar.querySelectorAll('button') : []); const nextBtn = btns.find(b => !b.disabled && !b.classList.contains('topbar-title')); if (nextBtn) { nextBtn.click(); return true; } return false; }")
            if not advanced:
                logger.warning(f"[{course['name']}] {target_date}: next-month button not found (stuck on '{header}'); aborting.")
                return []
            await human_delay(page, 600, 1200)
        else:
            logger.warning(f"[{course['name']}] {target_date}: couldn't reach {target_month_str} after 12 advances; aborting.")
            return []
        day_num = str(target_date.day)
        clicked = await page.evaluate(f"() => {{ const target = '{day_num}'; for (const btn of document.querySelectorAll('button.btn-day-unit')) {{ if (btn.disabled) continue; const span = btn.parentElement?.querySelector('.day-background-upper'); const txt = (span?.innerText || '').trim(); if (txt !== target) continue; if ((span?.className || '').includes('prev-month')) continue; btn.click(); return true; }} return null; }}")
        if not clicked:
            return []
        await human_delay(page, 3000, 5000)
        card_texts, body_text = await page.evaluate("() => { const selectors = ['[class*=\"teetime\"]', '[class*=\"tee-time\"]', '[class*=\"timeslot\"]', '[class*=\"time-slot\"]', '[class*=\"booking\"]', '[class*=\"result-item\"]', '[class*=\"search-result\"]', '[class*=\"tee-card\"]']; let cards = []; for (const sel of selectors) { const found = document.querySelectorAll(sel); if (found.length > 0) { cards = Array.from(found); break; } } return [cards.map(c => c.innerText), document.body.innerText]; }")
        return parse_cpsgolf(card_texts, body_text)
    finally:
        await page.close()

async def scrape_chronogolf(context, course: dict, target_date: date) -> list[dict]:
    base_url = course["url"]
    date_str = target_date.isoformat()
    url = f"{base_url}?date={date_str}&step=teetimes&holes={course.get('holes', 18)}&coursesIds=&deals=false&groupSize={course.get('group_size', 4)}"
    page = await context.new_page()
    try:
        await goto_with_retry(page, url, wait_until="networkidle", timeout=60_000)
        await human_delay(page, 3000, 6000)
        # Chronogolf silently redirects both past dates and unreleased future dates.
        # If the landed URL no longer contains our requested date, bail out.
        if f"date={date_str}" not in page.url:
            logger.info(f"[{course['name']}] {target_date}: redirected to {page.url!r} — date not yet released, skipping.")
            return []
        card_texts, body_text = await page.evaluate("() => { const selectors = ['[class*=\"teetime-card\"]', '[class*=\"teetime\"]', '[class*=\"tee-time\"]', '[class*=\"green-fee\"]']; let cards = []; for (const sel of selectors) { const found = document.querySelectorAll(sel); if (found.length > 0) { cards = Array.from(found); break; } } return [cards.map(c => c.innerText), document.body.innerText]; }")
        return parse_chronogolf(card_texts, body_text)
    finally:
        await page.close()

async def scrape_webtrac(context, course: dict, target_date: date) -> list[dict]:
    page = await context.new_page()
    try:
        base_url = course["url"].split("?")[0]
        await goto_with_retry(page, base_url + "?module=GR&display=Detail", wait_until="networkidle", timeout=60_000)
        csrf = await page.evaluate("() => document.querySelector('#_csrf_token')?.value || ''")
        params = {
            "Action": "Start",
            "_csrf_token": csrf,
            "numberofplayers": "4",
            "begindate": target_date.strftime("%m/%d/%Y"),
            "begintime": "12:00 am",
            "numberofholes": "18",
            "display": "Detail",
            "module": "GR",
            "grwebsearch_buttonsearch": "yes",
        }
        await goto_with_retry(page, base_url + "?" + urlencode(params), wait_until="networkidle", timeout=60_000)
        rows = await page.evaluate("() => Array.from(document.querySelectorAll('#grwebsearch_output_table tbody tr')).map(row => Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()))")
        return parse_webtrac(rows)
    finally:
        await page.close()

def load_cache(cache_file: Path, d: date) -> list[dict] | None:
    try:
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
            if d.isoformat() in data:
                return data[d.isoformat()]
    except Exception as e:
        logger.error(f"Cache load error for {d}: {e}")
    return None

def save_cache(cache_file: Path, d: date, slots: list[dict]):
    all_cache = {}
    try:
        if cache_file.exists():
            content = cache_file.read_text().strip()
            if content:
                all_cache = json.loads(content)
    except Exception as e:
        logger.error(f"Cache read error for {d}: {e}")
    all_cache[d.isoformat()] = slots
    try:
        cache_file.write_text(json.dumps(all_cache, indent=2))
    except Exception as e:
        logger.error(f"Cache write error for {d}: {e}")

def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]

async def check_day(context, course: dict, target_date: date):
    """Check a single date. Returns (new_slots, detected_label_or_None).
    detected_label is set on the first run that finds slots, for consolidated nudge in main()."""
    name = course["name"]
    t_min, t_max = course["tee_time_min"], get_sunset_cutoff(target_date, course["tee_time_max"])
    cache_file = CACHE_DIR / course["cache_file"]

    if course.get("skip_past_dates") and target_date < datetime.now(ET).date():
        return [], None

    if course["type"] == "cpsgolf":
        raw = await scrape_cpsgolf(context, course, target_date)
    elif course["type"] == "chronogolf":
        if course.get("chronogolf_club_id"):
            raw = fetch_chronogolf_club_teetimes(course, target_date)
        else:
            raw = await scrape_chronogolf(context, course, target_date)
    elif course["type"] == "webtrac":
        raw = await scrape_webtrac(context, course, target_date)
    else:
        return [], None

    raw = normalize_monitor_slots(raw)

    current_slots = [
        s for s in deduplicate_slots(raw, t_min, t_max)
        if not is_slot_in_past(s.get("time", ""), target_date)
    ]

    cached_slots = load_cache(cache_file, target_date)
    is_first_run = cached_slots is None
    new_slots = [] if is_first_run else find_new_slots(cached_slots, current_slots)

    new_slot_times = {s.get("time", "").strip().upper() for s in new_slots}
    for s in current_slots:
        s["is_new"] = s.get("time", "").strip().upper() in new_slot_times
    save_cache(cache_file, target_date, current_slots)

    if not current_slots:
        logging.info(f"[{name}] {target_date}: No slots available at all.")
        return [], None

    if is_first_run:
        date_label = target_date.strftime("%a %-d")
        logging.info(f"[{name}] {target_date}: First run – will include in detection nudge.")
        return new_slots, f"{name} – {date_label}"
    elif new_slots:
        logging.info(f"✨ NEW SLOT DETECTED: {name} on {target_date} ({len(new_slots)} new times)!")
    else:
        logging.info(f"[{name}] {target_date}: No new slots found (matches cache).")

    return new_slots, None

async def check_course(playwright, course: dict, dates: list[date]) -> list[str]:
    """Manage browser for course and group notifications by course.
    Returns list of detected-date labels for main() to consolidate into one nudge."""
    browser, context = await launch_browser(playwright)
    course_new_slots = {}  # date_label -> list of slots
    detected_labels = []

    monitored = set(DEFAULT_SCRAPE_WEEKDAYS) | set(EXTRA_SCRAPE_WEEKDAYS)
    try:
        for d in dates:
            if not is_within_booking_window(course, d):
                continue
            try:
                new_slots, detected = await check_day(context, course, d)
                # Pushover for any weekday we actively monitor (default Fri–Sun; add extras in EXTRA_SCRAPE_WEEKDAYS).
                notify_day = d.weekday() in monitored
                if detected and notify_day:
                    detected_labels.append(detected)
                if new_slots and notify_day:
                    course_new_slots[d.strftime("%a %-d")] = new_slots
            except Exception as e:
                logger.exception(f"Error checking {course['name']} on {d}: {e}")
            await asyncio.sleep(random.uniform(1.5, 3.5))

        if course_new_slots:
            name = course["name"]
            subject = f"Tee Time Alert - {name}"
            lines = []
            for date_label, slots in course_new_slots.items():
                times_str = ", ".join(s.get("time", "?") for s in slots)
                lines.append(f"{date_label} - {times_str}")
            if course["type"] != "chronogolf":
                lines.append(f"\n{course['url']}")
            push_msg = "\n".join(lines)
            email_body = f"New tee times opened at {name}:\n\n" + push_msg
            notify(subject, email_body, push_msg)

    finally:
        await browser.close()

    return detected_labels

APP_COURSE_IDS = {
    "Miami Beach":         "miami-beach",
    "Normandy Shores":     "normandy-shores",
    "Miami Shores":        "miami-shores",
    "Miami Lakes":         "miami-lakes",
    "Plantation Preserve": "plantation-preserve",
}

def _slot_to_app(slot: dict) -> dict:
    out = {"time": slot.get("time", "")}
    if slot.get("holes"):
        try:
            out["holes"] = int(str(slot["holes"]).split()[0])
        except Exception:
            pass
    if slot.get("is_new"):
        out["isNew"] = True
    return out

def generate_data_json():
    """Emit a single data.json the Miami Tee Times app reads."""
    dates_all = get_monitor_dates()
    courses_out = []
    for course in COURSES:
        app_id = APP_COURSE_IDS.get(course["name"])
        if not app_id:
            continue
        cache_file = CACHE_DIR / course["cache_file"]
        days_out = []
        for d in visible_dates_for_course(course, dates_all):
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            raw_slots = normalize_monitor_slots(load_cache(cache_file, d) or [])
            slots = [
                _slot_to_app(s)
                for s in deduplicate_slots(raw_slots, course["tee_time_min"], t_max_day)
                if not is_slot_in_past(s.get("time", ""), d)
            ]
            days_out.append({"date": d.isoformat(), "slots": slots})
        courses_out.append({"id": app_id, "days": days_out})

    payload = {
        "version": 1,
        "generatedAt": datetime.now(ET).isoformat(),
        "courses": courses_out,
    }
    Path("data.json").write_text(json.dumps(payload, indent=2))
    logger.info("data.json generated.")

def _slot_time_class(time_str: str, target_date: date, sunset_dt: datetime) -> str:
    try:
        t_str = time_str.strip().upper()
        is_pm = t_str.endswith("PM")
        time_parts = t_str.replace("AM", "").replace("PM", "").strip().split(":")
        h, m = int(time_parts[0]), int(time_parts[1]) if len(time_parts) > 1 else 0
        h_24 = h + 12 if is_pm and h != 12 else 0 if not is_pm and h == 12 else h
        slot_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=h_24, minute=m, tzinfo=ET)
        if (sunset_dt - slot_dt).total_seconds() <= 16_200:
            return "slot--twilight"
        if h_24 < 10:
            return "slot--early"
        if h_24 < 12:
            return "slot--midday"
        return "slot--afternoon"
    except Exception:
        return ""

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
    .sunset-box { background: var(--sunset-orange); display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 6px; font-weight: 800; font-size: 0.75rem; margin-top: 5px; color: white; }
    .header-status { margin-top: 8px; font-size: 0.7rem; font-weight: 600; opacity: 0.9; }
    .header-status b { color: var(--gold); }
    .legend { background: var(--surface); border-bottom: 1px solid var(--border); padding: 7px 12px; display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; }
    .legend-item { appearance: none; background: transparent; border: 1px solid transparent; border-radius: 14px; cursor: pointer; display: flex; align-items: center; gap: 5px; padding: 2px 6px; font-family: inherit; font-size: 0.65rem; font-weight: 700; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.04em; transition: border-color 0.2s, opacity 0.2s, background 0.2s; }
    .legend-item:hover { border-color: var(--border); }
    .legend-item:focus-visible { outline: 2px solid var(--brand-green); outline-offset: 2px; }
    .legend-item:not(.active) { opacity: 0.42; }
    .legend-item.active { background: var(--bg); border-color: var(--border); }
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
    .slot--hidden { display: none; }
    .slot--new { box-shadow: 0 0 0 2px var(--gold); animation: pulse-new 2s ease-in-out 15; }
    .new-badge { position: absolute; top: -8px; left: 50%; transform: translateX(-50%); font-size: 0.5rem; background: var(--gold); color: #000; padding: 1px 4px; border-radius: 2px; }
    @keyframes pulse-new { 0%, 100% { box-shadow: 0 0 0 2px var(--gold); } 50% { box-shadow: 0 0 0 5px rgba(255,183,3,0.4); } }
    .no-slots { font-size: 0.7rem; color: var(--text-sub); font-style: italic; text-align: center; padding: 5px 0; }
    .empty-state { text-align: center; padding: 40px 20px; color: var(--text-sub); font-size: 0.9rem; font-weight: 600; max-width: 600px; margin: 0 auto 40px auto; }
    #theme-toggle { position: fixed; bottom: 15px; right: 15px; width: 44px; height: 44px; border-radius: 50%; background: var(--brand-green); color: var(--gold); border: none; cursor: pointer; z-index: 100; box-shadow: 0 4px 6px rgba(0,0,0,0.2); transition: background 0.3s; }
    @media (max-width: 480px) { html { font-size: 115%; } main { grid-template-columns: 1fr; padding: 8px; } h1 { font-size: 2.2rem; } .slots { grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); } }
  </style>
</head>
<body>
  <header>
    <h1>TEE TIME MONITOR</h1>
    <div class="sunset-box">☀️ SUNSET: {{ actual_sunset }}</div>
    <div class="header-status">
      <div style="margin-bottom:4px"><b>Automatically checked every 10–15 minutes</b></div>
      <div>Last updated: <span id="time-ago">just now</span> (<span id="last-ts">{{ now_str }}</span>)</div>
    </div>
  </header>
  <div class="legend" id="time-filter-bar" aria-label="Time filters">
    <button type="button" class="legend-item active" data-time-filter="slot--early" aria-pressed="true"><span class="legend-dot legend-dot--early"></span>Early (pre-10 AM)</button>
    <button type="button" class="legend-item active" data-time-filter="slot--midday" aria-pressed="true"><span class="legend-dot legend-dot--midday"></span>Midday (10 AM–noon)</button>
    <button type="button" class="legend-item active" data-time-filter="slot--afternoon" aria-pressed="true"><span class="legend-dot legend-dot--afternoon"></span>Afternoon (noon+)</button>
    <button type="button" class="legend-item active" data-time-filter="slot--twilight" aria-pressed="true"><span class="legend-dot legend-dot--twilight"></span>Twilight</button>
  </div>
  {% if filter_days|length > 1 %}
  <div class="filter-bar" id="day-filter-bar">
    {% for day in filter_days %}
    <button type="button" class="filter-btn active" data-day="{{ day }}">{{ day }}</button>
    {% endfor %}
  </div>
  {% endif %}
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
            {% elif day.not_released %}
              <div class="no-slots">Not yet released</div>
            {% else %}
              <div class="no-slots">No times available</div>
            {% endif %}
          </div>
          {% endfor %}
        {% elif c.no_visible_days|default(false) %}
          <div class="day-row"><div class="no-slots">No dates in the current booking window.</div></div>
        {% else %}
          <div class="day-row"><div class="no-slots">{{ 'Not yet released.' if c.all_not_released else 'Fully booked for the dates shown.' }}</div></div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </main>
  <div id="empty-state-msg" style="display:none" class="empty-state">Select at least one day and time range to see available times.</div>
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
    (function filters() {
      const dayBar = document.getElementById('day-filter-bar');
      const timeBar = document.getElementById('time-filter-bar');
      function saveToHash() {
        const offDays = [...(dayBar?.querySelectorAll('.filter-btn') || [])].filter(b => !b.classList.contains('active')).map(b => b.dataset.day).join(',');
        const offTimes = [...timeBar.querySelectorAll('.legend-item')].filter(b => !b.classList.contains('active')).map(b => b.dataset.timeFilter).join(',');
        const hash = [offDays ? 'd=' + offDays : '', offTimes ? 't=' + offTimes : ''].filter(Boolean).join('&');
        history.replaceState(null, '', hash ? '#' + hash : location.pathname + location.search);
      }
      function loadFromHash() {
        const params = new URLSearchParams(location.hash.slice(1));
        const offDays = new Set((params.get('d') || '').split(',').filter(Boolean));
        const offTimes = new Set((params.get('t') || '').split(',').filter(Boolean));
        dayBar?.querySelectorAll('.filter-btn').forEach(b => {
          const active = !offDays.has(b.dataset.day);
          b.classList.toggle('active', active);
        });
        timeBar.querySelectorAll('.legend-item').forEach(b => {
          const active = !offTimes.has(b.dataset.timeFilter);
          b.classList.toggle('active', active);
          b.setAttribute('aria-pressed', String(active));
        });
      }
      function activeDays() {
        if (!dayBar) return null;
        return new Set([...dayBar.querySelectorAll('.filter-btn.active')].map(b => b.dataset.day));
      }
      function activeTimes() {
        return new Set([...timeBar.querySelectorAll('.legend-item.active')].map(b => b.dataset.timeFilter));
      }
      function applyFilters() {
        const days = activeDays();
        const times = activeTimes();
        document.querySelectorAll('.slots li').forEach(slot => {
          const show = [...times].some(cls => slot.classList.contains(cls));
          slot.classList.toggle('slot--hidden', !show);
        });
        document.querySelectorAll('.day-row[data-day]').forEach(row => {
          const dayMatches = !days || days.has(row.dataset.day);
          const hasVisibleSlot = !!row.querySelector('.slots li:not(.slot--hidden)');
          const hasNoSlotMessage = !!row.querySelector('.no-slots');
          row.style.display = dayMatches && times.size > 0 && (hasVisibleSlot || hasNoSlotMessage) ? 'block' : 'none';
        });
        const anyVisible = [...document.querySelectorAll('.day-row[data-day]')].some(row => row.style.display !== 'none');
        const emptyEl = document.getElementById('empty-state-msg');
        if (emptyEl) emptyEl.style.display = anyVisible ? 'none' : 'block';
      }
      loadFromHash();
      applyFilters();
      if (dayBar) {
        dayBar.querySelectorAll('.filter-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            btn.classList.toggle('active');
            saveToHash();
            applyFilters();
          });
        });
      }
      timeBar.querySelectorAll('.legend-item').forEach(btn => {
        btn.addEventListener('click', () => {
          btn.classList.toggle('active');
          btn.setAttribute('aria-pressed', String(btn.classList.contains('active')));
          saveToHash();
          applyFilters();
        });
      });
    })();
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
    dates_all = get_monitor_dates()
    now_dt = datetime.now(ET)
    now_str, now_ts = now_dt.strftime("%-I:%M %p ET, %a %b %-d"), int(now_dt.timestamp())

    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    filter_weekday_names: set[str] = set()
    for course in COURSES:
        for d in visible_dates_for_course(course, dates_all):
            filter_weekday_names.add(d.strftime("%A"))
    filter_days = sorted(filter_weekday_names, key=lambda n: weekday_order.index(n) if n in weekday_order else 99)

    course_data = []
    for course in COURSES:
        dates_vis = visible_dates_for_course(course, dates_all)
        if not dates_vis:
            course_data.append({
                "name":             course["name"],
                "safe_id":          course["name"].replace(" ", "-").lower(),
                "display_name":     f"{course['name']} (no days in booking window)",
                "any_slots":        False,
                "all_not_released": False,
                "days":             [],
                "no_visible_days":  True,
            })
            continue

        days_for_course = []
        total_slots_count = 0
        for d in dates_vis:
            cache_file = CACHE_DIR / course["cache_file"]
            sunset_dt = sun(MIAMI.observer, date=d, tzinfo=ET)["sunset"]
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            cached = load_cache(cache_file, d)
            not_released = cached is None
            raw_slots = normalize_monitor_slots(cached or [])
            slots = [
                s for s in deduplicate_slots(raw_slots, course["tee_time_min"], t_max_day)
                if not is_slot_in_past(s.get("time", ""), d)
            ]
            total_slots_count += len(slots)

            if course["type"] == "chronogolf":
                book_url = chronogolf_book_url(course, d)
            elif course["type"] == "cpsgolf":
                t_max_book = get_sunset_cutoff(d, course["tee_time_max"])
                book_url = f"{course['url']}?TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={t_max_book}"
            else:
                book_url = course["url"]

            days_for_course.append({
                "date_obj":     d,
                "sunset_dt":    sunset_dt,
                "label":        d.strftime("%a %-d"),
                "weekday":      d.strftime("%A"),
                "not_released": not_released,
                "slots": [
                    {
                        "time":   s.get("time", "?"),
                        "is_new": s.get("is_new", False),
                        "cls":    _slot_time_class(s.get("time", "?"), d, sunset_dt),
                    }
                    for s in slots
                ],
                "book_url": book_url,
            })

        any_slots = total_slots_count > 0
        all_not_released = (
            not any_slots
            and bool(days_for_course)
            and all(day["not_released"] for day in days_for_course)
        )
        if any_slots:
            display_name = f"{course['name']} ({total_slots_count} slots)"
        elif all_not_released:
            display_name = f"{course['name']} (Not Yet Released)"
        else:
            display_name = f"{course['name']} (Fully Booked)"
        course_data.append({
            "name":             course["name"],
            "safe_id":          course["name"].replace(" ", "-").lower(),
            "display_name":     display_name,
            "any_slots":        any_slots,
            "all_not_released": all_not_released,
            "days":             days_for_course,
            "no_visible_days":  False,
        })

    if dates_all:
        actual_sunset = sun(MIAMI.observer, date=dates_all[0], tzinfo=ET)["sunset"].strftime("%-I:%M %p")
    else:
        actual_sunset = sun(MIAMI.observer, date=datetime.now(ET).date(), tzinfo=ET)["sunset"].strftime("%-I:%M %p")

    template = Template(HTML_TEMPLATE)
    html_out = template.render(
        courses=course_data,
        filter_days=filter_days,
        actual_sunset=actual_sunset,
        now_str=now_str,
        now_ts=now_ts,
    )

    Path("index.html").write_text(html_out)
    Path("version.json").write_text(json.dumps({"ts": now_ts}))

def _select_courses(filter_terms: list[str]) -> list[dict]:
    if not filter_terms:
        return COURSES
    terms = [t.lower() for t in filter_terms]
    picked = [c for c in COURSES if any(t in c["name"].lower() for t in terms)]
    if not picked:
        sys.exit(f"No course matched {filter_terms!r}.")
    return picked

async def main(courses: list[dict]):
    dates = get_monitor_dates()
    async with async_playwright() as playwright:
        results = await asyncio.gather(
            *[check_course(playwright, course, dates) for course in courses],
            return_exceptions=True,
        )
    all_detected = [label for r in results if isinstance(r, list) for label in r]
    if all_detected:
        send_pushover("Tee Time Monitor – Now Tracking", "\n".join(all_detected))
    if len(courses) == len(COURSES):
        generate_html()
        generate_data_json()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", "-c", action="append", default=[])
    args = parser.parse_args()
    asyncio.run(main(_select_courses(args.course)))
