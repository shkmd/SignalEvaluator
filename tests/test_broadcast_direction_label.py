"""Regression tests for telegram_broadcast.format_signal_message().

Direction labeling: it used to hardcode "Buy" for every signal regardless of actual side, so a
genuine sell/short equity signal (SL above entry, target below -- e.g. a real production signal:
BAJFINANCE entry 999.5, SL 1015, target 968.5, direction="bearish") was broadcast labeled as a
buy, which is actively misleading, not just a display quirk. Must use the same order_side()
derivation as order placement -- not evaluation["direction"] directly, since a PE's direction is
always "bearish" by definition even though buying the put is this app's actual strategy.

Message format: a compact single-block style ("📈 Buy SYRMA 1640CE Around 56-58 Range") matching
what channels the user follows actually look like, deliberately without an "add more at X" /
averaging-down line even when the source signal carries one (signal.get("add_more_at")) -- this
app doesn't endorse averaging into a losing position."""
from app.telegram_broadcast import format_signal_message


def _equity_signal(**overrides):
    base = {
        "resolved_symbol": "BAJFINANCE", "symbol": "BAJFINANCE", "instrument": "EQ",
        "entry_low": 999.5, "entry_high": 999.5, "sl": 1015.0, "targets": [968.5],
        "signal_type": "positional", "action": None,
    }
    base.update(overrides)
    return base


def test_bearish_equity_signal_is_labeled_sell_not_buy():
    signal = _equity_signal()
    evaluation = {"direction": "bearish", "score": 72.6, "verdict": "Strong setup"}
    message = format_signal_message(signal, evaluation)
    assert "Sell BAJFINANCE Around 999.5" in message
    assert "Buy BAJFINANCE" not in message
    assert "\U0001F4C9" in message  # 📉
    assert "\U0001F4C8" not in message  # 📈


def test_bullish_equity_signal_is_labeled_buy():
    signal = _equity_signal(action="buy")
    evaluation = {"direction": "bullish", "score": 80, "verdict": "Strong setup"}
    message = format_signal_message(signal, evaluation)
    assert "Buy BAJFINANCE Around 999.5" in message
    assert "Sell BAJFINANCE" not in message
    assert "\U0001F4C8" in message  # 📈


def test_explicit_sell_action_is_respected_over_a_bullish_direction():
    signal = _equity_signal(action="sell")
    evaluation = {"direction": "bullish", "score": 60, "verdict": "Moderate"}
    message = format_signal_message(signal, evaluation)
    assert "Sell BAJFINANCE" in message


def test_send_test_message_payload_shape_does_not_crash_order_side():
    # Real bug: the "Send test message" button's dummy signal/evaluation had neither "action"
    # nor "direction" set, so format_signal_message() -> trading.order_side() raised
    # KeyError('direction') and the button failed with "Error: 'direction'" for every user.
    # Mirrors telegram_broadcast.send_test_message()'s actual test_signal/test_evaluation.
    test_signal = {
        "resolved_symbol": "TEST", "instrument": "EQ", "action": "buy",
        "entry_low": 100, "entry_high": 100, "sl": 95, "targets": [110],
        "signal_type": "positional",
    }
    test_evaluation = {"score": 100, "verdict": "Test message -- please ignore", "direction": "bullish"}
    message = format_signal_message(test_signal, test_evaluation)
    assert "Buy TEST Around 100" in message


def test_pe_option_signal_still_says_buy_despite_bearish_direction():
    # PE direction is always "bearish" by scoring.py's own definition, but this app always
    # *buys* the put -- must not be mislabeled "Sell" just because direction is bearish.
    signal = {
        "resolved_symbol": "RELIANCE", "symbol": "RELIANCE", "instrument": "PE", "strike": 2900,
        "entry_low": 55.0, "entry_high": 55.0, "sl": 45.0, "targets": [75.0],
        "signal_type": "positional", "action": "buy",
    }
    evaluation = {"direction": "bearish", "score": 85, "verdict": "Strong", "options": {}}
    message = format_signal_message(signal, evaluation)
    assert "Buy RELIANCE 2900PE Around 55" in message
    assert "Sell RELIANCE" not in message


def test_option_contract_line_matches_the_reference_layout():
    signal = {
        "resolved_symbol": "SYRMA", "symbol": "SYRMA", "instrument": "CE", "strike": 1640,
        "entry_low": 56.0, "entry_high": 58.0, "sl": 40.0, "targets": [64.0, 68.0, 75.0, 85.0, 100.0],
        "targets_open_ended": True, "signal_type": "intraday", "action": "buy",
    }
    evaluation = {"direction": "bullish", "score": 68.2, "verdict": "Moderate", "options": {}}
    message = format_signal_message(signal, evaluation)
    assert "INTRADAY" in message
    assert "Buy SYRMA 1640CE Around 56-58 Range" in message
    assert "SL 40" in message
    assert "Target 64/68/75/85/100+" in message


def test_single_price_entry_has_no_range_word():
    signal = _equity_signal(action="buy", entry_low=999.5, entry_high=999.5)
    evaluation = {"direction": "bullish", "score": 60, "verdict": "Moderate"}
    message = format_signal_message(signal, evaluation)
    assert "Around 999.5" in message
    assert "Range" not in message


def test_add_more_at_is_never_surfaced_even_when_the_signal_has_one():
    # e.g. a channel message parsed as "Add more at 11" (see parser.py's add_more_at field) --
    # this app doesn't endorse averaging into a losing position, so the broadcast must never
    # mention it even when the source signal carries that field.
    signal = _equity_signal(action="buy", add_more_at=950.0)
    evaluation = {"direction": "bullish", "score": 60, "verdict": "Moderate"}
    message = format_signal_message(signal, evaluation)
    assert "950" not in message
    assert "add more" not in message.lower()
    assert "dip" not in message.lower()
