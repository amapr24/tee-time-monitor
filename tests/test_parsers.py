"""
Parser tests — exercise the pure-Python extraction logic without a browser.

Fixtures mimic the innerText Playwright's `page.evaluate` would return:
- cpsgolf / chronogolf scrapers return a list of card innerText strings
- webtrac scraper returns a list of rows where each row is a list of td innerTexts

If a site DOM changes shape (new card wrapper, new table layout), these tests
won't catch it — but they will catch a parser regression where the text is
still recognizable but we stop extracting it correctly.
"""

import re
from datetime import date

import tee_time_monitor
from tee_time_monitor import (
    cpsgolf_book_url,
    parse_cpsgolf,
    parse_cpsgolf_card,
    parse_chronogolf,
    parse_chronogolf_card,
    parse_chronogolf_club_api,
    parse_webtrac,
    parse_webtrac_row,
)


# ── CPS Golf ──────────────────────────────────────────────────────────────────

def test_cpsgolf_card_basic():
    # Note: regex captures "18 HOLE" (no trailing S) — mirrors existing JS behavior.
    raw = "7:30 AM\n18 HOLES\n$65.00"
    assert parse_cpsgolf_card(raw) == {
        "time":  "7:30 AM",
        "holes": "18 HOLE",
        "price": "$65.00",
    }


def test_cpsgolf_card_pm_with_spaces_in_ampm():
    # CPS sometimes renders time with spaces between digits and A/P/M
    raw = "2:15 P M   9 HOLES   $40.00"
    slot = parse_cpsgolf_card(raw)
    assert slot["time"] == "2:15 PM"
    assert slot["holes"] == "9 HOLE"
    assert slot["price"] == "$40.00"


def test_cpsgolf_card_no_match():
    assert parse_cpsgolf_card("Book your tee time today!") is None
    assert parse_cpsgolf_card("") is None


def test_cpsgolf_dedups_by_time_and_holes():
    cards = [
        "7:30 AM 18 HOLES $65.00",
        "7:30 AM 18 HOLES $65.00",  # dup
        "7:30 AM 9 HOLES $40.00",   # different holes — keep
    ]
    slots = parse_cpsgolf(cards)
    assert len(slots) == 2
    assert {s["holes"] for s in slots} == {"18 HOLE", "9 HOLE"}


def test_cpsgolf_falls_back_to_body_text():
    body = "Available times include 8:00 AM and 10:30 AM and 2:15 PM for booking."
    slots = parse_cpsgolf([], body)
    times = [s["time"] for s in slots]
    assert times == ["8:00 AM", "10:30 AM", "2:15 PM"]


def test_cpsgolf_skips_fallback_when_cards_found():
    cards = ["7:30 AM 18 HOLES"]
    body = "Also 11:00 AM somewhere"
    slots = parse_cpsgolf(cards, body)
    assert len(slots) == 1
    assert slots[0]["time"] == "7:30 AM"


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

def test_cpsgolf_book_url_time_bounds_are_integer_hours():
    # Regression: the sunset-cutoff datetime used to be interpolated raw into
    # the URL ("...TeeOffTimeMax=2026-06-12 16:02:24.866244-04:00").
    course = {"url": "https://x.example/search-teetime", "tee_time_min": 6, "tee_time_max": 15}
    url = cpsgolf_book_url(course, date(2026, 6, 12))
    assert re.fullmatch(
        r"https://x\.example/search-teetime\?TeeOffTimeMin=6&TeeOffTimeMax=\d{1,2}", url
    )


def test_cpsgolf_book_url_falls_back_to_course_max(monkeypatch):
    # When the sunset calc fails, get_sunset_cutoff returns the fallback hour as-is.
    monkeypatch.setattr(tee_time_monitor, "get_sunset_cutoff", lambda d, fb: fb)
    course = {"url": "https://x.example/search-teetime", "tee_time_min": 6, "tee_time_max": 15}
    assert cpsgolf_book_url(course, date(2026, 6, 12)).endswith("&TeeOffTimeMax=15")


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
