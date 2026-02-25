"""
Tee Time Monitor -- Miami Lakes, Normandy & Miami Shores
Checks multiple golf courses and sends email + Pushover push notifications
when new tee times appear.

Setup:
  pip install playwright requests
  playwright install chromium

To add a new course, just add an entry to the COURSES list below.
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
EMAIL_TO       = os.environ.get("EMAIL_TO")   # comma-separated for multiple

PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

# ── Course configuration ───────────────────────────────────────────────────────
#
# type "cpsgolf"    -- calendar-based site (Miami Lakes)
# type "chronogolf" -- date-in-URL site (Normandy Shores, and any other
#                      Chronogolf course -- just change the slug in the url)
#
# TEE_TIME_MIN / TEE_TIME_MAX: 24h hours. 0=midnight, 7=7AM, 18=6PM
# skip_past_dates: True for Chronogolf (it silently redirects past dates to today)

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

DAY_NAMES = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday",  5: "Saturday", 6: "Sunday"
}

CACHE_DIR = Path(".")   # cache files live in the repo root alongside the script

# ── Sunset helper ──────────────────────────────────────────────────────────────

MIAMI = LocationInfo("Miami", "USA", "America/New_York", 25.7617, -80.1918)

def get_sunset_cutoff(target_date: date, fallback_hour: int) -> int:
    """
    Returns the latest hour (24h int) to start a round, defined as
    5 hours before sunset in Miami. Falls back to fallback_hour if astral fails.
    """
    try:
        s = sun(MIAMI.observer, date=target_date, tzinfo=ET)
        sunset_hour   = s["sunset"].hour
        sunset_minute = s["sunset"].minute
        cutoff_hour   = sunset_hour - 4
        #if sunset_minute < 30:
            #cutoff_hour -= 1
        #cutoff_hour = max(cutoff_hour, 6)
        print(f"  Sunset: {s['sunset'].strftime('%-I:%M %p ET')} → cutoff: {cutoff_hour:02d}:00")
        return cutoff_hour
    except Exception as e:
        print(f"  Sunset calc failed ({e}) -- using fallback {fallback_hour:02d}:00")
        return fallback_hour

# ── Date helpers ───────────────────────────────────────────────────────────────

def get_upcoming_weekend_dates() -> list[date]:
    """
    Return all Fri/Sat/Sun from today through the next 9 days (Eastern Time).
    Covers remaining days of this weekend + the full next weekend.
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
        return True


def deduplicate_slots(slots: list[dict], t_min: int, t_max: int) -> list[dict]:
    seen = set()
    out  = []
    for slot in slots:
        t = slot.get("time", "").strip().upper()
        if not is_within_window(slot.get("time", ""), t_min, t_max):
            continue
        key = (t,)
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out

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
    send_email(subject, body)
    send_pushover(subject, push_msg)

# ── Browser launch helper (shared) ────────────────────────────────────────────

async def launch_browser(playwright):
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

async def scrape_cpsgolf(course: dict, target_date: date) -> list[dict]:
    base_url  = course["url"]
    t_min     = course["tee_time_min"]
    t_max     = course["tee_time_max"]

    async with async_playwright() as p:
        browser, context = await launch_browser(p)
        page = await context.new_page()
        url  = f"{base_url}?TeeOffTimeMin={t_min}&TeeOffTimeMax={t_max}"

        print(f"  Loading page...")
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

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
            await page.wait_for_timeout(800)

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
            await browser.close()
            return []

        print(f"  {clicked}")
        await page.wait_for_timeout(4_000)

        tee_times = await page.evaluate("""
            () => {
                const results  = [];
                const seenKeys = new Set();
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
                if (cards.length > 0) {
                    for (const card of cards) {
                        const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!raw) continue;
                        const timeMatch = raw.match(/(\\d{1,2}:\\d{2})\\s*P\\s*M|(\\d{1,2}:\\d{2})\\s*A\\s*M/i);
                        const holeMatch = raw.match(/\\d+\\s*HOLE/i);
                        const priceMatch = raw.match(/\\$[\\d.]+/);
                        if (timeMatch) {
                            const timeBase = timeMatch[1] || timeMatch[2];
                            const ampm     = timeMatch[1] ? 'PM' : 'AM';
                            const time     = timeBase + ' ' + ampm;
                            const holes    = holeMatch ? holeMatch[0] : '';
                            const key      = time + '|' + holes;
                            if (!seenKeys.has(key)) {
                                seenKeys.add(key);
                                results.push({ time, holes, price: priceMatch ? priceMatch[0] : '' });
                            }
                        }
                    }
                }
                if (results.length === 0) {
                    const fullText = document.body.innerText.replace(/\\s+/g, ' ');
                    const pattern  = /(\\d{1,2}:\\d{2})\\s*P\\s*M|(\\d{1,2}:\\d{2})\\s*A\\s*M/gi;
                    let match;
                    while ((match = pattern.exec(fullText)) !== null) {
                        const base = match[1] || match[2];
                        const ampm = match[1] ? 'PM' : 'AM';
                        const time = base + ' ' + ampm;
                        const key  = time + '|';
                        if (!seenKeys.has(key)) {
                            seenKeys.add(key);
                            results.push({ time, holes: '', price: '' });
                        }
                    }
                }
                return results;
            }
        """)

        await browser.close()

    print(f"  Raw slots found: {len(tee_times)}")
    return tee_times

# ── Scraper: Chronogolf (date in URL) ─────────────────────────────────────────

async def scrape_chronogolf(course: dict, target_date: date) -> list[dict]:
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

    async with async_playwright() as p:
        browser, context = await launch_browser(p)
        page = await context.new_page()

        print(f"  Loading page...")
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(5_000)

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

        tee_times = await page.evaluate("""
            () => {
                const results  = [];
                const seenKeys = new Set();
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
                if (cards.length > 0) {
                    for (const card of cards) {
                        const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!raw || raw.length < 3) continue;
                        const timeMatch12 = raw.match(/(\\d{1,2}:\\d{2})\\s*(AM|PM)/i);
                        const timeMatch24 = raw.match(/\\b([01]?\\d|2[0-3]):(\\d{2})\\b/);
                        const holeMatch   = raw.match(/(\\d+)\\s*hole/i);
                        const priceMatch  = raw.match(/\\$[\\d,.]+/);
                        let time = '';
                        if (timeMatch12) {
                            time = timeMatch12[1] + ' ' + timeMatch12[2].toUpperCase();
                        } else if (timeMatch24) {
                            let h = parseInt(timeMatch24[1]);
                            const m = timeMatch24[2];
                            const ampm = h >= 12 ? 'PM' : 'AM';
                            if (h > 12) h -= 12;
                            if (h === 0) h = 12;
                            time = h + ':' + m + ' ' + ampm;
                        }
                        if (time) {
                            const holes = holeMatch ? holeMatch[1] + ' holes' : '';
                            const key   = time + '|' + holes;
                            if (!seenKeys.has(key)) {
                                seenKeys.add(key);
                                results.push({ time, holes, price: priceMatch ? priceMatch[0] : '' });
                            }
                        }
                    }
                }
                if (results.length === 0) {
                    const fullText = document.body.innerText.replace(/\\s+/g, ' ');
                    const pat12 = /(\\d{1,2}:\\d{2})\\s*(AM|PM)/gi;
                    let m;
                    while ((m = pat12.exec(fullText)) !== null) {
                        const time = m[1] + ' ' + m[2].toUpperCase();
                        const key  = time + '|';
                        if (!seenKeys.has(key)) {
                            seenKeys.add(key);
                            results.push({ time, holes: '', price: '' });
                        }
                    }
                }
                return results;
            }
        """)

        if not tee_times:
            snippet = await page.evaluate("() => document.body.innerText.slice(0, 200)")
            print(f"  DEBUG page text:\n{snippet}\n")

        await browser.close()

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

async def check_day(course: dict, target_date: date):
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
  
    # Scrape based on site type
    if course["type"] == "cpsgolf":
        raw = await scrape_cpsgolf(course, target_date)
    elif course["type"] == "chronogolf":
        raw = await scrape_chronogolf(course, target_date)
    else:
        print(f"  Unknown site type: {course['type']}")
        return

    current_slots = deduplicate_slots(raw, t_min, t_max)
    print(f"  Found {len(current_slots)} unique slot(s) after dedup.")

    cached_slots = load_cache(cache_file, target_date)
    new_slots    = find_new_slots(cached_slots, current_slots)

    save_cache(cache_file, target_date, current_slots)

    if not current_slots:
        print("  No slots found -- skipping.\n")
        return

    if new_slots:
        print(f"  {len(new_slots)} NEW slot(s)!")

        date_label = f"{day_name}, {target_date.strftime('%B %-d, %Y')}"
        subject    = f"Tee Time Alert - {name} {target_date.strftime('%a %b %-d')}"

        # Build book URL based on type
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

# ── HTML generator ────────────────────────────────────────────────────────────

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
            slots = load_cache(cache_file, d)
            # Build booking URL
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

    # Build course cards HTML
    cards_html = ""
    for cd in course_data:
        course = cd["course"]
        days_html = ""
        for day in cd["days"]:
            if day["slots"]:
                times_html = "".join(
                    f'<span class="slot">{s.get("time","?")}'
                    f'{(" · " + s["price"]) if s.get("price") else ""}</span>'
                    for s in day["slots"]
                )
                day_body = f'<div class="slots">{times_html}</div>'
            else:
                day_body = '<p class="no-times">No times available</p>'

            days_html += f"""
            <div class="day-block">
              <div class="day-header">
                <span class="day-name">{day["label"]}</span>
                <a class="book-btn" href="{day["book_url"]}" target="_blank">Book →</a>
              </div>
              {day_body}
            </div>"""

        # Compute a representative cutoff for the card header (use first date)
        repr_cutoff = get_sunset_cutoff(cd["days"][0]["date"], course["tee_time_max"]) if cd["days"] else course["tee_time_max"]
        cutoff_ampm = f"{repr_cutoff % 12 or 12}:00 {'AM' if repr_cutoff < 12 else 'PM'}"

        cards_html += f"""
        <div class="course-card">
          <div class="card-header">
            <div class="course-name">{course["name"]}</div>
            <div class="course-meta">{course.get("address","")}</div>
            <div class="course-meta">{""}</div>
          </div>
          {days_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>Tee Time Watch</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,400&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

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
      --text-mid:     #3a5c42;
      --text-light:   #7a9485;
    }}

    body {{
      font-family: 'DM Sans', sans-serif;
      background: var(--cream);
      color: var(--text-dark);
      min-height: 100vh;
      overflow-x: hidden;
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
    }}
    .ticker-inner span {{ margin: 0 48px; }}
    @keyframes ticker {{
      0%   {{ transform: translateX(0); }}
      100% {{ transform: translateX(-50%); }}
    }}

    /* ── Header ── */
    header {{
      background: var(--green-deep);
      padding: 48px 30px;
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
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 18px;
      position: relative;
    }}
    .header-flag, .header-golfer {{
      font-size: 6rem;
      flex-shrink: 0;
      animation: flagwave 3s ease-in-out infinite;
      transform-origin: bottom center;
    }}
    .header-golfer {{ animation-direction: reverse; }}
    @keyframes flagwave {{
      0%, 100% {{ transform: rotate(-3deg); }}
      50%       {{ transform: rotate(3deg); }}
    }}
    h1 {{
      font-family: 'Bebas Neue', sans-serif;
      font-size: clamp(3.5rem, 10vw, 6rem);
      color: var(--white);
      letter-spacing: 0.08em;
      line-height: 0.9;
      position: relative;
    }}
    h1 em {{
      color: var(--gold);
      font-style: normal;
    }}
    .subtitle {{
      font-size: 0.8rem;
      color: rgba(255,255,255,0.5);
      margin-top: 12px;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      position: relative;
    }}

    /* ── Updated bar ── */
    .updated-bar {{
      background: var(--green-mid);
      text-align: center;
      padding: 10px 24px;
      font-size: 0.78rem;
      color: rgba(255,255,255,0.65);
      letter-spacing: 0.04em;
      border-bottom: 3px solid var(--gold);
    }}
    .updated-bar strong {{ color: var(--white); font-weight: 500; }}

    /* ── Main grid ── */
    main {{
      max-width: 1100px;
      margin: 32px auto 0;
      padding: 0 20px 20px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 24px;
      align-items: start;
    }}

    /* ── Course card ── */
    .course-card {{
      background: var(--white);
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
      padding: 20px 20px 0;
      position: relative;
      overflow: hidden;
    }}
    /* ── Course card header ── */
    .card-header {{
      background: linear-gradient(135deg, var(--green-deep) 0%, var(--green-mid) 100%);
      padding: 20px 24px 0;
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
      color: var(--white);
      letter-spacing: 0.06em;
      line-height: 1;
    }}
    .course-meta {{
      font-size: 0.7rem;
      color: rgba(255,255,255,0.55);
      letter-spacing: 0.04em;
      margin-top: 4px;
    }}
    .window-tag {{
      color: var(--gold);
      font-size: 0.7rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      padding: 6px 0 8px;
      display: block;
    }}

    /* ── Day block ── */
    .day-block {{
      padding: 14px 20px;
      border-bottom: 1px solid #edf5f0;
      background: var(--white);
    }}
    .day-block:last-child {{ border-bottom: none; }}
    .day-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 15px;
    }}
    .day-name {{
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--text-mid);
    }}
    .book-btn {{
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--green-deep);
      background: var(--gold);
      text-decoration: none;
      padding: 5px 12px;
      border-radius: 20px;
      transition: all 0.15s;
      letter-spacing: 0.04em;
    }}
    .book-btn:hover {{ background: var(--gold-dark); }}

    /* ── Slots ── */
    .slots {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .slot {{
      background: var(--green-pale);
      color: var(--green-deep);
      font-size: 0.82rem;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 6px;
      font-variant-numeric: tabular-nums;
      border: 1px solid rgba(46,139,79,0.2);
      min-width: 82px;
      text-align: center;
    }}
    .no-times {{
      font-size: 0.82rem;
      color: var(--text-light);
      font-style: italic;
    }}

    /* ── Callout strip ── */
    .callout-strip {{
      background: var(--green-deep);
      padding: 28px 24px;
      margin: 32px 20px 0;
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
      font-size: clamp(1.4rem, 4vw, 2.2rem);
      color: var(--gold);
      letter-spacing: 0.1em;
      text-align: center;
      flex: 1;
    }}
    .callout-quote span {{
      color: rgba(255,255,255,0.4);
      font-size: 0.6em;
      display: block;
      letter-spacing: 0.2em;
      font-family: 'DM Sans', sans-serif;
      font-weight: 300;
      margin-top: 4px;
    }}
    .callout-divider {{
      width: 1px;
      height: 40px;
      background: rgba(255,255,255,0.15);
      flex-shrink: 0;
    }}
    
/* ── Check Now buttons ── */
    .check-now-wrap {{
      text-align: center;
      padding: 36px 24px 16px;
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .check-now-btn {{
      background: var(--gold);
      color: var(--green-deep);
      border: none;
      padding: 14px 0;
      width: 200px;
      flex-shrink: 0;
      font-family: 'Bebas Neue', sans-serif;
      font-size: 1.1rem;
      letter-spacing: 0.12em;
      border-radius: 40px;
      cursor: pointer;
      transition: all 0.2s;
      box-shadow: 0 4px 16px rgba(232,185,74,0.4);
    }}
    .check-now-btn:hover {{ background: var(--gold-dark); transform: translateY(-2px); }}
    .check-now-btn:disabled {{ opacity: 0.6; cursor: not-allowed; transform: none; }}
    
    @media (max-width: 600px) {{
      .check-now-wrap {{
        flex-direction: column;
        gap: 12px;
      }}
      .check-now-btn {{
        width: 100%;
        max-width: 280px;
      }}
    }}

    .trigger-msg {{
      width: 100%;
      text-align: center;
      font-size: 0.8rem;
      color: var(--text-mid);
      min-height: 1.2em;
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
      color: var(--white);
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
  </style>
</head>
<body>

  <div class="ticker">
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
        <h1>TEE TIME<br><em>WATCH</em></h1>
        <p class="subtitle">Miami Area Golf &nbsp;·&nbsp; Weekend Availability</p>
        <span class="window-tag">⏱ {course["tee_time_min"]}:00 AM – {cutoff_ampm} (SUNSET MINUS ~4HRS)</span>
      </div>
      <span class="header-golfer">🏌️</span>
    </div>
  </header>

  <div class="updated-bar">
    Checked every 15 minutes &nbsp;·&nbsp; Last run: <strong>{now_str}</strong><span class="mins-ago" id="mins-ago"></span>
  </div>

  <main>
    {cards_html}
  </main>

  <div class="callout-strip">
    <div class="callout-quote">FORE!<span>heads up</span></div>
    <div class="callout-divider"></div>
    <div class="callout-quote">BOOK FAST<span>they go quick</span></div>
    <div class="callout-divider"></div>
    <div class="callout-quote">TEE IT UP<span>weekend's calling</span></div>
  </div>

  <div class="check-now-wrap">
    <button class="check-now-btn" onclick="triggerCheck()">CHECK NOW ⛳</button>
    <button class="check-now-btn" onclick="this.textContent='RELOADING… ↺'; location.reload()">REFRESH ↺</button>
  </div>
  <div style="text-align:center; margin-top:8px;">
    <div class="trigger-msg" id="trigger-msg"></div>
  </div>

  <footer>
    <span class="footer-fore">⛳ 🏌️ ⛳</span>
    Monitoring {len(COURSES)} courses · Fri–Sun · Times shown in ET · © {datetime.now(ET).year} Tee Time Watch
  </footer>

  <div class="toast" id="toast"></div>

  <script>
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

    // ── Toast helper ──
    function showToast(msg, duration = 3500) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), duration);
    }}

    // ── Check Now ──
    async function triggerCheck() {{
      const btn = document.querySelector('.check-now-btn');
      btn.disabled = true;
      btn.textContent = 'TRIGGERING… ⛳';
      try {{
        const resp = await fetch('/api/trigger', {{ method: 'POST' }});
        const data = await resp.json();
        if (resp.ok) {{
          showToast('✓ Check triggered! Results update in ~2 mins.');
        }} else {{
          showToast('Error: ' + (data.error || 'unknown'));
        }}
      }} catch (e) {{
        showToast('Network error — try again.');
      }}
      btn.textContent = 'CHECK NOW ⛳';
      btn.disabled = false;
    }}
  </script>

</body>
</html>"""

    Path("index.html").write_text(html)
    print("  index.html generated.")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    dates = get_upcoming_weekend_dates()

    print(f"\nTee Time Monitor")
    print(f"  Courses: {', '.join(c['name'] for c in COURSES)}")
    print(f"  Dates:   {', '.join(d.strftime('%a %b %-d') for d in dates)}\n")

    for course in COURSES:
        print(f"\n{'#'*60}")
        print(f"  {course['name']}")
        print(f"{'#'*60}")
        for d in dates:
            await check_day(course, d)

    print("\n" + "="*60)
    print("Generating index.html...")
    generate_html()
    print("Done.\n")


if __name__ == "__main__":
    asyncio.run(main())
