"""
Debug version to see what's being scraped
"""

import asyncio
import json
from datetime import date, timedelta
from playwright.async_api import async_playwright

URL = "https://miamilakes.cps.golf/onlineresweb/search-teetime?TeeOffTimeMin=0&TeeOffTimeMax=18"

def next_friday() -> date:
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)

def deduplicate_slots(slots: list[dict]) -> list[dict]:
    """Remove duplicate tee time slots by time+holes combination."""
    seen = set()
    deduped = []
    for slot in slots:
        key = (slot.get("time", "").strip().upper(), slot.get("holes", "").strip().upper())
        if key not in seen:
            seen.add(key)
            deduped.append(slot)
    return deduped

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

        # EXTRACT WITH DETAILED DEBUG INFO
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

    return tee_times

async def main():
    friday = next_friday()
    print(f"\n🏌️  Miami Lakes Tee Time Monitor (DEBUG)")
    print(f"  Target date : {friday.strftime('%A, %B %-d, %Y')}\n")

    current_slots = await select_friday_and_scrape(friday)
    
    print(f"\n\n=== RAW SLOTS FROM SCRAPER ===")
    print(f"Total count: {len(current_slots)}")
    print(json.dumps(current_slots, indent=2))
    
    deduped = deduplicate_slots(current_slots)
    
    print(f"\n\n=== AFTER PYTHON DEDUPLICATION ===")
    print(f"Total count: {len(deduped)}")
    print(json.dumps(deduped, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
