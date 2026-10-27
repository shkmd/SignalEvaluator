"""Unit tests for trading.get_live_price()'s short-lived cache and the "skip NSE when no broker
is connected" fast-fail path -- both added to cut the ~3s (up to ~17s worst-case) lag Positions
was seeing on every 10s poll. See app/trading.py's PRICE_CACHE_TTL_SECONDS/_fetch_live_price."""
import time
from unittest.mock import patch

import pytest

from app import trading


@pytest.fixture(autouse=True)
def _clear_price_cache():
    # The cache is a module-level dict (deliberately, so it's shared across requests/threads) --
    # without clearing it, one test's cached entry would leak into the next.
    trading._price_cache.clear()
    yield
    trading._price_cache.clear()


# ---- caching ----

def test_second_call_within_ttl_is_served_from_cache():
    with patch.object(trading, "_fetch_live_price", return_value={"available": True, "price": 100.0, "source": "yfinance"}) as mock_fetch:
        trading.get_live_price("RELIANCE", "EQ", None)
        trading.get_live_price("RELIANCE", "EQ", None)
    mock_fetch.assert_called_once()


def test_call_after_ttl_expires_fetches_again():
    # Prime the cache directly with an entry older than the TTL, rather than guessing how many
    # times get_live_price() itself calls time.monotonic() internally.
    stale_ts = time.monotonic() - (trading.PRICE_CACHE_TTL_SECONDS + 1)
    trading._price_cache[("RELIANCE", "EQ", None)] = (stale_ts, {"available": True, "price": 90.0, "source": "yfinance"})
    with patch.object(trading, "_fetch_live_price", return_value={"available": True, "price": 100.0, "source": "yfinance"}) as mock_fetch:
        result = trading.get_live_price("RELIANCE", "EQ", None)
    mock_fetch.assert_called_once()
    assert result["price"] == 100.0


def test_different_contracts_are_cached_separately():
    with patch.object(trading, "_fetch_live_price", return_value={"available": True, "price": 100.0, "source": "yfinance"}) as mock_fetch:
        trading.get_live_price("RELIANCE", "EQ", None)
        trading.get_live_price("TCS", "EQ", None)
        trading.get_live_price("RELIANCE", "CE", 2900.0)
    assert mock_fetch.call_count == 3


def test_cache_key_excludes_user_id_so_two_users_share_one_fetch():
    with patch.object(trading, "_fetch_live_price", return_value={"available": True, "price": 100.0, "source": "yfinance"}) as mock_fetch:
        trading.get_live_price("RELIANCE", "EQ", None, user_id=1)
        trading.get_live_price("RELIANCE", "EQ", None, user_id=2)
    mock_fetch.assert_called_once()


def test_a_failed_lookup_is_also_cached():
    with patch.object(trading, "_fetch_live_price", return_value={"available": False, "reason": "boom"}) as mock_fetch:
        r1 = trading.get_live_price("RELIANCE", "EQ", None)
        r2 = trading.get_live_price("RELIANCE", "EQ", None)
    mock_fetch.assert_called_once()
    assert r1 == r2 == {"available": False, "reason": "boom"}


# ---- skipping the slow/blocked NSE fallback when no broker is connected ----

def test_no_broker_at_all_skips_nse_and_fails_fast():
    with patch("app.brokers.get_connected_adapter", return_value=None), \
         patch.object(trading.options_mod, "fetch_option_chain_snapshot") as mock_nse:
        result = trading._fetch_live_price("RELIANCE", "CE", 2900.0, user_id=1)
    mock_nse.assert_not_called()
    assert result["available"] is False
    assert "no broker" in result["reason"].lower()


def test_no_user_id_also_skips_nse():
    # No user_id at all means there's no session to even check -- same fast-fail as "not connected".
    with patch.object(trading.options_mod, "fetch_option_chain_snapshot") as mock_nse:
        result = trading._fetch_live_price("RELIANCE", "CE", 2900.0, user_id=None)
    mock_nse.assert_not_called()
    assert result["available"] is False


def test_broker_connected_and_succeeds_short_circuits_before_nse():
    # Assigned directly to the instance (not via the class body) so it stays a plain callable
    # instead of becoming a bound method that would swallow an extra "self" argument.
    fake_broker = type("FakeBroker", (), {})()
    fake_broker.find_option_by_strike = lambda user_id, name, strike, instrument: {"available": True, "ltp": 123.45, "source": "kite"}
    with patch("app.brokers.get_connected_adapter", return_value=fake_broker), \
         patch.object(trading.options_mod, "fetch_option_chain_snapshot") as mock_nse:
        result = trading._fetch_live_price("RELIANCE", "CE", 2900.0, user_id=1)
    mock_nse.assert_not_called()
    assert result == {"available": True, "price": 123.45, "source": "kite"}


def test_broker_connected_but_this_lookup_fails_still_falls_through_to_nse():
    fake_broker = type("FakeBroker", (), {})()
    fake_broker.find_option_by_strike = lambda user_id, name, strike, instrument: {"available": False, "reason": "not found"}
    with patch("app.brokers.get_connected_adapter", return_value=fake_broker), \
         patch.object(trading.options_mod, "fetch_option_chain_snapshot",
                      return_value={"available": True, "ltp": 50.0}) as mock_nse:
        result = trading._fetch_live_price("RELIANCE", "CE", 2900.0, user_id=1)
    mock_nse.assert_called_once()
    assert result == {"available": True, "price": 50.0, "source": "nse_option_chain"}


def test_broker_without_find_option_by_strike_still_counts_as_connected():
    # e.g. an adapter that doesn't implement options LTP lookups yet -- it's still a real
    # connection, so NSE stays a reasonable fallback rather than being skipped outright.
    fake_broker = object()
    with patch("app.brokers.get_connected_adapter", return_value=fake_broker), \
         patch.object(trading.options_mod, "fetch_option_chain_snapshot",
                      return_value={"available": True, "ltp": 50.0}) as mock_nse:
        result = trading._fetch_live_price("RELIANCE", "CE", 2900.0, user_id=1)
    mock_nse.assert_called_once()
    assert result["available"] is True


def test_equity_lookup_is_unaffected_by_the_broker_check():
    fake_fast_info = {"last_price": 1234.5}
    with patch.object(trading.yf, "Ticker") as mock_ticker:
        mock_ticker.return_value.fast_info = fake_fast_info
        result = trading._fetch_live_price("RELIANCE", "EQ", None, user_id=None)
    assert result == {"available": True, "price": 1234.5, "source": "yfinance"}
