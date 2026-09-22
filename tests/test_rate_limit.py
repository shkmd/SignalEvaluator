"""Unit tests for app.rate_limit -- the in-memory sliding-window limiter backing brute-force
protection on login/signup/change-password."""
from unittest.mock import patch

from app import rate_limit


def setup_function():
    # Each test gets a clean slate -- the module-level dict is shared global state.
    rate_limit._attempts.clear()


def test_allows_attempts_under_the_max():
    for _ in range(4):
        allowed, retry_after = rate_limit.check("k", max_attempts=5, window_seconds=900)
        assert allowed is True
        assert retry_after == 0
        rate_limit.record("k")


def test_blocks_once_max_attempts_reached():
    for _ in range(5):
        rate_limit.record("k")
    allowed, retry_after = rate_limit.check("k", max_attempts=5, window_seconds=900)
    assert allowed is False
    assert retry_after > 0


def test_different_keys_are_independent():
    for _ in range(5):
        rate_limit.record("attacker@example.com")
    allowed, _ = rate_limit.check("attacker@example.com", max_attempts=5, window_seconds=900)
    assert allowed is False

    allowed, _ = rate_limit.check("innocent@example.com", max_attempts=5, window_seconds=900)
    assert allowed is True


def test_reset_clears_recorded_attempts():
    for _ in range(5):
        rate_limit.record("k")
    rate_limit.reset("k")
    allowed, _ = rate_limit.check("k", max_attempts=5, window_seconds=900)
    assert allowed is True


def test_attempts_outside_the_window_are_ignored():
    with patch("app.rate_limit.time.time", return_value=1_000_000.0):
        for _ in range(5):
            rate_limit.record("k")
    # 20 minutes later, outside a 15-minute window
    with patch("app.rate_limit.time.time", return_value=1_000_000.0 + 20 * 60):
        allowed, retry_after = rate_limit.check("k", max_attempts=5, window_seconds=900)
    assert allowed is True
    assert retry_after == 0


def test_retry_after_shrinks_as_the_window_elapses():
    with patch("app.rate_limit.time.time", return_value=1_000_000.0):
        for _ in range(5):
            rate_limit.record("k")
        _, retry_after_immediately = rate_limit.check("k", max_attempts=5, window_seconds=900)

    with patch("app.rate_limit.time.time", return_value=1_000_000.0 + 600):  # 10 minutes later
        _, retry_after_later = rate_limit.check("k", max_attempts=5, window_seconds=900)

    assert retry_after_later < retry_after_immediately
