"""
Miami Lakes Tee Time Monitor
Checks CPS Golf for available tee times on specified days
and sends an email + Pushover push notification when new slots appear.

Setup:
  pip install playwright requests
  playwright install chromium
"""

import asyncio
import json
import os
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = "https://miamilakes.cps.golf/onlineresweb/search-teetime"

# Days to monitor: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
DAYS_TO_MONITOR = [4,5,6]   # Friday only — add 5,6 for Sat/Sun

# Tee time window (24h). 0=midnight, 6=6AM, 18=6PM
TEE_TIME_MIN = 7
TEE_TIME_MAX = 14

# Email
SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")   # comma-separated for multiple

# Pushover (optional — leave secrets blank to skip)
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")

CACHE_FILE = Path("last_teetimes.json")

DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
             4: "Friday",  5: "Saturday", 6: "Sunday"}

# ─────────────────────────────────────────────────────────────────────────────


def get_next_occurrences(days_to_check: list[int]) -> dict[int, date]:
    """Return the soonest upcoming date for each requested weekday."""
    today = date.today()
    result = {}
    for i in range(1, 15):          # look up to 2 weeks ahead
        d = today + timedelta(days=i)
        wd = d.weekday()
        if wd in days_to_check and wd not in result:
            result[wd] = d
        if len(result) == len(days_to_check):
            break
    return result


def is_within_time_window(time_str: str) -> bool:
    try:
        parts = time_str.split()
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
        if not h:
            continue
        if not is_within_time_window(slot.get("time", "")):
            continue
        key = (t, h)
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out


# ── Notifications ─────────────────────────────────────────────────────────────

def send_pushover(title: str, message: str):
    """Send an iPhone push notification via Pushover."""
    if not all([PUSHOVER_USER, PUSHOVER_TOKEN]):
        print("  ℹ Pushover credentials not set — skipping push notification.")
        return
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   PUSHOVER_TOKEN,
                "user":    PUSHOVER_USER,
                "title":   title,
                "message": message,
                "sound":   "cashregister",   # satisfying alert sound
                "priority": 1,               # high priority — bypasses quiet hours
            },
            timeout=10,
        )
        if resp.status_code == 200:
            print("  ✅ Pushover notification sent.")
        else:
            print(f"  ⚠ Pushover error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  ⚠ Pushover exception: {e}")


def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        print("  ℹ Email credentials not set — skipping email.")
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
        print(f"  ✅ Email sent to {', '.join(recipients)}")
    except Exception as e:
        print(f"  ⚠ Email error: {e}")


def notify(subject: str, body: str, push_message: str):
    """Send all configured notifications."""
    send_email(subject, body)
    send_pushover(subject, push_message)


# ── Browser scraping ──────────────────────────────────────────────────────────

async def select_day_and_scrape(target_date: date) -> list[dict]:
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
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()
        url  = f"{BASE_URL}?TeeOffTimeMin={TEE_TIME_MIN}&TeeOffTimeMax={TEE_TIME_MAX}"

        print(f"  Loading page...")
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

        # ── Navigate to correct month ─────────────────────────────────────────
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
                print(f"  ✓ Month: {header}")
                break
            if not header:
                print(f"  Header not found — proceeding anyway.")
                break

            print(f"  Advancing from '{header}'...")
            await page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const t = (el.innerText || '').trim();
                        if (t === '›' || t === '>' || t === '▶' || t === '→') {
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

        # ── Click the day ─────────────────────────────────────────────────────
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
            print(f"  ⚠ Could not find day {day_num}.")
            await browser.close()
            return []

        print(f"  ✓ {clicked}")
        await page.wait_for_timeout(4_000)

        # ── Extract tee times from rendered DOM ───────────────────────────────
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
                            const holes    = holeMatch  ? holeMatch[0]  : '';
                            const key      = time + '|' + holes;
                            if (!seenKeys.has(key)) {
                                seenKeys.add(key);
                                results.push({ time, holes, price: priceMatch ? priceMatch[0] : '' });
                            }
                        }
                    }
                }

                // Fallback: scan full page text for split-element times (e.g. "3:00 P M")
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


# ── Cache ─────────────────────────────────────────────────────────────────────

def cache_key(d: date) -> str:
    return f"teetimes_{d.isoformat()}"


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
    print(f"  ✅ Cache updated for {d.strftime('%A, %B %-d')}")


# ── Per-day check ─────────────────────────────────────────────────────────────

async def check_day(target_date: date):
    day_name = DAY_NAMES.get(target_date.weekday(), "Unknown")
    print(f"\n{'='*60}")
    print(f"🏌️  Checking {day_name}, {target_date.strftime('%B %-d, %Y')}")
    print(f"  Time window: {TEE_TIME_MIN:02d}:00 – {TEE_TIME_MAX:02d}:59")
    print(f"{'='*60}\n")

    current_slots = deduplicate_slots(await select_day_and_scrape(target_date))
    print(f"  Found {len(current_slots)} unique slot(s) after dedup.")

    if not current_slots:
        print("  No slots found — skipping.\n")
        return

    cached_slots = load_cache(target_date)
    new_slots    = find_new_slots(cached_slots, current_slots)

    if new_slots:
        print(f"  🚨 {len(new_slots)} NEW slot(s)!")

        date_label = f"{day_name}, {target_date.strftime('%B %-d, %Y')}"
        subject    = f"⛳ Tee Time Alert – Miami Lakes {target_date.strftime('%a %b %-d')}"

        # Build detailed email body
        lines = [f"New tee time(s) just opened at Miami Lakes Golf Course",
                 f"for {date_label}:\n"]
        for slot in new_slots:
            line = f"  • {slot.get('time', '?')}"
            if slot.get("holes"): line += f"  |  {slot['holes']}"
            if slot.get("price"): line += f"  |  {slot['price']}"
            lines.append(line)
        lines.append(f"\nBook here:\n{BASE_URL}")
        body = "\n".join(lines)

        # Compact push notification (Pushover messages have a 1024 char limit)
        slot_list   = ", ".join(s.get("time", "?") for s in new_slots)
        push_msg    = f"{len(new_slots)} new slot(s) on {date_label}:\n{slot_list}\n\nmiamilakes.cps.golf"

        notify(subject, body, push_msg)
    else:
        print("  No new slots since last check.")

    save_cache(target_date, current_slots)


def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n🏌️  Miami Lakes Tee Time Monitor")
    print(f"  Monitoring: {', '.join(DAY_NAMES[d] for d in DAYS_TO_MONITOR)}\n")

    dates = get_next_occurrences(DAYS_TO_MONITOR)

    for day_num in DAYS_TO_MONITOR:
        if day_num in dates:
            await check_day(dates[day_num])

    print("\n" + "="*60)
    print("✅ Done.\n")


if __name__ == "__main__":
    asyncio.run(main())
