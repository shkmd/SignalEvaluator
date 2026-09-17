"""Unit tests for app.market.is_market_hours_now() -- the gate the background auto-scan loop
uses to decide whether to run. Times are all IST; see app/market.py for the documented
limitation (no exchange holiday calendar)."""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import market

IST = ZoneInfo("Asia/Kolkata")


def _at(y, m, d, h, mi):
    with patch("app.market.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(y, m, d, h, mi, tzinfo=IST)
        return market.is_market_hours_now()


def test_mid_session_thursday_is_open():
    assert _at(2026, 9, 17, 10, 0) is True


def test_before_open_is_closed():
    assert _at(2026, 9, 17, 8, 0) is False


def test_after_close_is_closed():
    assert _at(2026, 9, 17, 16, 0) is False


def test_saturday_is_closed():
    assert _at(2026, 9, 19, 10, 0) is False


def test_sunday_is_closed():
    assert _at(2026, 9, 20, 10, 0) is False


def test_exact_open_boundary_is_open():
    assert _at(2026, 9, 17, 9, 15) is True


def test_exact_close_boundary_is_open():
    assert _at(2026, 9, 17, 15, 30) is True


def test_one_minute_before_open_is_closed():
    assert _at(2026, 9, 17, 9, 14) is False


def test_one_minute_after_close_is_closed():
    assert _at(2026, 9, 17, 15, 31) is False
