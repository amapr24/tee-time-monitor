"""Monitor helpers: booking window, 18-hole filter, past-slot cutoff."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import tee_time_monitor as tm

ET = ZoneInfo("America/New_York")


def test_slot_is_18_holes_empty_means_default_18():
    assert tm.slot_is_18_holes({"time": "8:00 AM", "holes": ""}) is True


def test_slot_is_18_holes_explicit_18():
    assert tm.slot_is_18_holes({"holes": "18 holes"}) is True
    assert tm.slot_is_18_holes({"holes": "18 HOLE"}) is True
    assert tm.slot_is_18_holes({"holes": "18 (Front)"}) is True


def test_slot_is_18_holes_rejects_9():
    assert tm.slot_is_18_holes({"holes": "9 HOLE"}) is False
    assert tm.slot_is_18_holes({"holes": "9 holes"}) is False


def test_normalize_monitor_slots_strips_price_and_9_hole():
    slots = [
        {"time": "7:00 AM", "holes": "18 holes", "price": "$55"},
        {"time": "7:09 AM", "holes": "9 holes", "price": "$30"},
    ]
    out = tm.normalize_monitor_slots(slots)
    assert len(out) == 1
    assert out[0]["time"] == "7:00 AM"
    assert "price" not in out[0]


def test_is_slot_in_past_today_uses_clock_before_now(monkeypatch):
    """On the scrape day, hide times strictly before 'now'; keep the current clock minute."""
    fixed = datetime(2026, 5, 15, 9, 33, 45, tzinfo=ET)  # Friday 9:33:45
    monkeypatch.setattr(tm, "_now_et", lambda: fixed)
    d = date(2026, 5, 15)
    assert tm.is_slot_in_past("9:32 AM", d) is True
    assert tm.is_slot_in_past("9:33 AM", d) is False
    assert tm.is_slot_in_past("10:00 AM", d) is False


def test_is_slot_in_past_other_day_always_false(monkeypatch):
    monkeypatch.setattr(tm, "_now_et", lambda: datetime(2026, 5, 15, 12, 0, tzinfo=ET))
    assert tm.is_slot_in_past("6:00 AM", date(2099, 1, 2)) is False


def test_is_within_booking_window(monkeypatch):
    anchor = datetime(2026, 5, 14, 12, 0, tzinfo=ET)
    monkeypatch.setattr(tm, "_now_et", lambda: anchor)
    course = {"booking_window_days": 3}
    assert tm.is_within_booking_window(course, date(2026, 5, 17)) is True
    assert tm.is_within_booking_window(course, date(2026, 5, 18)) is False


def test_visible_dates_for_course_no_window_returns_all():
    dlist = [date(2026, 5, 15), date(2026, 5, 16)]
    assert tm.visible_dates_for_course({}, dlist) == dlist
