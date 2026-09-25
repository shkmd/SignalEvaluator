"""Unit tests for the pure parsing/signal-building parts of app.chartink -- no DB or network."""
from app.chartink import _parse_stocks, _build_signal


def test_parse_stocks_zips_symbols_with_trigger_prices():
    payload = {"stocks": "RELIANCE,TCS,INFY", "trigger_prices": "2950.50,4120.00,1875.25"}
    result = _parse_stocks(payload)
    assert result == [("RELIANCE", 2950.50), ("TCS", 4120.00), ("INFY", 1875.25)]


def test_parse_stocks_falls_back_to_none_prices_on_mismatched_lengths():
    payload = {"stocks": "RELIANCE,TCS", "trigger_prices": "2950.50"}
    result = _parse_stocks(payload)
    assert result == [("RELIANCE", None), ("TCS", None)]


def test_parse_stocks_handles_missing_trigger_prices():
    payload = {"stocks": "RELIANCE,TCS"}
    result = _parse_stocks(payload)
    assert result == [("RELIANCE", None), ("TCS", None)]


def test_parse_stocks_uppercases_and_strips_whitespace():
    payload = {"stocks": " reliance , tcs ", "trigger_prices": "100,200"}
    result = _parse_stocks(payload)
    assert result == [("RELIANCE", 100.0), ("TCS", 200.0)]


def test_parse_stocks_empty_payload_returns_empty_list():
    assert _parse_stocks({}) == []


def test_build_signal_bullish_uses_atr_for_sl_and_target():
    signal = _build_signal("RELIANCE", "RELIANCE", bullish=True, entry=1000.0, atr=10.0)
    assert signal["action"] == "buy"
    assert signal["instrument"] == "EQ"
    assert signal["entry_low"] == 1000.0
    assert signal["sl"] == 985.0  # 1000 - 1.5*10
    assert signal["targets"] == [1030.0]  # 1000 + 2*15


def test_build_signal_bearish_flips_sl_and_target_direction():
    signal = _build_signal("RELIANCE", "RELIANCE", bullish=False, entry=1000.0, atr=10.0)
    assert signal["action"] == "sell"
    assert signal["sl"] == 1015.0
    assert signal["targets"] == [970.0]


def test_build_signal_floors_risk_when_atr_missing_or_zero():
    signal = _build_signal("RELIANCE", "RELIANCE", bullish=True, entry=1000.0, atr=None)
    # MIN_RISK_PCT floor = 1000 * 0.005 = 5.0 risk
    assert signal["sl"] == 995.0
    assert signal["targets"] == [1010.0]


# ---- parse_body: tolerate whatever Content-Type the sender actually uses ----
from app.chartink import parse_body


def test_parse_body_json():
    raw = b'{"stocks":"RELIANCE,TCS","trigger_prices":"2950.5,4120","scan_name":"S"}'
    assert parse_body(raw) == {"stocks": "RELIANCE,TCS", "trigger_prices": "2950.5,4120", "scan_name": "S"}


def test_parse_body_form_encoded_fallback():
    raw = b"stocks=RELIANCE%2CTCS&trigger_prices=2950.5%2C4120&scan_name=My+Scan"
    assert parse_body(raw) == {"stocks": "RELIANCE,TCS", "trigger_prices": "2950.5,4120", "scan_name": "My Scan"}


def test_parse_body_coerces_non_string_json_values():
    assert parse_body(b'{"stocks":"TCS","trigger_prices":4120.5}')["trigger_prices"] == "4120.5"


def test_parse_body_empty_and_garbage_never_raise():
    assert parse_body(b"") == {}
    assert parse_body(b"   ") == {}
    assert parse_body(b"\xff\xfe not anything") == {}
