"""
Miami Lakes Tee Time Monitor
Checks CPS Golf for available tee times on specified days
and sends an email alert when new slots appear.

Setup:
  pip install playwright
  playwright install chromium
"""

import asyncio
import json
import os
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

# ── Configuration ────────────────────────────────────────────────────────────
# CUSTOMIZATION: Change these values to suit your needs

# Golf course base URL (change to monitor a different course)
BASE_URL = "https://miamilakes.cps.golf/onlineresweb/search-teetime"

# Days to monitor: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday
# Default monitors Friday (4), Saturday (5), and Sunday (6)
DAYS_TO_MONITOR = [4, 5, 6]

# Tee time window: only alert for times between TEE_TIME_MIN and TEE_TIME_MAX (0-23)
# 0 = midnight, 6 = 6 AM, 12 = noon, 18 = 6 PM, 23 = 11 PM
TEE_TIME_MIN = 0      # Start of day (midnight)
TEE_TIME_MAX = 18     # End of day (6 PM) — change to 23 for all day

# Email configuration
SMTP_SERVER    = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")

# Cache file for tracking previously seen tee times
CACHE_FILE     = Path("last_teetimes.json")

# ───────────────────────────────────────────────────���─────────────────────────

# Day name mapping
DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 
             4: "Friday", 5: "Saturday", 6: "Sunday"}


def get_next_occurrences(days_to_check: list[int], num_weeks: int = 4) -> list[date]:
    """
    Get the next occurrences of specified days.
    
    Args:
        days_to_check: List of weekday numbers (0=Monday, 6=Sunday)
        num_weeks: How many weeks ahead to check (default 4)
    
    Returns:
        List of dates for the next occurrences of those days
    """
    today = date.today()
    target_dates = []
    
    for i in range(1, num_weeks * 7 + 1):
        check_date = today + timedelta(days=i)
        if check_date.weekday() in days_to_check:
            target_dates.append(check_date)
    
    return target_dates


def is_within_time_window(time_str: str) -> bool:
    """Check if a tee time falls within the configured time window."""
    try:
        # Parse time string like "3:00 PM" or "9:30 AM"
        time_part = time_str.split()[0]  # Get "3:00" from "3:00 PM"
        am_pm = time_str.split()[1] if len(time_str.split()) > 1 else "AM"
        
        hour, minute = map(int, time_part.split(":"))
        
        # Convert to 24-hour format
        if am_pm.upper() == "PM" and hour != 12:
            hour += 12
        elif am_pm.upper() == "AM" and hour == 12:
            hour = 0
        
        return TEE_TIME_MIN <= hour <= TEE_TIME_MAX
    except Exception:
        return True  # Include if we can't parse


def deduplicate_slots(slots: list[dict]) -> list[dict]:
    """Remove duplicate tee time slots and filter out slots with no hole info.
    
    Keeps only slots that have meaningful hole information.
    Removes duplicates by time + holes combination.
    """
    seen = set()
    deduped = []
    for slot in slots:
        time = slot.get("time", "").strip().upper()
        holes = slot.get("holes", "").strip().upper()
        
        # Skip slots with no hole information - these are duplicates
        if not holes:
            continue
        
        # Filter by time window
        if not is_within_time_window(slot.get("time", "")):
            continue
        
        # Create a key from time and holes
        key = (time, holes)
        
        if key not in seen:
            seen.add(key)
            deduped.append(slot)
    
    return deduped


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

        # Build full URL with time parameters
        url = f"{BASE_URL}?TeeOffTimeMin={TEE_TIME_MIN}&TeeOffTimeMax={TEE_TIME_MAX}"
        
        print(f"  Loading page...")
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

        # ── Step 1: Navigate to the correct month ────────────────────────────
        target_month_str = target_date.strftime("%B %Y")
        print(f"  Looking for month: {target_month_str}")

        for attempt in range(12):
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
                print(f"  ✓ Correct month: {header}")
                break

            if not header:
                print(f"  Header not found, proceeding anyway.")
                break

            print(f"  Advancing month from '{header}'...")
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

        # ── Step 2: Click the correct day ────────────────────────────────────
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
            print(f"  ⚠ Could not find day {day_num} to click.")
            await browser.close()
            return []

        print(f"  ✓ {clicked}")
        await page.wait_for_timeout(4_000)

        # ── Step 3: Extract tee times directly from the rendered DOM ─────────
        tee_times = await page.evaluate("""
            () => {
                const results = [];
                const cardSelectors = [
                    '[class*="teetime"]', '[class*="tee-time"]',
                    '[class*="timeslot"]', '[class*="time-slot"]',
                    '[class*="booking"]', '[class*="result-item"]',
                    '[class*="search-result"]', '[class*="tee-card"]',
                    '[data-time]', '[data-teetime]',
                    '.timeslot-item', '.tee-time-slot',
                    'div[role="button"]', 'button[aria-label*="time"]'
                ];

                let cards = [];
                for (const sel of cardSelectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0) {
                        cards = Array.from(found);
                        break;
                    }
                }

                const seenSlots = new Set();

                if (cards.length > 0) {
                    for (const card of cards) {
                        const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!raw) continue;

                        const timeMatch = raw.match(/(\\d{1,2}:\\d{2})\\s*P\\s*M|(\\d{1,2}:\\d{2})\\s*A\\s*M/i);
                        const holeMatch = raw.match(/\\d+\\s*HOLE/i);
                        const priceMatch = raw.match(/\\$[\\d.]+/);

                        if (timeMatch) {
                            const timeBase = timeMatch[1] || timeMatch[2];
                            const ampm = timeMatch[1] ? 'PM' : 'AM';
                            const time = timeBase + ' ' + ampm;
                            const holes = holeMatch ? holeMatch[0] : '';
                            
                            const key = time + '|' + holes;
                            
                            if (!seenSlots.has(key)) {
                                seenSlots.add(key);
                                results.push({
                                    time: time,
                                    holes: holes,
                                    price: priceMatch ? priceMatch[0] : ''
                                });
                            }
                        }
                    }
                }

                if (results.length === 0) {
                    const fullText = document.body.innerText.replace(/\\s+/g, ' ');
                    const pattern = /(\\d{1,2}:\\d{2})\\s*P\\s*M|(\\d{1,2}:\\d{2})\\s*A\\s*M/gi;
                    let match;
                    while ((match = pattern.exec(fullText)) !== null) {
                        const base = match[1] || match[2];
                        const ampm = match[1] ? 'PM' : 'AM';
                        const time = base + ' ' + ampm;
                        
                        const key = time + '|';
                        
                        if (!seenSlots.has(key)) {
                            seenSlots.add(key);
                            results.push({ time: time, holes: '', price: '' });
                        }
                    }
                }

                return results;
            }
        """)

        await browser.close()

    print(f"  Raw slots found: {len(tee_times)} slots")
    return tee_times


def get_cache_key(target_date: date) -> str:
    """Get a unique cache key for a specific date."""
    return f"teetimes_{target_date.isoformat()}"


def load_cache(target_date: date) -> list[dict]:
    """Load cached tee times for a specific date."""
    try:
        if CACHE_FILE.exists():
            all_cache = json.loads(CACHE_FILE.read_text())
            return all_cache.get(get_cache_key(target_date), [])
    except Exception:
        pass
    return []


def save_cache(target_date: date, data: list[dict]):
    """Save tee times for a specific date to cache."""
    try:
        # Load existing cache
        all_cache = {}
        if CACHE_FILE.exists():
            try:
                content = CACHE_FILE.read_text()
                if content.strip():
                    all_cache = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                all_cache = {}
        
        # Update cache for this date
        all_cache[get_cache_key(target_date)] = data
        
        # Save back to file
        CACHE_FILE.write_text(json.dumps(all_cache, indent=2))
    except Exception as e:
        print(f"  ⚠ Error saving cache: {e}")


def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]


def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        print("  ⚠ Email credentials not configured — printing alert to console:\n")
        print(f"  SUBJECT: {subject}\n\n{body}")
        return
    
    # Split EMAIL_TO by comma to support multiple recipients
    recipients = [email.strip() for email in EMAIL_TO.split(',')]
    
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
    print(f"  ✅ Alert email sent to {', '.join(recipients)}")


async def check_day(target_date: date):
    """Check tee times for a single day."""
    day_name = DAY_NAMES.get(target_date.weekday(), "Unknown")
    print(f"\n{'='*60}")
    print(f"🏌️  Checking {day_name}, {target_date.strftime('%B %-d, %Y')}")
    print(f"  Time window: {TEE_TIME_MIN:02d}:00 - {TEE_TIME_MAX:02d}:59")
    print(f"{'='*60}\n")

    current_slots = await select_day_and_scrape(target_date)
    
    # Deduplicate slots
    current_slots = deduplicate_slots(current_slots)
    
    print(f"  Found {len(current_slots)} unique tee time slot(s).")

    if not current_slots:
        print("  Nothing to compare — no alert sent.\n")
        return

    cached_slots = load_cache(target_date)
    new_slots    = find_new_slots(cached_slots, current_slots)

    if new_slots:
        print(f"  🚨 {len(new_slots)} NEW slot(s) detected!")
        lines = [
            f"New tee time(s) just opened at Miami Lakes Golf Course",
            f"for {day_name}, {target_date.strftime('%B %-d, %Y')}:\n",
        ]
        for slot in new_slots:
            time  = slot.get("time",  "Unknown time")
            holes = slot.get("holes", "")
            price = slot.get("price", "")
            line  = f"  • {time}"
            if holes: line += f"  |  {holes}"
            if price: line += f"  |  {price}"
            lines.append(line)
        lines.append(f"\nBook here:\n{BASE_URL}")
        body = "\n".join(lines)
        send_email(
            subject=f"⛳ Tee Time Alert – Miami Lakes {target_date.strftime('%a %b %-d')}",
            body=body
        )
    else:
        print("  No new slots since last check — no alert sent.")

    save_cache(target_date, current_slots)


async def main():
    print(f"\n🏌️  Miami Lakes Tee Time Monitor (Multi-Day)")
    print(f"  Monitoring: {', '.join(DAY_NAMES[d] for d in DAYS_TO_MONITOR)}")
    
    # Get the next occurrence of each monitored day
    target_dates = get_next_occurrences(DAYS_TO_MONITOR, num_weeks=4)
    
    # Check each date (limit to next occurrence of each day)
    unique_dates = {}
    for d in target_dates:
        day_num = d.weekday()
        if day_num not in unique_dates:
            unique_dates[day_num] = d
    
    for day_num in DAYS_TO_MONITOR:
        if day_num in unique_dates:
            await check_day(unique_dates[day_num])
    
    print("\n" + "="*60)
    print("✅ All days checked. Done.\n")


if __name__ == "__main__":
    asyncio.run(main())
