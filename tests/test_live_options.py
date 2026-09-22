"""Unit tests for live options order placement -- app.trading._place_option_order(),
place_live_order()'s CE/PE branch, and _close_live_order()'s CE/PE branch.

These never touch a real broker: every broker call is a Mock. Per the explicit safety note in
app/brokers/kite.py's place_order() docstring, this codebase's own convention is that
broker.place_order() must never be invoked for real from automated tests -- these tests honor
that by mocking the broker object entirely."""
from unittest.mock import MagicMock, patch

from app import trading


def _fake_broker(name, **find_option_overrides):
    broker = MagicMock()
    broker.__name__ = f"app.brokers.{name}"
    contract = {
        "available": True, "source": name, "tradingsymbol": "RELIANCE24SEP2900CE",
        "expiry": "2026-09-25", "strike": 2900.0, "ltp": 55.0, "lot_size": 250,
    }
    if name == "upstox":
        contract["instrument_key"] = "NSE_FO|12345"
    if name == "dhan":
        contract["security_id"] = "999"
    contract.update(find_option_overrides)
    broker.find_option_by_strike.return_value = contract
    broker.place_order.return_value = "ORDER123"
    return broker, contract


# ---- _place_option_order ----

def test_places_kite_option_order_with_nfo_exchange():
    broker, contract = _fake_broker("kite")
    order_id, broker_name, resolved, qty = trading._place_option_order(
        broker, 1, "RELIANCE", 2900, "CE", "BUY", 1
    )
    assert order_id == "ORDER123"
    assert broker_name == "kite"
    assert resolved == contract
    broker.place_order.assert_called_once_with(
        1, tradingsymbol="RELIANCE24SEP2900CE", exchange="NFO",
        transaction_type="BUY", quantity=250, product="MIS", order_type="MARKET",
    )


def test_places_upstox_option_order_with_instrument_key():
    broker, _ = _fake_broker("upstox")
    trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 1)
    broker.place_order.assert_called_once_with(
        1, "NSE_FO|12345", transaction_type="BUY", quantity=250, product="I", order_type="MARKET",
    )


def test_places_dhan_option_order_with_security_id_and_segment():
    broker, _ = _fake_broker("dhan")
    trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 1)
    broker.place_order.assert_called_once_with(
        1, "999", "NSE_FNO", transaction_type="BUY", quantity=250, product_type="INTRADAY", order_type="MARKET",
    )


def test_strips_ns_suffix_before_looking_up_the_contract():
    broker, _ = _fake_broker("kite")
    trading._place_option_order(broker, 1, "RELIANCE.NS", 2900, "CE", "BUY", 1)
    broker.find_option_by_strike.assert_called_once_with(1, "RELIANCE", 2900, "CE")


def test_raises_when_contract_not_available():
    broker, _ = _fake_broker("kite", available=False, reason="No CE contracts found for RELIANCE.")
    try:
        trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 1)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "No CE contracts" in str(e)
    broker.place_order.assert_not_called()


def test_refuses_to_place_a_blind_order_when_no_live_quote():
    broker, _ = _fake_broker("kite", ltp=None)
    try:
        trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 1)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "No live quote" in str(e)
    broker.place_order.assert_not_called()


def test_quantity_rounds_up_to_a_full_lot_not_down():
    # 300 shares requested, lot size 250 -- must round UP to 2 lots (500), never down to 1 (250)
    broker, _ = _fake_broker("kite")
    _, _, _, qty = trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 300)
    assert qty == 500


def test_quantity_of_exactly_one_lot_is_not_bumped_to_two():
    broker, _ = _fake_broker("kite")
    _, _, _, qty = trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 250)
    assert qty == 250


def test_a_tiny_requested_quantity_still_gets_at_least_one_full_lot():
    broker, _ = _fake_broker("kite")
    _, _, _, qty = trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 1)
    assert qty == 250


def test_unknown_broker_name_is_refused():
    broker, _ = _fake_broker("someotherbroker")
    try:
        trading._place_option_order(broker, 1, "RELIANCE", 2900, "CE", "BUY", 1)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "No options order-placement wiring" in str(e)


# ---- place_live_order() CE/PE branch ----

def _ce_signal(**overrides):
    base = {
        "resolved_symbol": "RELIANCE", "symbol": "RELIANCE", "instrument": "CE", "strike": 2900.0,
        "action": "buy", "entry_low": 55.0, "entry_high": 55.0, "sl": 45.0, "targets": [75.0],
    }
    base.update(overrides)
    return base


def test_place_live_order_places_an_option_order_and_records_resolved_contract():
    broker, contract = _fake_broker("kite")
    settings = {"quantity": 1}
    with patch.object(trading.db, "order_exists_for_signal", return_value=False), \
         patch("app.brokers.get_connected_adapter", return_value=broker), \
         patch.object(trading.db, "daily_realized_pnl", return_value=0), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"position_sizing_enabled": False}), \
         patch.object(trading.db, "insert_order", return_value=55) as mock_insert:
        result = trading.place_live_order(1, 10, _ce_signal(), {"direction": "bullish", "score": 90}, settings)

    assert result["placed"] is True
    assert result["order_id"] == 55
    inserted = mock_insert.call_args[0][1]
    assert inserted["instrument"] == "CE"
    assert inserted["quantity"] == 250
    assert inserted["strike"] == 2900.0
    assert inserted["side"] == "buy"
    assert inserted["broker"] == "kite"


def test_place_live_order_refuses_when_signal_has_no_strike():
    settings = {"quantity": 1}
    with patch.object(trading.db, "order_exists_for_signal", return_value=False), \
         patch("app.brokers.get_connected_adapter", return_value=MagicMock()), \
         patch.object(trading.db, "daily_realized_pnl", return_value=0), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"position_sizing_enabled": False}):
        result = trading.place_live_order(1, 10, _ce_signal(strike=None), {"direction": "bullish", "score": 90}, settings)

    assert result["placed"] is False
    assert "no strike" in result["reason"].lower()


def test_place_live_order_surfaces_the_broker_failure_reason():
    broker, _ = _fake_broker("kite", available=False, reason="No CE contracts found.")
    settings = {"quantity": 1}
    with patch.object(trading.db, "order_exists_for_signal", return_value=False), \
         patch("app.brokers.get_connected_adapter", return_value=broker), \
         patch.object(trading.db, "daily_realized_pnl", return_value=0), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"position_sizing_enabled": False}):
        result = trading.place_live_order(1, 10, _ce_signal(), {"direction": "bullish", "score": 90}, settings)

    assert result["placed"] is False
    assert "No CE contracts found" in result["reason"]


def test_place_live_order_still_respects_the_one_order_per_signal_guard():
    with patch.object(trading.db, "order_exists_for_signal", return_value=True):
        result = trading.place_live_order(1, 10, _ce_signal(), {"direction": "bullish", "score": 90}, {"quantity": 1})
    assert result["placed"] is False
    assert "already exists" in result["reason"]


# ---- _close_live_order() CE/PE branch ----

def _open_ce_order(**overrides):
    base = {
        "id": 77, "user_id": 1, "instrument": "CE", "resolved_symbol": "RELIANCE",
        "strike": 2900.0, "side": "buy", "quantity": 250, "entry_price": 55.0,
    }
    base.update(overrides)
    return base


def test_close_live_order_uses_the_orders_own_strike_not_a_fresh_atm_pick():
    broker, contract = _fake_broker("kite", ltp=62.0)
    order = _open_ce_order()
    with patch("app.brokers.get_connected_adapter", return_value=broker), \
         patch.object(trading.db, "close_order") as mock_close:
        result = trading._close_live_order(1, order, "manual_close")

    broker.find_option_by_strike.assert_called_once_with(1, "RELIANCE", 2900.0, "CE")
    # closing a long -> the offsetting order must be a SELL
    broker.place_order.assert_called_once()
    assert broker.place_order.call_args[1]["transaction_type"] == "SELL"
    assert result["closed"] is True
    assert result["exit_price"] == 62.0
    mock_close.assert_called_once()


def test_close_live_order_reports_failure_without_marking_the_db_row_closed():
    broker, _ = _fake_broker("kite", available=False, reason="No CE contracts found.")
    order = _open_ce_order()
    with patch("app.brokers.get_connected_adapter", return_value=broker), \
         patch.object(trading.db, "close_order") as mock_close:
        result = trading._close_live_order(1, order, "manual_close")

    assert result["closed"] is False
    assert "still open" in result["reason"]
    mock_close.assert_not_called()
