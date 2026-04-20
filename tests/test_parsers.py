"""
Parser tests — exercise the pure-Python extraction logic without a browser.

Fixtures mimic the innerText Playwright's `page.evaluate` would return:
- cpsgolf / chronogolf scrapers return a list of card innerText strings
- webtrac scraper returns a list of rows where each row is a list of td innerTexts

If a site DOM changes shape (new card wrapper, new table layout), these tests
won't catch it — but they will catch a parser regression where the text is
still recognizable but we stop extracting it correctly.
"""

from tee_time_monitor import (
    parse_cpsgolf,
    parse_cpsgolf_card,
    parse_chronogolf,
    parse_chronogolf_card,
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


# ── WebTrac ───────────────────────────────────────────────────────────────────

def _webtrac_row(time, date, holes, course, open_slots, status="", cost=""):
    return ["", time, date, holes, course, str(open_slots), status, cost]


def test_webtrac_row_basic():
    row = _webtrac_row("7:00 am", "04/18/2026", "18 (Front)", "Plantation", 4, "", "$42.00")
    assert parse_webtrac_row(row) == {
        "time":  "7:00 am",
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
    row = _webtrac_row("7:00 am", "04/18/2026", "", "P", 2)
    assert parse_webtrac_row(row)["holes"] == "18 Holes"


def test_webtrac_row_missing_cost_cell():
    # Table sometimes omits the trailing cost cell; parser must tolerate it.
    row = ["", "7:00 am", "04/18/2026", "18", "P", "2", ""]
    slot = parse_webtrac_row(row)
    assert slot["price"] == ""
    assert slot["time"] == "7:00 am"


def test_webtrac_row_open_slots_with_trailing_text():
    # WebTrac sometimes renders open-slots cell with decorations/icons after
    # the number ("4 Open", "4\nof\n4"). parseInt-style leniency: grab leading int.
    row = ["", "7:00 am", "04/18/2026", "18", "P", "4 Open", "", "$42"]
    assert parse_webtrac_row(row)["time"] == "7:00 am"
    row2 = ["", "7:15 am", "04/18/2026", "18", "P", "3\nof\n4", "", "$42"]
    assert parse_webtrac_row(row2)["time"] == "7:15 am"


def test_webtrac_parses_multiple_rows_filtering_zero():
    rows = [
        _webtrac_row("7:00 am", "04/18/2026", "18", "P", 4, "", "$42"),
        _webtrac_row("7:15 am", "04/18/2026", "18", "P", 0),
        _webtrac_row("7:30 am", "04/18/2026", "18", "P", 2, "", "$42"),
    ]
    slots = parse_webtrac(rows)
    assert [s["time"] for s in slots] == ["7:00 am", "7:30 am"]
