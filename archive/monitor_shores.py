"""
Miami Shores Golf Course - Tee Time Monitor
Uses Chronogolf platform (chronogolf.com)
Date goes directly in the URL -- no calendar interaction needed.

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
from playwright.async_api import async_playwright

# ── Configuration ─────────────────────────────────────────────────────────────

COURSE_NAME = "Miami Shores"
BASE_URL    = "https://www.chronogolf.com/club/miami-shores-country-club"

# URL parameters
HOLES      = 18
GROUP_SIZE = 4

# Always monitored as a matched Fri/Sat/Sun weekend block
DAYS_TO_MONITOR = [4, 5, 6]   # 4=Friday, 5=Saturday, 6=Sunday

# Tee time window (24h). 0=midnight, 6=6AM, 18=6PM
TEE_TIME_MIN = 8
TEE_TIME_MAX = 14

# Email
SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")

# Pushover (optional -- leave secrets blank to skip)
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

# Separate cache file so it doesn't collide with Miami Lakes
CACHE_FILE = Path("last_teetimes_shores.json")
ET         = ZoneInfo("America/New_York")

DAY_NAMES = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday",  5: "Saturday", 6: "Sunday"
}

# ─────────────────────────────────────────────────────────────────────────────


def get_upcoming_weekend_dates() -> list[date]:
    """
    Return all Fri/Sat/Sun from today through the next 9 days (Eastern Time).
    Covers remaining days of this weekend + the full next weekend.
    Never returns past dates so Chronogolf never gets stale date requests.
    """
    today = datetime.now(ET).date()
    dates = []
    for i in range(6):
        d = today + timedelta(days=i)
        if d.weekday() in (4, 5, 6):
            dates.append(d)
    return dates


def build_url(target_date: date) -> str:
    return (
        f"{BASE_URL}"
        f"?date={target_date.isoformat()}"
        f"&step=teetimes"
        f"&holes={HOLES}"
        f"&coursesIds="
        f"&deals=false"
        f"&groupSize={GROUP_SIZE}"
    )


def is_within_time_window(time_str: str) -> bool:
    try:
        parts = time_str.strip().split()
        hour, _ = map(int, parts[0].split(":"))
        ampm = parts[1].upper() if len(parts) > 1 else "AM"
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return TEE_TIME_MIN <= hour <= TEE_TIME_MAX
    except Exception:
        return True


def deduplicate_slots(slots: list[dict]) -> list[dict]:
    seen = set()
    out  = []
    for slot in slots:
        t = slot.get("time", "").strip().upper()
        h = slot.get("holes", "").strip().upper()
        if not is_within_time_window(slot.get("time", "")):
            continue
        key = (t, h)
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out


# ── Notifications ─────────────────────────────────────────────────────────────

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
                "priority": 1,
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
        print("  Email credentials not set -- skipping.")
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


def notify(subject: str, body: str, push_message: str):
    send_email(subject, body)
    send_pushover(subject, push_message)


# ── Browser scraping ──────────────────────────────────────────────────────────

async def scrape_chronogolf(target_date: date) -> list[dict]:
    """
    Navigate directly to the date URL and extract tee times.
    Chronogolf renders an Angular app -- we wait for the tee time
    list to appear before scraping.
    """
    url = build_url(target_date)
    print(f"  URL: {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
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

        page = await context.new_page()
        print(f"  Loading page...")
        await page.goto(url, wait_until="networkidle", timeout=60_000)

        # Chronogolf is an Angular SPA -- give it extra time to render
        await page.wait_for_timeout(5_000)

        # Try to wait for a tee time element to appear
        try:
            await page.wait_for_selector(
                '[class*="teetime"], [class*="tee-time"], [class*="timeslot"], '
                '[class*="booking"], [class*="slot"], [class*="green-fee"]',
                timeout=10_000
            )
            print("  Tee time elements detected.")
        except Exception:
            print("  Timed out waiting for tee time elements -- scraping anyway.")

        # ── Debug: print page title and a snippet of visible text ─────────
        title = await page.title()
        print(f"  Page title: {title}")

        # ── Extract tee times ─────────────────────────────────────────────
        tee_times = await page.evaluate("""
            () => {
                const results  = [];
                const seenKeys = new Set();

                // Chronogolf-specific selectors (Angular component classes)
                // These are ordered from most to least specific
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
                    '[class*="result"]',
                    'app-teetime',
                    'app-tee-time',
                    'app-slot'
                ];

                let cards = [];
                let usedSelector = '';
                for (const sel of cardSelectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0) {
                        cards = Array.from(found);
                        usedSelector = sel;
                        break;
                    }
                }

                console.log('Selector used: ' + usedSelector + ', cards found: ' + cards.length);

                if (cards.length > 0) {
                    for (const card of cards) {
                        const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!raw || raw.length < 3) continue;

                        // Match time patterns: "3:00 PM", "3:00PM", "15:00"
                        const timeMatch12 = raw.match(/(\\d{1,2}:\\d{2})\\s*(AM|PM)/i);
                        const timeMatch24 = raw.match(/\\b([01]?\\d|2[0-3]):(00|15|30|45)\\b/);
                        const holeMatch   = raw.match(/(\\d+)\\s*hole/i);
                        const priceMatch  = raw.match(/\\$[\\d,.]+/);

                        let time = '';
                        if (timeMatch12) {
                            time = timeMatch12[1] + ' ' + timeMatch12[2].toUpperCase();
                        } else if (timeMatch24) {
                            // Convert 24h to 12h
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
                                results.push({
                                    time,
                                    holes,
                                    price: priceMatch ? priceMatch[0] : ''
                                });
                            }
                        }
                    }
                }

                // Fallback: scan full page text
                if (results.length === 0) {
                    const fullText = document.body.innerText.replace(/\\s+/g, ' ');

                    // Try 12h format first
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

                    // Try 24h format if still nothing
                    if (results.length === 0) {
                        const pat24 = /\\b([01]?\\d|2[0-3]):(00|15|30|45)\\b/g;
                        while ((m = pat24.exec(fullText)) !== null) {
                            let h = parseInt(m[1]);
                            const min  = m[2];
                            const ampm = h >= 12 ? 'PM' : 'AM';
                            if (h > 12) h -= 12;
                            if (h === 0) h = 12;
                            const time = h + ':' + min + ' ' + ampm;
                            const key  = time + '|';
                            if (!seenKeys.has(key)) {
                                seenKeys.add(key);
                                results.push({ time, holes: '', price: '' });
                            }
                        }
                    }
                }

                return results;
            }
        """)

        # ── Debug: dump first 2000 chars of page text if nothing found ────
        if not tee_times:
            snippet = await page.evaluate(
                "() => document.body.innerText.slice(0, 2000)"
            )
            print(f"  DEBUG page text snippet:\n{snippet}\n")

        await browser.close()

    print(f"  Raw slots found: {len(tee_times)}")
    return tee_times


# ── Cache ─────────────────────────────────────────────────────────────────────

def cache_key(d: date) -> str:
    return f"shores_{d.isoformat()}"


def load_cache(d: date) -> list[dict]:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text()).get(cache_key(d), [])
    except Exception:
        pass
    return []


def save_cache(d: date, slots: list[dict]):
    all_cache = {}
    try:
        if CACHE_FILE.exists():
            content = CACHE_FILE.read_text().strip()
            if content:
                data = json.loads(content)
                all_cache = data if isinstance(data, dict) else {}
    except Exception:
        pass
    all_cache[cache_key(d)] = slots
    CACHE_FILE.write_text(json.dumps(all_cache, indent=2))
    print(f"  Cache updated for {d.strftime('%A, %B %-d')}")


def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]


# ── Per-day check ─────────────────────────────────────────────────────────────

async def check_day(target_date: date):
    day_name = DAY_NAMES.get(target_date.weekday(), "Unknown")
    print(f"\n{'='*60}")
    print(f"{COURSE_NAME} -- Checking {day_name}, {target_date.strftime('%B %-d, %Y')}")
    print(f"  Time window: {TEE_TIME_MIN:02d}:00 - {TEE_TIME_MAX:02d}:59")
    print(f"{'='*60}\n")

    # Chronogolf silently redirects past dates to today -- skip them entirely
    today_et = datetime.now(ET).date()
    if target_date < today_et:
        print(f"  Skipping past date {target_date} to avoid Chronogolf redirect.")
        return

    current_slots = deduplicate_slots(await scrape_chronogolf(target_date))
    print(f"  Found {len(current_slots)} unique slot(s) after dedup.")

    if not current_slots:
        print("  No slots found -- skipping.\n")
        return

    cached_slots = load_cache(target_date)
    new_slots    = find_new_slots(cached_slots, current_slots)

    if new_slots:
        print(f"  {len(new_slots)} NEW slot(s)!")

        date_label = f"{day_name}, {target_date.strftime('%B %-d, %Y')}"
        subject    = f"Tee Time Alert - {COURSE_NAME} {target_date.strftime('%a %b %-d')}"
        book_url   = build_url(target_date)

        lines = [
            f"New tee time(s) just opened at {COURSE_NAME}",
            f"for {date_label}:\n",
        ]
        for slot in new_slots:
            line = f"  - {slot.get('time', '?')}"
            if slot.get("holes"): line += f"  |  {slot['holes']}"
            if slot.get("price"): line += f"  |  {slot['price']}"
            lines.append(line)
        lines.append(f"\nBook here:\n{book_url}")
        body = "\n".join(lines)

        slot_list = ", ".join(s.get("time", "?") for s in new_slots)
        push_msg  = (
            f"{len(new_slots)} new slot(s) on {date_label}:\n{slot_list}\n\n"
            f"Book: {book_url}"
        )

        notify(subject, body, push_msg)
    else:
        print("  No new slots since last check.")

    save_cache(target_date, current_slots)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{COURSE_NAME} Tee Time Monitor")
    print(f"  Monitoring: Friday, Saturday, Sunday (rolling 9-day window)\n")

    dates = get_upcoming_weekend_dates()

    for d in dates:
        await check_day(d)

    print("\n" + "="*60)
    print("Done.\n")


if __name__ == "__main__":
    asyncio.run(main())
