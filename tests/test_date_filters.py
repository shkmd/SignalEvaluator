"""Unit tests for db._ist_date_to_utc_bounds() -- the IST-calendar-day-to-UTC-range conversion
backing the History/Order Book date filters and the Dashboard's "today only" signal list.
IST is UTC+5:30 with no DST, so a naive string-date comparison against created_at (stored in
UTC) would be off by 5.5 hours at both ends of the day -- this is exactly the kind of bug that
would silently show yesterday's late-evening signals as "today" or hide today's early-morning
ones."""
from datetime import datetime, timezone

from app.db import _ist_date_to_utc_bounds


def test_bounds_span_exactly_one_ist_calendar_day():
    start_iso, end_iso = _ist_date_to_utc_bounds("2026-09-22")
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    assert (end - start).total_seconds() == 24 * 3600


def test_start_is_ist_midnight_converted_to_utc():
    # IST midnight (00:00 +05:30) on 2026-09-22 is 18:30 UTC on 2026-09-21
    start_iso, _ = _ist_date_to_utc_bounds("2026-09-22")
    start = datetime.fromisoformat(start_iso)
    assert start == datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc)


def test_end_is_next_ist_midnight_converted_to_utc():
    _, end_iso = _ist_date_to_utc_bounds("2026-09-22")
    end = datetime.fromisoformat(end_iso)
    assert end == datetime(2026, 9, 22, 18, 30, tzinfo=timezone.utc)


def test_a_timestamp_just_before_ist_midnight_falls_outside_the_day():
    # 2026-09-22 00:15 IST is only 15 minutes into the day -- but in UTC it's still
    # 2026-09-21 18:45, i.e. it would look like "yesterday" under a naive date-string compare.
    start_iso, end_iso = _ist_date_to_utc_bounds("2026-09-22")
    early_morning_ist_in_utc = "2026-09-21T18:45:00+00:00"
    assert start_iso <= early_morning_ist_in_utc < end_iso


def test_a_timestamp_late_at_night_ist_stays_within_the_same_day():
    # 2026-09-22 23:45 IST is 2026-09-22 18:15 UTC -- still the same IST trading day.
    start_iso, end_iso = _ist_date_to_utc_bounds("2026-09-22")
    late_night_ist_in_utc = "2026-09-22T18:15:00+00:00"
    assert start_iso <= late_night_ist_in_utc < end_iso


def test_a_timestamp_from_the_next_day_is_excluded():
    start_iso, end_iso = _ist_date_to_utc_bounds("2026-09-22")
    next_day_ist_in_utc = "2026-09-22T18:31:00+00:00"  # just past the next IST midnight
    assert not (start_iso <= next_day_ist_in_utc < end_iso)
