"""Unit tests for trading.apply_risk_management() -- the trailing-stop and profit-lock engine.
Pure function, no DB access, so every scenario is directly testable."""
from app.trading import apply_risk_management


def _order(**overrides):
    base = {
        "side": "buy", "entry_price": 100.0, "sl": 90.0, "peak_price": 100.0, "quantity": 10,
        "profit_locked": False, "trailing_enabled": False, "trail_pct": None,
        "lock_trigger_pct": None, "lock_pct": None,
        "lock_trigger_amount": None, "lock_amount": None,
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


# ---- rupee-amount profit lock ("if a position reached ₹1500 profit, lock it") ----

def test_amount_lock_triggers_once_total_profit_crosses_threshold():
    # entry 100, qty 10 -- +15 points = +₹150 total profit, which is below the 1500 trigger
    order = _order(quantity=10, lock_trigger_amount=1500, lock_amount=1500)
    result = apply_risk_management(order, 115.0)
    assert result["profit_locked"] is False
    assert result["sl"] == 90.0  # untouched

    # +160 points = +₹1600 total -- crosses ₹1500
    result = apply_risk_management(order, 260.0)
    assert result["profit_locked"] is True
    assert result["sl"] == 250.0  # entry 100 + 1500/qty(10) = 250 -- locks the full ₹1500


def test_amount_lock_blank_lock_amount_is_treated_as_none_not_zero():
    # lock_amount omitted entirely (None) -- caller (API/UI) is responsible for defaulting it to
    # the trigger amount; the engine itself must not fire with no lock amount to compute against.
    order = _order(quantity=10, lock_trigger_amount=1500, lock_amount=None)
    result = apply_risk_management(order, 260.0)
    assert result["profit_locked"] is False
    assert result["sl"] == 90.0


def test_amount_lock_on_short_side():
    order = _order(side="sell", entry_price=1000.0, sl=1100.0, peak_price=1000.0, quantity=10,
                    lock_trigger_amount=1500, lock_amount=1500)
    result = apply_risk_management(order, 950.0)  # (1000-950)*10 = ₹500 -- short of the ₹1500 trigger
    assert result["profit_locked"] is False

    result = apply_risk_management(order, 850.0)  # (1000-850)*10 = ₹1500 -- crosses it
    assert result["profit_locked"] is True
    assert result["sl"] == 850.0  # entry 1000 - 1500/qty(10) = 850 -- locks the full ₹1500


def test_pct_and_amount_triggers_combine_and_the_tighter_lock_wins():
    # pct lock proposes entry*1.02 = 102.0; amount lock (qty 10, ₹1500) proposes entry + 150 = 250.0
    order = _order(quantity=10, lock_trigger_pct=5, lock_pct=2, lock_trigger_amount=1500, lock_amount=1500)
    result = apply_risk_management(order, 260.0)  # clears both triggers
    assert result["profit_locked"] is True
    assert result["sl"] == 250.0  # the tighter of the two candidate locks


def test_amount_lock_needs_a_quantity():
    order = _order(quantity=None, lock_trigger_amount=1500, lock_amount=1500)
    result = apply_risk_management(order, 500.0)
    assert result["profit_locked"] is False
    assert result["sl"] == 90.0  # lock rule skipped (no quantity to size it) -- only the peak moved


def test_live_and_paper_orders_use_the_same_engine():
    # No mode-based branching inside apply_risk_management -- the function itself is mode-
    # agnostic; trading.monitor_open_positions() is what decides whether to actually act on it.
    order = _order(quantity=10, lock_trigger_amount=1500, lock_amount=1500, mode="live")
    result = apply_risk_management(order, 260.0)
    assert result["profit_locked"] is True and result["sl"] == 250.0
