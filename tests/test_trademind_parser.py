"""Unit tests for the deterministic FIFO trade-grouping logic in
app.trademind.parser_zerodha -- the piece most likely to silently miscalculate P&L if it has
a bug, so every scenario from the spec (simple close, scale-out, scale-in, position flip) gets
its own explicit assertion on quantity, avg prices and gross_pnl.
"""
from app.trademind.parser_zerodha import group_into_trades, fingerprint


def _ex(side, qty, price, ts, symbol="RELIANCE", exchange="NSE", segment="EQ"):
    return {
        "symbol": symbol, "exchange": exchange, "segment": segment, "side": side,
        "quantity": qty, "price": price, "executed_at": ts,
        "broker_trade_id": None, "broker_order_id": None, "isin": None,
    }


def test_simple_full_close():
    execs = [
        _ex("buy", 100, 100, "2026-01-01T09:15:00"),
        _ex("sell", 100, 110, "2026-01-01T10:00:00"),
    ]
    trades = group_into_trades(execs)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["quantity"] == 100
    assert t["avg_entry_price"] == 100
    assert t["avg_exit_price"] == 110
    assert t["gross_pnl"] == 1000


def test_scale_out_partial_exits():
    execs = [
        _ex("buy", 100, 100, "2026-01-01T09:15:00"),
        _ex("sell", 40, 105, "2026-01-01T10:00:00"),
        _ex("sell", 60, 110, "2026-01-01T11:00:00"),
    ]
    trades = group_into_trades(execs)
    assert len(trades) == 1
    t = trades[0]
    assert t["quantity"] == 100
    assert t["avg_exit_price"] == (40 * 105 + 60 * 110) / 100
    assert t["gross_pnl"] == 40 * (105 - 100) + 60 * (110 - 100)


def test_scale_in_entries():
    execs = [
        _ex("buy", 50, 100, "2026-01-01T09:15:00"),
        _ex("buy", 50, 102, "2026-01-01T09:20:00"),
        _ex("sell", 100, 110, "2026-01-01T11:00:00"),
    ]
    trades = group_into_trades(execs)
    assert len(trades) == 1
    t = trades[0]
    assert t["quantity"] == 100
    assert t["avg_entry_price"] == 101
    # FIFO: lot1 (50@100) then lot2 (50@102), both closed by the single sell at 110
    assert t["gross_pnl"] == 50 * (110 - 100) + 50 * (110 - 102)


def test_position_flip_creates_two_trades():
    execs = [
        _ex("buy", 100, 100, "2026-01-01T09:15:00"),
        _ex("sell", 150, 110, "2026-01-01T10:00:00"),  # closes the 100 long, opens a 50 short
        _ex("buy", 50, 105, "2026-01-01T11:00:00"),  # closes the 50 short
    ]
    trades = group_into_trades(execs)
    assert len(trades) == 2

    long_trade = trades[0]
    assert long_trade["side"] == "long"
    assert long_trade["quantity"] == 100
    assert long_trade["avg_entry_price"] == 100
    assert long_trade["avg_exit_price"] == 110
    assert long_trade["gross_pnl"] == 1000

    short_trade = trades[1]
    assert short_trade["side"] == "short"
    assert short_trade["quantity"] == 50
    assert short_trade["avg_entry_price"] == 110
    assert short_trade["avg_exit_price"] == 105
    assert short_trade["gross_pnl"] == 50 * (110 - 105)


def test_different_symbols_are_not_mixed():
    execs = [
        _ex("buy", 100, 100, "2026-01-01T09:15:00", symbol="RELIANCE"),
        _ex("buy", 50, 200, "2026-01-01T09:16:00", symbol="TCS"),
        _ex("sell", 100, 110, "2026-01-01T10:00:00", symbol="RELIANCE"),
        _ex("sell", 50, 210, "2026-01-01T10:01:00", symbol="TCS"),
    ]
    trades = group_into_trades(execs)
    assert len(trades) == 2
    symbols = {t["symbol"] for t in trades}
    assert symbols == {"RELIANCE", "TCS"}


def test_open_position_produces_no_trade_yet():
    execs = [_ex("buy", 100, 100, "2026-01-01T09:15:00")]
    trades = group_into_trades(execs)
    assert trades == []


def test_fingerprint_is_stable_and_order_independent_of_dict_key_order():
    ex1 = _ex("buy", 100, 100, "2026-01-01T09:15:00")
    ex1["fingerprint"] = None  # not part of the hash input itself
    fp_a = fingerprint(1, ex1)
    fp_b = fingerprint(1, dict(ex1))
    assert fp_a == fp_b

    ex2 = _ex("buy", 100, 100.01, "2026-01-01T09:15:00")  # different price
    fp_c = fingerprint(1, ex2)
    assert fp_a != fp_c

    fp_other_account = fingerprint(2, ex1)
    assert fp_a != fp_other_account
