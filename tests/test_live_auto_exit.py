"""Unit tests for monitor_open_positions()'s live-order branch -- specifically the new
live_auto_exit_enabled path (default OFF) that places a REAL offsetting order on SL/target hit
instead of only alerting. All broker/DB/alert calls are mocked."""
from unittest.mock import patch

from app import trading


def _live_order(**overrides):
    base = {
        "id": 88, "user_id": 1, "signal_id": 42, "resolved_symbol": "RELIANCE", "instrument": "EQ",
        "strike": None, "side": "buy", "quantity": 10, "entry_price": 2900.0, "sl": 2850.0,
        "target": 3000.0, "sl_alert_sent": 0,
    }
    base.update(overrides)
    return base


def test_auto_exit_disabled_by_default_falls_back_to_alert_only():
    order = _live_order()
    with patch.object(trading.db, "list_all_open_orders", side_effect=lambda mode: [order] if mode == "live" else []), \
         patch.object(trading, "get_live_price", return_value={"available": True, "price": 2800.0, "source": "fake"}), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"live_auto_exit_enabled": False}), \
         patch.object(trading, "_close_live_order") as mock_close, \
         patch.object(trading.alerts, "maybe_alert_sl_proximity", return_value=True) as mock_alert, \
         patch.object(trading.db, "mark_sl_alert_sent") as mock_mark:
        closed = trading.monitor_open_positions()

    mock_close.assert_not_called()  # no real order placed -- auto-exit is off
    mock_alert.assert_called_once()
    mock_mark.assert_called_once_with(88)
    assert closed == []


def test_auto_exit_enabled_places_a_real_order_and_records_the_close():
    order = _live_order()
    signal = {"channel": "F&O Scanner"}
    with patch.object(trading.db, "list_all_open_orders", side_effect=lambda mode: [order] if mode == "live" else []), \
         patch.object(trading, "get_live_price", return_value={"available": True, "price": 2800.0, "source": "fake"}), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"live_auto_exit_enabled": True}), \
         patch.object(trading, "_close_live_order", return_value={"closed": True, "exit_price": 2810.0, "pnl": -900.0}) as mock_close, \
         patch.object(trading.db, "update_outcome") as mock_outcome, \
         patch.object(trading.db, "get_signal", return_value=signal), \
         patch.object(trading.graduation, "check_and_graduate") as mock_graduate, \
         patch.object(trading.alerts, "alert_live_auto_exit") as mock_alert_ok, \
         patch.object(trading.alerts, "maybe_alert_sl_proximity") as mock_proximity:
        closed = trading.monitor_open_positions()

    mock_close.assert_called_once_with(1, order, "sl_hit")
    mock_outcome.assert_called_once_with(1, 42, "sl_hit")
    mock_graduate.assert_called_once_with(1, "F&O Scanner")
    mock_alert_ok.assert_called_once()
    mock_proximity.assert_not_called()  # real exit happened -- no need for a proximity nudge too
    assert closed == [{"order_id": 88, "reason": "sl_hit", "exit_price": 2810.0, "pnl": -900.0, "mode": "live"}]


def test_auto_exit_failure_alerts_urgently_and_leaves_the_order_open():
    order = _live_order()
    with patch.object(trading.db, "list_all_open_orders", side_effect=lambda mode: [order] if mode == "live" else []), \
         patch.object(trading, "get_live_price", return_value={"available": True, "price": 2800.0, "source": "fake"}), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"live_auto_exit_enabled": True}), \
         patch.object(trading, "_close_live_order", return_value={"closed": False, "reason": "Kite session expired."}), \
         patch.object(trading.db, "update_outcome") as mock_outcome, \
         patch.object(trading.alerts, "alert_live_auto_exit_failed") as mock_alert_fail, \
         patch.object(trading.alerts, "maybe_alert_sl_proximity", return_value=True) as mock_proximity, \
         patch.object(trading.db, "mark_sl_alert_sent"):
        closed = trading.monitor_open_positions()

    mock_alert_fail.assert_called_once()
    assert "Kite session expired" in mock_alert_fail.call_args[0][3]
    mock_outcome.assert_not_called()  # never mark an outcome for a position that's still open
    mock_proximity.assert_called_once()  # falls through to the routine alert as a backup
    assert closed == []


def test_auto_exit_survives_close_live_order_raising_an_exception():
    """_close_live_order can itself raise (e.g. a broker adapter bug) -- one order's failure
    must never take down monitoring for every other user's positions in the same cycle."""
    order = _live_order()
    with patch.object(trading.db, "list_all_open_orders", side_effect=lambda mode: [order] if mode == "live" else []), \
         patch.object(trading, "get_live_price", return_value={"available": True, "price": 2800.0, "source": "fake"}), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"live_auto_exit_enabled": True}), \
         patch.object(trading, "_close_live_order", side_effect=RuntimeError("boom")), \
         patch.object(trading.alerts, "alert_live_auto_exit_failed") as mock_alert_fail, \
         patch.object(trading.alerts, "maybe_alert_sl_proximity", return_value=False):
        closed = trading.monitor_open_positions()

    mock_alert_fail.assert_called_once()
    assert "boom" in mock_alert_fail.call_args[0][3]
    assert closed == []


def test_no_hit_never_touches_auto_exit_at_all():
    order = _live_order()
    with patch.object(trading.db, "list_all_open_orders", side_effect=lambda mode: [order] if mode == "live" else []), \
         patch.object(trading, "get_live_price", return_value={"available": True, "price": 2900.0, "source": "fake"}), \
         patch.object(trading.db, "get_auto_trade_settings") as mock_settings, \
         patch.object(trading, "_close_live_order") as mock_close, \
         patch.object(trading.alerts, "maybe_alert_sl_proximity", return_value=False):
        trading.monitor_open_positions()

    mock_settings.assert_not_called()
    mock_close.assert_not_called()
