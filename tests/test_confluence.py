"""Unit tests for app.confluence.check_and_trade() -- only trade when the F&O Scanner and a
Chartink scan both flag the same symbol/direction within the configured window. All DB/trading
calls are mocked; these tests are about the matching and dedup logic, not real persistence."""
from unittest.mock import patch

from app import confluence


def _scanner_row(**overrides):
    base = {
        "id": 100, "source": "scanner", "resolved_symbol": "RELIANCE.NS", "direction": "bullish",
        "instrument": "CE", "strike": 2900, "symbol": "RELIANCE", "signal_type": "positional",
        "entry_low": 50.0, "entry_high": 50.0, "sl": 45.0, "targets": [60.0], "lot_size": 250,
        "evaluation": {"score": 80, "direction": "bullish"},
    }
    base.update(overrides)
    return base


def _chartink_row(**overrides):
    base = {
        "id": 200, "source": "chartink", "resolved_symbol": "RELIANCE.NS", "direction": "bullish",
        "instrument": "EQ", "strike": None, "symbol": "RELIANCE", "signal_type": "positional",
        "entry_low": 2950.0, "entry_high": 2950.0, "sl": 2900.0, "targets": [3050.0], "lot_size": 1,
        "evaluation": {"score": 70, "direction": "bullish"},
    }
    base.update(overrides)
    return base


def test_does_nothing_when_disabled():
    with patch.object(confluence.db, "get_confluence_settings", return_value={"enabled": False}), \
         patch.object(confluence.db, "get_signal") as mock_get_signal:
        confluence.check_and_trade(1, 100)
    mock_get_signal.assert_not_called()


def test_ignores_signal_from_an_unrelated_source():
    settings = {"enabled": True, "window_minutes": 30, "quantity": 1}
    with patch.object(confluence.db, "get_confluence_settings", return_value=settings), \
         patch.object(confluence.db, "get_signal", return_value={"source": "telegram"}), \
         patch.object(confluence.db, "find_recent_signal") as mock_find:
        confluence.check_and_trade(1, 5)
    mock_find.assert_not_called()


def test_no_trade_when_no_matching_signal_from_the_other_source():
    settings = {"enabled": True, "window_minutes": 30, "quantity": 1}
    with patch.object(confluence.db, "get_confluence_settings", return_value=settings), \
         patch.object(confluence.db, "get_signal", return_value=_scanner_row()), \
         patch.object(confluence.db, "find_recent_signal", return_value=None), \
         patch.object(confluence.db, "insert_signal") as mock_insert, \
         patch.object(confluence.trading, "place_order_for_channel") as mock_place:
        confluence.check_and_trade(1, 100)
    mock_insert.assert_not_called()
    mock_place.assert_not_called()


def test_scanner_signal_arriving_second_matches_earlier_chartink_signal():
    settings = {"enabled": True, "window_minutes": 30, "quantity": 2}
    scanner_row = _scanner_row()
    with patch.object(confluence.db, "get_confluence_settings", return_value=settings), \
         patch.object(confluence.db, "get_signal", return_value=scanner_row), \
         patch.object(confluence.db, "find_recent_signal", return_value=_chartink_row()), \
         patch.object(confluence.db, "insert_signal", return_value=999) as mock_insert, \
         patch.object(confluence.trading, "place_order_for_channel") as mock_place:
        confluence.check_and_trade(1, scanner_row["id"])

    signal_arg = mock_insert.call_args[0][1]
    assert signal_arg["action"] == "buy"  # CE is always a buy regardless of direction
    assert signal_arg["instrument"] == "CE"
    assert mock_insert.call_args[1]["source"] == "confluence"
    assert mock_insert.call_args[1]["external_ref"] == "scanner:100"
    mock_place.assert_called_once()
    assert mock_place.call_args[0][1] == 999  # placed against the new confluence signal id
    assert mock_place.call_args[0][5] == 2  # quantity passed through


def test_chartink_signal_arriving_second_still_trades_off_the_earlier_scanner_signal():
    settings = {"enabled": True, "window_minutes": 30, "quantity": 1}
    chartink_row = _chartink_row()
    with patch.object(confluence.db, "get_confluence_settings", return_value=settings), \
         patch.object(confluence.db, "get_signal", return_value=chartink_row), \
         patch.object(confluence.db, "find_recent_signal", return_value=_scanner_row()), \
         patch.object(confluence.db, "insert_signal", return_value=999) as mock_insert, \
         patch.object(confluence.trading, "place_order_for_channel") as mock_place:
        confluence.check_and_trade(1, chartink_row["id"])

    # Even though the *chartink* signal triggered the check, the trade is built off the
    # scanner row's own strike/instrument/entry, never chartink's equity-only levels.
    signal_arg = mock_insert.call_args[0][1]
    assert signal_arg["instrument"] == "CE"
    assert signal_arg["strike"] == 2900
    mock_place.assert_called_once()


def test_bearish_equity_scanner_signal_derives_sell_action():
    settings = {"enabled": True, "window_minutes": 30, "quantity": 1}
    scanner_row = _scanner_row(id=101, instrument="EQ", strike=None, direction="bearish")
    with patch.object(confluence.db, "get_confluence_settings", return_value=settings), \
         patch.object(confluence.db, "get_signal", return_value=scanner_row), \
         patch.object(confluence.db, "find_recent_signal", return_value=_chartink_row(direction="bearish")), \
         patch.object(confluence.db, "insert_signal", return_value=999) as mock_insert, \
         patch.object(confluence.trading, "place_order_for_channel"):
        confluence.check_and_trade(1, scanner_row["id"])

    assert mock_insert.call_args[0][1]["action"] == "sell"


def test_repeat_match_on_the_same_scanner_signal_is_deduped():
    """insert_signal returning None means the unique (user, source, external_ref) index already
    has a row for this scanner signal -- e.g. Chartink re-fired the same scan a few minutes
    later. No second order should be placed."""
    settings = {"enabled": True, "window_minutes": 30, "quantity": 1}
    with patch.object(confluence.db, "get_confluence_settings", return_value=settings), \
         patch.object(confluence.db, "get_signal", return_value=_scanner_row()), \
         patch.object(confluence.db, "find_recent_signal", return_value=_chartink_row()), \
         patch.object(confluence.db, "insert_signal", return_value=None), \
         patch.object(confluence.trading, "place_order_for_channel") as mock_place:
        confluence.check_and_trade(1, 100)
    mock_place.assert_not_called()
