"""
Miami Lakes Tee Time Monitor
Checks CPS Golf for available tee times on the coming Friday
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
URL            = "https://miamilakes.cps.golf/onlineresweb/search-teetime?TeeOffTimeMin=0&TeeOffTimeMax=18"

SMTP_SERVER    = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO       = os.environ.get("EMAIL_TO")

CACHE_FILE     = Path("last_teetimes.json")
# ─────────────────────────────────────────────────────────────────────────────


def next_friday() -> date:
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


async def select_friday_and_scrape(friday: date) -> list[dict]:
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

        print(f"  Loading page...")
        await page.goto(URL, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

        # ── Step 1: Navigate to the correct month ────────────────────────────
        target_month_str = friday.strftime("%B %Y")
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
        day_num = str(friday.day)
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
        # The site renders times split across elements (e.g. "3:00" + "P" + "M")
        # so we use JavaScript to read the full innerText of each booking card
        # and reconstruct the time + details from what the user would see.
        tee_times = await page.evaluate("""
            () => {
                const results = [];

                // Look for booking card containers — try several selector patterns
                const cardSelectors = [
                    '[class*="teetime"]', '[class*="tee-time"]',
                    '[class*="timeslot"]', '[class*="time-slot"]',
                    '[class*="booking"]', '[class*="result-item"]',
                    '[class*="search-result"]', '[class*="tee-card"]'
                ];

                let cards = [];
                for (const sel of cardSelectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > 0) {
                        cards = Array.from(found);
                        break;
                    }
                }

                if (cards.length > 0) {
                    for (const card of cards) {
                        const raw = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!raw) continue;

                        // Reconstruct time: look for pattern like "3:00 P M" -> "3:00 PM"
                        const timeMatch = raw.match(/(\\d{1,2}:\\d{2})\\s*P\\s*M|(\\d{1,2}:\\d{2})\\s*A\\s*M/i);
                        const holeMatch = raw.match(/\\d+\\s*HOLE/i);
                        const priceMatch = raw.match(/\\$[\\d.]+/);

                        if (timeMatch) {
                            const timeBase = timeMatch[1] || timeMatch[2];
                            const ampm = timeMatch[1] ? 'PM' : 'AM';
                            results.push({
                                time: timeBase + ' ' + ampm,
                                holes: holeMatch ? holeMatch[0] : '',
                                price: priceMatch ? priceMatch[0] : ''
                            });
                        }
                    }
                }

                // Fallback: scan full page text for the split-element time pattern
                if (results.length === 0) {
                    const fullText = document.body.innerText.replace(/\\s+/g, ' ');
                    // Match "3:00 P M" or "3:00 P M" style (split PM)
                    const pattern = /(\\d{1,2}:\\d{2})\\s*P\\s*M|(\\d{1,2}:\\d{2})\\s*A\\s*M/gi;
                    let match;
                    const seen = new Set();
                    while ((match = pattern.exec(fullText)) !== null) {
                        const base = match[1] || match[2];
                        const ampm = match[1] ? 'PM' : 'AM';
                        const key = base + ' ' + ampm;
                        if (!seen.has(key)) {
                            seen.add(key);
                            results.push({ time: key, holes: '', price: '' });
                        }
                    }
                }

                return results;
            }
        """)

        await browser.close()

    print(f"  Raw slots found: {tee_times}")
    return tee_times


def load_cache() -> list[dict]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return []


def save_cache(data: list[dict]):
    CACHE_FILE.write_text(json.dumps(data, indent=2))


def find_new_slots(old: list[dict], new: list[dict]) -> list[dict]:
    old_times = {t.get("time", "").strip().upper() for t in old}
    return [t for t in new if t.get("time", "").strip().upper() not in old_times]


def send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_TO]):
        print("  ⚠ Email credentials not configured — printing alert to console:\n")
        print(f"  SUBJECT: {subject}\n\n{body}")
        return
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_TO
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_TO], msg.as_string())
    print(f"  ✅ Alert email sent to {EMAIL_TO}")


async def main():
    friday = next_friday()
    print(f"\n🏌️  Miami Lakes Tee Time Monitor")
    print(f"  Target date : {friday.strftime('%A, %B %-d, %Y')}\n")

    current_slots = await select_friday_and_scrape(friday)
    print(f"\n  Found {len(current_slots)} tee time slot(s).")

    if not current_slots:
        print("  Nothing to compare — no alert sent.\n")
        return

    cached_slots = load_cache()
    new_slots    = find_new_slots(cached_slots, current_slots)

    if new_slots:
        print(f"  🚨 {len(new_slots)} NEW slot(s) detected!")
        lines = [
            f"New tee time(s) just opened at Miami Lakes Golf Course",
            f"for {friday.strftime('%A, %B %-d, %Y')}:\n",
        ]
        for slot in new_slots:
            time  = slot.get("time",  "Unknown time")
            holes = slot.get("holes", "")
            price = slot.get("price", "")
            line  = f"  • {time}"
            if holes: line += f"  |  {holes}"
            if price: line += f"  |  {price}"
            lines.append(line)
        lines.append(f"\nBook here:\n{URL}")
        body = "\n".join(lines)
        send_email(
            subject=f"⛳ Tee Time Alert – Miami Lakes {friday.strftime('%a %b %-d')}",
            body=body
        )
    else:
        print("  No new slots since last check — no alert sent.")

    save_cache(current_slots)
    print("\nDone.\n")


if __name__ == "__main__":
    asyncio.run(main())
