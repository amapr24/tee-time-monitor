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

# Email settings — set these as environment variables (see README)
SMTP_SERVER    = os.environ.get("SMTP_SERVER",   "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")    # e.g. yourname@gmail.com
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")  # Gmail App Password
EMAIL_TO       = os.environ.get("EMAIL_TO")        # who gets the alert

CACHE_FILE     = Path("last_teetimes.json")
# ─────────────────────────────────────────────────────────────────────────────


def next_friday() -> date:
    """Return the date of the coming Friday. If today is Friday, returns next Friday."""
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


async def select_friday_and_scrape(friday: date) -> list[dict]:
    """
    Open the booking page, navigate the inline calendar to the correct month,
    click Friday's date, then scrape and return available tee time slots.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"  Loading page...")
        await page.goto(URL, wait_until="networkidle", timeout=60_000)

        # ── Step 1: Navigate calendar to the correct month ───────────────────
        # The header shows e.g. "February 2026" in bold between two arrow buttons.
        # We click the right (›) arrow until we reach the target month.
        target_month_str = friday.strftime("%B %Y")  # e.g. "February 2026"

        for attempt in range(12):  # never advance more than 12 months
            # Read the current calendar header text
            try:
                header = await page.locator(
                    "[class*='monthYear'], [class*='month-year'], "
                    "[class*='calHeader'], [class*='cal-header'], "
                    "th[colspan] b, th[colspan] strong, "
                    ".month-header, [class*='calendarTitle']"
                ).first.text_content(timeout=5_000)
            except Exception:
                # Broad fallback: any bold text sitting inside a table header
                header = await page.locator("table th b, table th strong").first.text_content(timeout=5_000)

            header = (header or "").strip()
            print(f"  Calendar shows: '{header}'")

            if target_month_str in header:
                print(f"  ✓ Correct month reached.")
                break

            # Click the right-arrow next-month button
            # From the screenshot it appears as a › character inside a clickable element
            print(f"  Advancing month...")
            await page.locator(
                "[class*='rightArrow'], [class*='right-arrow'], "
                "[class*='nextMonth'], [class*='next-month'], "
                "button[aria-label*='next' i], "
                "span[class*='right' i]:not([class*='left']), "
                "a[class*='next' i], .fc-next-button, "
                # The › character itself as a last resort
                "th:last-child, td:last-child"
            ).first.click()
            await page.wait_for_timeout(600)
        else:
            print("  ⚠ Could not reach the target month after 12 attempts.")

        # ── Step 2: Click the correct day number ─────────────────────────────
        # The calendar renders as a standard HTML table. Each <td> contains a
        # day number. Past/unavailable days have a grey/disabled class.
        # We want the <td> whose exact text is our day number and is not greyed.
        day_num = str(friday.day)  # e.g. "20"
        print(f"  Looking for day cell: '{day_num}'...")

        clicked = False
        all_cells = page.locator("table td")
        count = await all_cells.count()

        for i in range(count):
            cell = all_cells.nth(i)
            text = (await cell.text_content()).strip()
            if text != day_num:
                continue
            classes = (await cell.get_attribute("class") or "").lower()
            # Skip greyed-out, disabled, or cells belonging to prev/next months
            if any(bad in classes for bad in ["gray", "grey", "disabled", "prev", "next", "old", "muted", "inactive"]):
                continue
            await cell.click()
            clicked = True
            print(f"  ✓ Clicked day {day_num} (cell classes: '{classes}')")
            break

        if not clicked:
            print(f"  ⚠ Day cell '{day_num}' not found — the calendar may use a different structure.")
            await browser.close()
            return []

        # ── Step 3: Wait for results to load ─────────────────────────────────
        await page.wait_for_timeout(3_000)

        try:
            await page.wait_for_selector(
                "[class*='teetime'], [class*='tee-time'], "
                "[class*='timeslot'], [class*='booking'], "
                ".no-results, [class*='noResult'], [class*='no-result']",
                timeout=10_000
            )
        except Exception:
            print("  ⚠ Result selector timed out — parsing whatever is rendered.")

        html = await page.content()
        await browser.close()

    return parse_tee_times(html)


def parse_tee_times(html: str) -> list[dict]:
    """
    Extract tee time slots from rendered page HTML.
    Attempts structured card parsing first, then falls back to a regex sweep.
    """
    import re
    from html.parser import HTMLParser

    tee_times = []

    class CardParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_card  = False
            self.depth    = 0
            self.current  = {}

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

    # Fallback: regex sweep for time patterns if no structured cards matched
    if not tee_times:
        found = re.findall(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", html, re.IGNORECASE)
        unique = list(dict.fromkeys(found))  # deduplicate, preserve order
        if unique:
            print(f"  Fallback regex found {len(unique)} time reference(s) in page.")
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
    print(f"  Found {len(current_slots)} tee time slot(s).")

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
