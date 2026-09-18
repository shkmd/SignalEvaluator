"""Regression test for the one-signal-at-most-one-order guard in trading.place_paper_order()/
place_live_order() -- without it, a signal that qualifies for more than one independently
enabled auto-trade path (general Broker-Setup auto-trade, F&O Scanner's own dedicated
paper-trade, a Telegram channel's dedicated paper-trade) gets traded once per path instead of
once, producing duplicate positions for the same contract."""
from unittest.mock import patch

from app import trading


def test_second_paper_order_for_same_signal_is_skipped():
    signal = {"resolved_symbol": "RELIANCE.NS", "instrument": "EQ", "strike": None, "sl": 2350}
    evaluation = {"direction": "bullish", "score": 85}

    with patch.object(trading, "get_live_price", return_value={"available": True, "price": 2410, "source": "fake"}), \
         patch.object(trading.db, "order_exists_for_signal", side_effect=[False, True]), \
         patch.object(trading.db, "insert_order", return_value=1) as mock_insert:
        first = trading.place_paper_order(1, 42, signal, evaluation, {"quantity": 1})
        second = trading.place_paper_order(1, 42, signal, evaluation, {"quantity": 50})

    assert first["placed"] is True
    assert second["placed"] is False
    assert "already exists" in second["reason"]
    mock_insert.assert_called_once()  # the DB write only happened for the first attempt


def test_live_order_also_respects_the_guard():
    signal = {"resolved_symbol": "RELIANCE.NS", "instrument": "EQ", "strike": None, "sl": 2350}
    evaluation = {"direction": "bullish", "score": 85}

    with patch.object(trading.db, "order_exists_for_signal", return_value=True):
        result = trading.place_live_order(1, 42, signal, evaluation, {"quantity": 1})

    assert result["placed"] is False
    assert "already exists" in result["reason"]
