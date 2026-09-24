"""Regression test for telegram_broadcast.format_signal_message() -- it used to hardcode
"Buy Range" for every signal regardless of actual side, so a genuine sell/short equity signal
(SL above entry, target below -- e.g. a real production signal: BAJFINANCE entry 999.5, SL
1015, target 968.5, direction="bearish") was broadcast labeled as a buy, which is actively
misleading, not just a display quirk. Must use the same order_side() derivation as order
placement -- not evaluation["direction"] directly, since a PE's direction is always "bearish"
by definition even though buying the put is this app's actual strategy."""
from app.telegram_broadcast import format_signal_message


def _equity_signal(**overrides):
    base = {
        "resolved_symbol": "BAJFINANCE", "symbol": "BAJFINANCE", "instrument": "EQ",
        "entry_low": 999.5, "entry_high": 999.5, "sl": 1015.0, "targets": [968.5],
        "signal_type": "positional", "action": None,
    }
    base.update(overrides)
    return base


def test_bearish_equity_signal_is_labeled_sell_range_not_buy():
    signal = _equity_signal()
    evaluation = {"direction": "bearish", "score": 72.6, "verdict": "Strong setup"}
    message = format_signal_message(signal, evaluation)
    assert "Sell Range - 999.5" in message
    assert "Buy Range" not in message


def test_bullish_equity_signal_is_labeled_buy_range():
    signal = _equity_signal(action="buy")
    evaluation = {"direction": "bullish", "score": 80, "verdict": "Strong setup"}
    message = format_signal_message(signal, evaluation)
    assert "Buy Range - 999.5" in message
    assert "Sell Range" not in message


def test_explicit_sell_action_is_respected_over_a_bullish_direction():
    signal = _equity_signal(action="sell")
    evaluation = {"direction": "bullish", "score": 60, "verdict": "Moderate"}
    message = format_signal_message(signal, evaluation)
    assert "Sell Range" in message


def test_pe_option_signal_still_says_buy_range_despite_bearish_direction():
    # PE direction is always "bearish" by scoring.py's own definition, but this app always
    # *buys* the put -- must not be mislabeled "Sell Range" just because direction is bearish.
    signal = {
        "resolved_symbol": "RELIANCE", "symbol": "RELIANCE", "instrument": "PE", "strike": 2900,
        "entry_low": 55.0, "entry_high": 55.0, "sl": 45.0, "targets": [75.0],
        "signal_type": "positional", "action": "buy",
    }
    evaluation = {"direction": "bearish", "score": 85, "verdict": "Strong", "options": {}}
    message = format_signal_message(signal, evaluation)
    assert "Buy Range - 55" in message
    assert "Sell Range" not in message
