"""Regression test for trading.order_side() -- the fix for a real production bug where every
PE-instrument order got side="sell" (short/write) instead of "buy" (the actual intended
strategy: buy the put/call matching the directional view), because side was derived from
evaluation["direction"] (a market-thesis label that's "bearish" for every PE by definition)
instead of the signal's own already-correct action field. This silently inverted P&L for every
options trade -- confirmed against real production data (MARUTI/NESTLEIND PE trades)."""
from app.trading import order_side


def test_scanner_pe_signal_is_buy_not_sell():
    # scanner_signals._build_atm_signal always sets action="buy" for both CE and PE
    signal = {"instrument": "PE", "action": "buy"}
    evaluation = {"direction": "bearish"}  # PE is always "bearish" by scoring.py's own definition
    assert order_side(signal, evaluation) == "buy"


def test_scanner_ce_signal_is_buy():
    signal = {"instrument": "CE", "action": "buy"}
    evaluation = {"direction": "bullish"}
    assert order_side(signal, evaluation) == "buy"


def test_explicit_sell_action_is_respected():
    # a channel explicitly calling "SELL NIFTY 24000 PE" (writing premium) -- a real, distinct
    # intent that must not be overridden
    signal = {"instrument": "PE", "action": "sell"}
    evaluation = {"direction": "bearish"}
    assert order_side(signal, evaluation) == "sell"


def test_equity_signal_with_no_action_falls_back_to_direction():
    # a manually/Telegram-parsed equity signal with no BUY/SELL keyword in the text
    signal = {"instrument": "EQ", "action": None}
    assert order_side(signal, {"direction": "bearish"}) == "sell"
    assert order_side(signal, {"direction": "bullish"}) == "buy"


def test_missing_action_key_also_falls_back():
    signal = {"instrument": "EQ"}
    assert order_side(signal, {"direction": "bullish"}) == "buy"
