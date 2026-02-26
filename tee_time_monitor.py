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
    """
    Generates a static HTML dashboard for golf tee time availability.
    """
    dates = get_upcoming_weekend_dates()
    now = datetime.now(ET)
    now_str = now.strftime("%-I:%M %p ET, %A %B %-d, %Y")
    now_ts = int(now.timestamp())

    # 1. Build data structure: course -> date -> slots
    course_data = []
    for course in COURSES:
        cache_file = CACHE_DIR / course["cache_file"]
        days = []
        
        for d in dates:
            t_max_day = get_sunset_cutoff(d, course["tee_time_max"])
            slots = load_cache(cache_file, d)
            
            # Construct booking URL
            if course["type"] == "cpsgolf":
                book_params = f"TeeOffTimeMin={course['tee_time_min']}&TeeOffTimeMax={t_max_day}"
                book_url = f"{course['url']}?{book_params}"
            else:
                book_url = (
                    f"{course['url']}?date={d.isoformat()}"
                    f"&step=teetimes&holes={course.get('holes', 18)}"
                    f"&coursesIds=&deals=false"
                    f"&groupSize={course.get('group_size', 4)}"
                )
            
            days.append({
                "date": d,
                "label": d.strftime("%a, %b %-d"),
                "slots": slots,
                "book_url": book_url,
            })
            
        course_data.append({"course": course, "days": days})

    # 2. Build course cards HTML
    cards_html = ""
    for cd in course_data:
        course = cd["course"]
        days_html = ""
        
        for day in cd["days"]:
            if day["slots"]:
                times_list = []
                for s in day["slots"]:
                    price = f" · {s['price']}" if s.get("price") else ""
                    times_list.append(f'<span class="slot">{s.get("time", "?")}{price}</span>')
                times_html = "".join(times_list)
                day_body = f'<div class="slots">{times_html}</div>'
            else:
                day_body = '<p class="no-times">No times available</p>'

            days_html += f"""
            <div class="day-block">
                <a class="book-btn-float" href="{day["book_url"]}" target="_blank">BOOK →</a>
                <div class="day-name">{day["label"]}</div>
                {day_body}
            </div>"""

        # Time formatting logic
        repr_date = cd["days"][0]["date"] if cd["days"] else None
        repr_cutoff = get_sunset_cutoff(repr_date, course["tee_time_max"]) if repr_date else course["tee_time_max"]
        period = "AM" if repr_cutoff < 12 else "PM"
        cutoff_ampm = f"{repr_cutoff % 12 or 12}:00 {period}"

        cards_html += f"""
        <div class="course-card">
          <div class="card-header">
            <div class="course-name">{course["name"]}</div>
            <div class="course-meta">{course.get("address", "")}</div>
            <span class="window-tag-small">⏱ {course["tee_time_min"]}AM – {cutoff_ampm}</span>
          </div>
          {days_html}
        </div>"""

    # 3. Final HTML assembly
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
    *, 
    *::before, 
    *::after {{ 
      box-sizing: border-box; 
      margin: 0; 
      padding: 0; 
    }}

    :root {{
      --green-deep: #0d2b1a;
      --green-mid: #1a5c32;
      --green-light: #2e8b4f;
      --green-pale: #d4eddc;
      --fairway: #3a7d44;
      --white: #ffffff;
      --cream: #f5f0e8;
      --gold: #e8b94a;
      --gold-dark: #c49a28;
      --text-dark: #0d2b1a;
      --text-mid: #3a5c42;
      --text-light: #7a9485;
    }}

    body {{ 
      font-family: 'DM Sans', sans-serif; 
      background: var(--cream); 
      color: var(--text-dark); 
      min-height: 100vh; 
      font-size: 0.9rem; 
    }}

    /* Ticker Styles */
    .ticker {{ 
      background: var(--gold); 
      color: var(--green-deep); 
      font-family: 'Bebas Neue', sans-serif; 
      font-size: 0.85rem; 
      padding: 4px 0; 
      overflow: hidden; 
      white-space: nowrap; 
    }}

    .ticker-inner {{ 
      display: inline-block; 
      animation: ticker 36s linear infinite; 
    }}

    .ticker-inner span {{ 
      margin: 0 30px; 
    }}

    @keyframes ticker {{ 
      0% {{ transform: translateX(0); }} 
      100% {{ transform: translateX(-50%); }} 
    }}

    /* Header Styles */
    header {{ 
      background: var(--green-deep); 
      padding: 20px 15px; 
      text-align: center; 
      position: relative; 
      border-bottom: 4px solid var(--fairway); 
    }}

    .header-inner {{ 
      display: flex; 
      align-items: center; 
      justify-content: center; 
      gap: 12px; 
    }}

    .header-flag, 
    .header-golfer {{ 
      font-size: 3rem; 
      animation: flagwave 3s ease-in-out infinite; 
    }}

    @keyframes flagwave {{ 
      0%, 100% {{ transform: rotate(-3deg); }} 
      50% {{ transform: rotate(3deg); }} 
    }}
    
    h1 {{ 
      font-family: 'Bebas Neue', sans-serif; 
      font-size: 3.2rem; 
      color: var(--white); 
      line-height: 0.85; 
      letter-spacing: 0.05em; 
    }}

    h1 em {{ 
      color: var(--gold); 
      font-style: normal; 
    }}

    .subtitle {{ 
      font-size: 0.65rem; 
      color: rgba(255,255,255,0.4); 
      margin-top: 5px; 
      text-transform: uppercase; 
      letter-spacing: 1.5px; 
    }}

    .updated-bar {{ 
      background: var(--green-mid); 
      text-align: center; 
      padding: 6px 15px; 
      font-size: 0.7rem; 
      color: rgba(255,255,255,0.7); 
      border-bottom: 2px solid var(--gold); 
    }}

    .updated-bar strong {{ 
      color: var(--white); 
    }}

    /* Layout & Cards */
    main {{ 
      max-width: 1100px; 
      margin: 15px auto 0; 
      padding: 0 12px; 
      display: grid; 
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); 
      gap: 15px; 
    }}

    .course-card {{ 
      background: var(--white); 
      border-radius: 12px; 
      overflow: hidden; 
      box-shadow: 0 4px 15px rgba(0,0,0,0.08); 
      border: 1px solid rgba(0,0,0,0.05); 
      transition: transform 0.2s; 
    }}

    .card-header {{ 
      background: linear-gradient(135deg, var(--green-deep), var(--green-mid)); 
      padding: 12px 15px; 
    }}

    .course-name {{ 
      font-family: 'Bebas Neue', sans-serif; 
      font-size: 1.5rem; 
      color: var(--white); 
      line-height: 1; 
    }}

    .course-meta {{ 
      font-size: 0.6rem; 
      color: rgba(255,255,255,0.5); 
    }}

    .window-tag-small {{ 
      color: var(--gold); 
      font-size: 0.6rem; 
      font-weight: bold; 
      text-transform: uppercase; 
      margin-top: 4px; 
      display: block; 
    }}

    .day-block {{ 
      padding: 10px 15px; 
      border-bottom: 1px solid #f0f0f0; 
      position: relative; 
      min-height: 60px; 
    }}

    .day-name {{ 
      font-size: 0.7rem; 
      font-weight: 700; 
      color: var(--text-mid); 
      text-transform: uppercase; 
      margin-bottom: 6px; 
    }}
    
    .book-btn-float {{
      position: absolute; 
      top: 10px; 
      right: 12px; 
      font-size: 0.6rem; 
      font-weight: bold;
      background: rgba(232, 185, 74, 0.15); 
      color: var(--gold-dark); 
      text-decoration: none;
      padding: 3px 8px; 
      border-radius: 4px; 
      border: 1px solid rgba(232, 185, 74, 0.3);
      transition: all 0.2s;
    }}

    .book-btn-float:hover {{ 
      background: var(--gold); 
      color: var(--green-deep); 
    }}

    .slots {{ 
      display: flex; 
      flex-wrap: wrap; 
      gap: 4px; 
      padding-right: 45px; 
    }}

    .slot {{ 
      background: var(--green-pale); 
      color: var(--green-deep); 
      font-size: 0.75rem; 
      font-weight: 600; 
      padding: 4px 8px; 
      border-radius: 4px; 
      border: 1px solid rgba(46,139,79,0.1); 
    }}

    .no-times {{ 
      font-size: 0.7rem; 
      color: var(--text-light); 
      font-style: italic; 
    }}

    /* Buttons & Footer */
    .check-now-wrap {{ 
      text-align: center; 
      padding: 20px; 
      display: flex; 
      justify-content: center; 
      gap: 10px; 
    }}

    .check-now-btn {{ 
      background: var(--green-deep); 
      color: var(--gold); 
      border: 1px solid var(--gold); 
      padding: 10px 20px; 
      font-family: 'Bebas Neue', sans-serif; 
      font-size: 1rem; 
      border-radius: 30px; 
      cursor: pointer; 
    }}
    
    footer {{ 
      text-align: center; 
      padding: 20px; 
      font-size: 0.7rem; 
      color: var(--text-light); 
    }}

    .toast {{ 
      position: fixed; 
      bottom: 20px; 
      left: 50%; 
      transform: translateX(-50%); 
      background: var(--green-deep); 
      color: white; 
      padding: 10px 20px; 
      border-radius: 20px; 
      font-size: 0.8rem; 
      border: 1px solid var(--gold); 
      opacity: 0; 
      transition: 0.3s; 
      pointer-events: none; 
    }}

    .toast.show {{ 
      opacity: 1; 
    }}
  </style>
</head>
<body>
  <div class="ticker">
    <div class="ticker-inner">
      <span>FORE! ⛳</span><span>TEE TIME WATCH 🏌️</span><span>MIAMI AREA GOLF ⛳</span><span>JUST BOOK THE TEE TIME 🏌️</span>
      <span>FORE! ⛳</span><span>TEE TIME WATCH 🏌️</span><span>MIAMI AREA GOLF ⛳</span><span>JUST BOOK THE TEE TIME 🏌️</span>
    </div>
  </div>

  <header>
    <div class="header-inner">
      <span class="header-flag">⛳</span>
      <div>
        <h1>TEE TIME <em>WATCH</em></h1>
        <p class="subtitle">Miami Area · Weekend Availability</p>
      </div>
      <span class="header-golfer">🏌️</span>
    </div>
  </header>

  <div class="updated-bar">
    Last run: <strong>{now_str}</strong><span id="mins-ago"></span>
  </div>

  <main>{cards_html}</main>

  <div class="check-now-wrap">
    <button class="check-now-btn" onclick="triggerCheck()">CHECK NOW ⛳</button>
    <button class="check-now-btn" onclick="location.reload()">REFRESH ↺</button>
  </div>

  <footer>
    Monitoring {len(COURSES)} courses · © {now.year} Tee Time Watch
  </footer>

  <div class="toast" id="toast"></div>

  <script>
    (function() {{
      const el = document.getElementById('mins-ago');
      const lastRun = new Date({now_ts} * 1000);

      function update() {{
        const mins = Math.floor((Date.now() - lastRun) / 60000);
        el.textContent = mins < 1 ? ' · just now' : ' · ' + mins + 'm ago';
      }}

      update();
      setInterval(update, 30000);
    }})();

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 3000);
    }}

    async function triggerCheck() {{
      const btn = document.querySelector('.check-now-btn');
      btn.disabled = true;
      btn.textContent = 'TRIGGERING...';
      
      try {{
        const resp = await fetch('/api/trigger', {{ method: 'POST' }});
        if (resp.ok) {{
          showToast('✓ Triggered! Updates in ~2 mins.');
        }} else {{
          showToast('Error triggering.');
        }}
      }} catch (e) {{
        showToast('Network error.');
      }} finally {{
        btn.textContent = 'CHECK NOW ⛳';
        btn.disabled = false;
      }}
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
