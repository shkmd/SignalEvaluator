"""Regression tests for parser.parse_signal() using real messages from the user's Telegram channels.

Real bug: a channel that posts "NIFTY 23200 CE / BUY ABOVE 130 / TGT 140/156/180+ / SL 110" was
parsed with symbol="ABOVE" (the word after BUY), no entry price, no targets and no strike/CE --
so every one of those calls appeared in History as an "ABOVE" signal. Also covers the app's own
broadcast format being re-ingested as a signal with symbol "RANGE"."""
from app.parser import parse_signal
from app.telegram_broadcast import format_signal_message, is_own_broadcast


def test_symbol_first_option_call_with_buy_above():
    p = parse_signal("BANKNIFTY 55700 CE \n\nBUY ABOVE 360\n\nTGT 380/420/460+\n\nSL 320")
    assert p["symbol"] == "BANKNIFTY"
    assert p["instrument"] == "CE"
    assert p["strike"] == 55700.0
    assert p["action"] == "buy"
    assert p["entry_low"] == p["entry_high"] == 360.0
    assert p["targets"] == [380.0, 420.0, 460.0]
    assert p["targets_open_ended"] is True
    assert p["sl"] == 320.0


def test_lowercase_message_and_pe():
    p = parse_signal("NIfty 23100 pe\n\nBUY above 145\n\nTGT 155/170/190+\n\nSL 130")
    assert (p["symbol"], p["instrument"], p["strike"]) == ("NIFTY", "PE", 23100.0)
    assert p["entry_low"] == 145.0
    assert p["targets"] == [155.0, 170.0, 190.0]


def test_above_is_never_taken_as_a_symbol():
    p = parse_signal("BUY ABOVE 130\nSL 110")
    assert p["symbol"] is None


def test_original_action_first_format_still_works():
    p = parse_signal("BUY RELIANCE 2900 CE\nAround 13-14\nSL 8\nTarget 16/18/20+")
    assert p["symbol"] == "RELIANCE"
    assert (p["strike"], p["instrument"]) == (2900.0, "CE")
    assert (p["entry_low"], p["entry_high"]) == (13.0, 14.0)
    assert p["sl"] == 8.0
    assert p["targets"] == [16.0, 18.0, 20.0]


def test_plain_equity_buy_still_works():
    p = parse_signal("SELL TATASTEEL\nSL 160\nTarget 140")
    assert (p["symbol"], p["action"], p["instrument"]) == ("TATASTEEL", "sell", "EQ")


def test_own_broadcast_message_is_recognised_so_it_is_not_reingested():
    signal = {
        "resolved_symbol": "MAXHEALTH", "symbol": "MAXHEALTH", "instrument": "EQ",
        "entry_low": 1012.6, "entry_high": 1012.6, "sl": 1052.0, "targets": [933.8],
        "signal_type": "positional", "action": "sell",
    }
    message = format_signal_message(signal, {"direction": "bearish", "score": 61.5, "verdict": "Moderate"})
    assert is_own_broadcast(message)


def test_a_normal_channel_message_is_not_mistaken_for_our_own():
    assert not is_own_broadcast("NIFTY 23200 CE\n\nBUY ABOVE 130\n\nTGT 140/156/180+\n\nSL 110")
    assert not is_own_broadcast("")
