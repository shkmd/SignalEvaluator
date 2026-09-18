"""Unit tests for trading.apply_risk_management() -- the trailing-stop and profit-lock engine.
Pure function, no DB access, so every scenario is directly testable."""
from app.trading import apply_risk_management


def _order(**overrides):
    base = {
        "side": "buy", "entry_price": 100.0, "sl": 90.0, "peak_price": 100.0,
        "profit_locked": False, "trailing_enabled": False, "trail_pct": None,
        "lock_trigger_pct": None, "lock_pct": None,
    }
    base.update(overrides)
    return base


def test_no_rules_configured_and_no_new_peak_means_no_change():
    order = _order()
    result = apply_risk_management(order, 95.0)  # below peak/entry -- no new high to record
    assert result["changed"] is False
    assert result["sl"] == 90.0


def test_peak_price_tracks_new_high_even_without_rules():
    order = _order()
    result = apply_risk_management(order, 110.0)
    assert result["peak_price"] == 110.0
    assert result["changed"] is True  # peak moved, even though sl itself didn't


def test_trailing_stop_follows_peak_on_long():
    order = _order(trailing_enabled=True, trail_pct=5)
    result = apply_risk_management(order, 120.0)
    assert result["peak_price"] == 120.0
    assert result["sl"] == 114.0  # 120 * 0.95
    assert result["changed"] is True


def test_trailing_stop_never_loosens_on_pullback():
    order = _order(sl=114.0, peak_price=120.0, trailing_enabled=True, trail_pct=5)
    result = apply_risk_management(order, 115.0)  # pulled back from the peak
    assert result["peak_price"] == 120.0  # peak unaffected by pullback
    assert result["sl"] == 114.0  # unchanged -- trailing sl from peak would be 115*0.95=109.25, worse


def test_trailing_stop_on_short_side():
    order = _order(side="sell", entry_price=100.0, sl=110.0, peak_price=100.0, trailing_enabled=True, trail_pct=5)
    result = apply_risk_management(order, 80.0)  # price fell -- favorable for a short
    assert result["peak_price"] == 80.0  # "peak" for a short is the lowest price seen
    assert result["sl"] == 84.0  # 80 * 1.05


def test_profit_lock_triggers_once_threshold_crossed():
    order = _order(lock_trigger_pct=5, lock_pct=2)
    result = apply_risk_management(order, 106.0)  # +6% -- crosses the 5% trigger
    assert result["profit_locked"] is True
    assert result["sl"] == 102.0  # entry * 1.02
    assert result["changed"] is True


def test_profit_lock_does_not_trigger_below_threshold():
    order = _order(lock_trigger_pct=5, lock_pct=2)
    result = apply_risk_management(order, 103.0)  # only +3%, below the 5% trigger
    assert result["profit_locked"] is False
    assert result["sl"] == 90.0  # untouched


def test_profit_lock_does_not_refire_once_already_locked():
    order = _order(sl=102.0, profit_locked=True, lock_trigger_pct=5, lock_pct=2)
    result = apply_risk_management(order, 108.0)
    assert result["sl"] == 102.0  # not re-evaluated/moved by the lock rule again


def test_profit_lock_never_loosens_an_already_better_sl():
    # trailing already moved SL past what the lock would set -- lock must not undo that
    order = _order(sl=105.0, peak_price=110.0, lock_trigger_pct=5, lock_pct=2)
    result = apply_risk_management(order, 106.0)
    assert result["profit_locked"] is True
    assert result["sl"] == 105.0  # lock's 102.0 is worse than the existing 105.0 -- kept as-is


def test_trailing_and_lock_combined_takes_the_better_sl():
    order = _order(trailing_enabled=True, trail_pct=10, lock_trigger_pct=5, lock_pct=2)
    result = apply_risk_management(order, 106.0)
    # lock proposes 102.0 (entry*1.02); trailing proposes 95.4 (peak 106 * 0.9) -- lock wins
    assert result["sl"] == 102.0
    assert result["profit_locked"] is True


def test_short_side_profit_lock():
    order = _order(side="sell", entry_price=100.0, sl=110.0, peak_price=100.0, lock_trigger_pct=5, lock_pct=2)
    result = apply_risk_management(order, 94.0)  # price fell 6% -- profitable for a short
    assert result["profit_locked"] is True
    assert result["sl"] == 98.0  # entry * 0.98


def test_missing_entry_price_is_a_safe_no_op():
    order = _order(entry_price=None, trailing_enabled=True, trail_pct=5)
    result = apply_risk_management(order, 120.0)
    assert result["changed"] is False
