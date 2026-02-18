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
            print(f"  Calendar header: '{header}'")

            if target_month_str in header:
                print(f"  ✓ Correct month found.")
                break

            if not header:
                print(f"  Header not found, proceeding anyway.")
                break

            print(f"  Advancing month...")
            await page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const t = (el.innerText || '').trim();
                        if (t === '›' || t === '>' || t === '▶' || t === '→') {
                            el.click(); return true;
                        }
                    }
                    // Try aria-label
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
        # The calendar uses divs, not tds. We find any element whose
        # complete text is exactly our day number and click it.
        day_num = str(friday.day)
        print(f"  Clicking day {day_num}...")

        clicked = await page.evaluate(f"""
            () => {{
                const target = '{day_num}';

                // Search all elements for an exact text match
                const all = document.querySelectorAll('div, span, a, button, li');
                for (const el of all) {{
                    // Use innerText to get rendered text only (no children noise)
                    const text = (el.innerText || '').trim();
                    if (text !== target) continue;

                    const classes = (el.className || '').toLowerCase();
                    // Skip disabled/greyed/other-month cells
                    if (['gray','grey','disabled','prev','next','old','muted','inactive']
                            .some(c => classes.includes(c))) continue;

                    // Skip elements that are parents of other matching elements
                    // (we want the innermost element with just the number)
                    const children = el.querySelectorAll('*');
                    let childHasText = false;
                    for (const child of children) {{
                        if ((child.innerText || '').trim() === target) {{
                            childHasText = true;
                            break;
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

        # ── Step 3: Wait for results ─────────────────────────────────────────
        await page.wait_for_timeout(4_000)

        try:
            await page.wait_for_selector(
                "[class*='teetime'], [class*='tee-time'], "
                "[class*='timeslot'], [class*='booking'], "
                ".no-results, [class*='noResult']",
                timeout=10_000
            )
        except Exception:
            print("  ⚠ Result selector timed out — parsing whatever is rendered.")

        # Debug: print what's now on the page after clicking
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 800) : ''"
        )
        print(f"  Page after click:\n---\n{body_text}\n---")

        html = await page.content()
        await browser.close()

    return parse_tee_times(html)


def parse_tee_times(html: str) -> list[dict]:
    import re
    from html.parser import HTMLParser

    tee_times = []

    class CardParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_card = False
            self.depth   = 0
            self.current = {}

        def handle_starttag(self, tag, attrs):
            classes = dict(attrs).get("class", "").lower()
            if any(k in classes for k in
                   ["teetime", "tee-time", "timeslot", "time-slot",
                    "booking-item", "search-result-item", "result-item"]):
                self.in_card = True
                self.depth   = 0
                self.current = {}
            if self.in_card:
                self.depth += 1

        def handle_endtag(self, tag):
            if self.in_card:
                self.depth -= 1
                if self.depth <= 0:
                    self.in_card = False
                    if "time" in self.current:
                        tee_times.append(self.current)
                    self.current = {}

        def handle_data(self, data):
            if not self.in_card:
                return
            text = data.strip()
            if not text:
                return
            if re.match(r"\d{1,2}:\d{2}\s*(AM|PM)", text, re.IGNORECASE):
                self.current["time"] = text
            elif re.search(r"\$[\d.]+", text):
                self.current.setdefault("price", text)
            elif re.search(r"\d+\s*(player|spot|opening|available)", text, re.IGNORECASE):
                self.current.setdefault("spots", text)

    CardParser().feed(html)

    if not tee_times:
        found = re.findall(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", html, re.IGNORECASE)
        unique = list(dict.fromkeys(found))
        if unique:
            print(f"  Fallback regex found {len(unique)} time reference(s).")
            tee_times = [{"time": t} for t in unique]

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
            price = slot.get("price", "")
            spots = slot.get("spots", "")
            line  = f"  • {time}"
            if price: line += f"  |  {price}"
            if spots: line += f"  |  {spots}"
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
