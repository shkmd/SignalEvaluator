"""Unit tests for place_paper_order()'s lot-size rounding on CE/PE signals -- the paper-trade
counterpart to _place_option_order()'s live-order rounding (tests/test_live_options.py). Without
this, a raw "Quantity per trade" setting (e.g. 1) gets recorded as "1 share of an option",
which isn't a real, tradable position and makes the paper P&L meaningless."""
from unittest.mock import patch

from app import trading


def _ce_signal(**overrides):
    base = {
        "resolved_symbol": "JINDALSTEL", "symbol": "JINDALSTEL", "instrument": "CE",
        "strike": 1180.0, "action": "buy", "entry_low": 12.35, "entry_high": 12.35,
        "sl": 10.0, "targets": [16.0],
    }
    base.update(overrides)
    return base


def _equity_signal(**overrides):
    base = {
        "resolved_symbol": "RELIANCE", "symbol": "RELIANCE", "instrument": "EQ",
        "strike": None, "action": "buy", "entry_low": 2450.0, "entry_high": 2450.0,
        "sl": 2400.0, "targets": [2550.0],
    }
    base.update(overrides)
    return base


def _place(signal, quantity_setting):
    evaluation = {"direction": "bullish", "score": 80}
    with patch.object(trading.db, "order_exists_for_signal", return_value=False), \
         patch.object(trading, "get_live_price", return_value={"available": True, "price": 12.0, "source": "fake"}), \
         patch.object(trading.db, "get_auto_trade_settings", return_value={"position_sizing_enabled": False, "default_trailing_enabled": False, "default_lock_enabled": False}), \
         patch.object(trading.db, "insert_order", return_value=1) as mock_insert:
        trading.place_paper_order(1, 10, signal, evaluation, {"quantity": quantity_setting})
    return mock_insert.call_args[0][1]["quantity"]


def test_general_auto_trade_quantity_of_one_rounds_up_to_a_full_lot():
    # signal carries its own lot_size (as F&O Scanner signals do)
    qty = _place(_ce_signal(lot_size=625), quantity_setting=1)
    assert qty == 625


def test_falls_back_to_db_lookup_when_signal_has_no_lot_size():
    # Telegram-parsed signals don't carry lot_size -- must resolve it from fo_universe instead
    with patch.object(trading.db, "get_lot_size", return_value=250):
        qty = _place(_ce_signal(lot_size=None), quantity_setting=1)
    assert qty == 250


def test_a_dedicated_path_already_passing_the_lot_size_is_not_doubled():
    # F&O Scanner's own paper-trade already passes base_quantity == lot_size
    qty = _place(_ce_signal(lot_size=625), quantity_setting=625)
    assert qty == 625


def test_quantity_rounds_up_not_down_for_a_partial_lot():
    qty = _place(_ce_signal(lot_size=625), quantity_setting=700)
    assert qty == 1250  # ceil(700/625) = 2 lots


def test_unknown_lot_size_defaults_to_one_share_unchanged():
    with patch.object(trading.db, "get_lot_size", return_value=None):
        qty = _place(_ce_signal(lot_size=None), quantity_setting=1)
    assert qty == 1


def test_equity_signals_are_never_lot_rounded():
    qty = _place(_equity_signal(), quantity_setting=1)
    assert qty == 1
