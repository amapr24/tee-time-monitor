"""
Parser tests — exercise the pure-Python extraction logic without a browser.

Fixtures mimic the innerText Playwright's `page.evaluate` would return:
- chronogolf scraper returns a list of card innerText strings
- webtrac scraper returns a list of rows where each row is a list of td innerTexts

If a site DOM changes shape (new card wrapper, new table layout), these tests
won't catch it — but they will catch a parser regression where the text is
still recognizable but we stop extracting it correctly.
"""

from datetime import date

import tee_time_monitor
from tee_time_monitor import (
    parse_chronogolf,
    parse_chronogolf_card,
    parse_chronogolf_club_api,
    parse_webtrac,
    parse_webtrac_row,
)


# ── Chronogolf ────────────────────────────────────────────────────────────────

def test_chronogolf_card_12h():
    raw = "8:00 AM\n18 holes\n$55.00"
    assert parse_chronogolf_card(raw) == {
        "time":  "8:00 AM",
        "holes": "18 holes",
        "price": "$55.00",
    }


def test_chronogolf_card_24h_converts_to_12h():
    raw = "14:30  18 holes  $55"
    slot = parse_chronogolf_card(raw)
    assert slot["time"] == "2:30 PM"


def test_chronogolf_card_midnight_24h():
    raw = "00:15 tee time 18 holes"
    slot = parse_chronogolf_card(raw)
    assert slot["time"] == "12:15 AM"


def test_chronogolf_card_noon_24h():
    raw = "12:00 18 holes"
    slot = parse_chronogolf_card(raw)
    assert slot["time"] == "12:00 PM"


def test_chronogolf_card_too_short_or_no_time():
    assert parse_chronogolf_card("") is None
    assert parse_chronogolf_card("ab") is None
    assert parse_chronogolf_card("no time here") is None


def test_chronogolf_dedups():
    cards = [
        "8:00 AM 18 holes $55",
        "8:00 AM 18 holes $60",  # same time+holes — dup wins first
        "8:15 AM 18 holes $55",
    ]
    slots = parse_chronogolf(cards)
    assert len(slots) == 2
    assert slots[0]["price"] == "$55"


def test_chronogolf_body_fallback():
    slots = parse_chronogolf([], "9:00 AM or 10:30 am if you want")
    assert [s["time"] for s in slots] == ["9:00 AM", "10:30 AM"]


def test_chronogolf_no_tee_times_marker_suppresses_body_fallback():
    # The Chronogolf page shows a news/notice box even when the day is empty
    # (e.g. "The driving range will open at 9:00AM."). When the page also says
    # "No tee times found", we must not scrape times out of that notice.
    body = (
        "Book your round Miami Beach Golf Club "
        "News The driving range will open at 9:00AM. "
        "No tee times found Adjust your filters or select another date."
    )
    assert parse_chronogolf([], body) == []


def test_chronogolf_club_api_skips_full_slots():
    entries = [
        {"start_time": "10:22", "out_of_capacity": False, "green_fees": [{"price": 89.0}]},
        {"start_time": "06:30", "out_of_capacity": True, "green_fees": [{"price": 89.0}]},
    ]
    slots = parse_chronogolf_club_api(entries)
    assert len(slots) == 1
    assert slots[0]["time"] == "10:22 AM"
    assert slots[0]["holes"] == "18 holes"


def test_chronogolf_club_api_converts_afternoon_24h():
    entries = [{"start_time": "13:18", "out_of_capacity": False, "green_fees": [{"price": 79.0}]}]
    slots = parse_chronogolf_club_api(entries)
    assert slots[0]["time"] == "1:18 PM"


# ── WebTrac ───────────────────────────────────────────────────────────────────

def _webtrac_row(time, date, holes, course, open_slots, status="", cost=""):
    return ["", time, date, holes, course, str(open_slots), status, cost]


def test_webtrac_row_basic():
    row = _webtrac_row("7:00 am", "04/18/2026", "18 (Front)", "Plantation", 4, "", "$42.00")
    assert parse_webtrac_row(row) == {
        "time":  "7:00 AM",
        "holes": "18 (Front)",
        "price": "$42.00",
    }


def test_webtrac_row_skipped_when_zero_open():
    row = _webtrac_row("7:00 am", "04/18/2026", "18", "P", 0)
    assert parse_webtrac_row(row) is None


def test_webtrac_row_skipped_when_time_malformed():
    row = _webtrac_row("soon", "04/18/2026", "18", "P", 2)
    assert parse_webtrac_row(row) is None


def test_webtrac_row_too_few_cells():
    assert parse_webtrac_row(["", "7:00 am", "04/18/2026"]) is None


def test_webtrac_row_defaults_holes_when_blank():
    row = _webtrac_row("7:00 am", "04/18/2026", "", "P", 4)
    assert parse_webtrac_row(row)["holes"] == "18 Holes"


def test_webtrac_row_missing_cost_cell():
    # Table sometimes omits the trailing cost cell; parser must tolerate it.
    row = ["", "7:00 am", "04/18/2026", "18", "P", "4", ""]
    slot = parse_webtrac_row(row)
    assert slot["price"] == ""
    assert slot["time"] == "7:00 AM"


def test_webtrac_row_open_slots_with_trailing_text():
    # WebTrac renders the open-slots cell as "4 Open" or similar; grab leading int.
    row = ["", "7:00 am", "04/18/2026", "18", "P", "4 Open", "", "$42"]
    assert parse_webtrac_row(row)["time"] == "7:00 AM"
    # "3\nof\n4" has leading int 3, which is not 4 → filtered out
    row2 = ["", "7:15 am", "04/18/2026", "18", "P", "3\nof\n4", "", "$42"]
    assert parse_webtrac_row(row2) is None


def test_webtrac_parses_multiple_rows_filtering_non_four():
    # Only rows with exactly 4 open spaces are included.
    rows = [
        _webtrac_row("7:00 am", "04/18/2026", "18", "P", 4, "", "$42"),
        _webtrac_row("7:15 am", "04/18/2026", "18", "P", 0),
        _webtrac_row("7:30 am", "04/18/2026", "18", "P", 2, "", "$42"),
    ]
    slots = parse_webtrac(rows)
    assert [s["time"] for s in slots] == ["7:00 AM"]


# ── Booking URLs ──────────────────────────────────────────────────────────────

def test_chronogolf_book_url_marketplace_slug():
    # Marketplace-slug courses (no chronogolf_club_id) build a per-date deep link
    # onto the club URL with the teetimes step + holes/groupSize query params.
    course = {
        "url": "https://www.chronogolf.com/club/miami-lakes-golf-club",
        "holes": 18,
        "group_size": 4,
    }
    url = tee_time_monitor.chronogolf_book_url(course, date(2026, 6, 12))
    assert url == (
        "https://www.chronogolf.com/club/miami-lakes-golf-club"
        "?date=2026-06-12&step=teetimes&holes=18&coursesIds=&deals=false&groupSize=4"
    )


def test_course_book_url_chronogolf_marketplace_opens_full_calendar(monkeypatch):
    # The card-header link deep-links into today's full tee-sheet (step=teetimes)
    # with the holes/group-size filters left empty so every time shows.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    monkeypatch.setattr(tee_time_monitor, "_now_et",
                        lambda: datetime(2026, 7, 23, 9, 0, tzinfo=et))
    course = {"type": "chronogolf", "url": "https://www.chronogolf.com/club/miami-lakes-golf-club"}
    assert tee_time_monitor.course_book_url(course) == (
        "https://www.chronogolf.com/club/miami-lakes-golf-club"
        "?date=2026-07-23&step=teetimes&holes=&coursesIds=&deals=false&groupSize=0"
    )


def test_course_book_url_club_and_webtrac_use_configured_url():
    # Club-widget (has chronogolf_club_id) and WebTrac courses already point their
    # url at the booking interface — the header link uses it verbatim.
    club = {
        "type": "chronogolf",
        "chronogolf_club_id": 19871,
        "url": "https://www.chronogolf.com/club/19871/widget?medium=widget&source=club",
    }
    assert tee_time_monitor.course_book_url(club) == club["url"]
    webtrac = {"type": "webtrac", "url": "https://parks.example/webtrac/web/search.html?module=GR"}
    assert tee_time_monitor.course_book_url(webtrac) == webtrac["url"]


# ── Cache maintenance ─────────────────────────────────────────────────────────

def test_save_cache_prunes_past_dates(tmp_path):
    import json
    from datetime import timedelta

    cache_file = tmp_path / "cache_test.json"
    today = tee_time_monitor._now_et().date()
    stale = (today - timedelta(days=3)).isoformat()
    cache_file.write_text(json.dumps({stale: [{"time": "7:00 AM"}]}))

    target = today + timedelta(days=2)
    tee_time_monitor.save_cache(cache_file, target, [{"time": "8:00 AM"}])

    data = json.loads(cache_file.read_text())
    assert target.isoformat() in data
    assert stale not in data


# ── Sunset cutoff ─────────────────────────────────────────────────────────────

def test_sunset_cutoff_is_4h10m_before_sunset():
    from datetime import timedelta

    d = date(2026, 6, 12)
    sunset = tee_time_monitor.get_sunset(d)
    cutoff = tee_time_monitor.get_sunset_cutoff(d, 15)
    assert cutoff == sunset - timedelta(hours=4, minutes=10)


def test_sunset_cutoff_falls_back_when_sunset_unknown(monkeypatch):
    monkeypatch.setattr(tee_time_monitor, "get_sunset", lambda d: None)
    assert tee_time_monitor.get_sunset_cutoff(date(2026, 6, 12), 15) == 15
