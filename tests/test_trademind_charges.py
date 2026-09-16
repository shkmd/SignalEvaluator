from app.trademind.charges import calculate_charges


def test_delivery_has_no_brokerage():
    c = calculate_charges("EQ", "delivery", buy_value=100000, sell_value=105000)
    assert c["brokerage"] == 0
    assert c["stt"] > 0
    assert c["total"] > 0


def test_intraday_brokerage_is_capped():
    # Turnover large enough that 0.03% would exceed the ₹40 combined cap (2 orders x ₹20)
    c = calculate_charges("EQ", "intraday", buy_value=10_000_000, sell_value=10_000_000)
    assert c["brokerage"] == 40.0


def test_intraday_stt_is_sell_side_only():
    c = calculate_charges("EQ", "intraday", buy_value=100000, sell_value=110000)
    assert c["stt"] == round(110000 * 0.00025, 2)


def test_charges_are_deterministic_and_versioned():
    a = calculate_charges("EQ", "intraday", 50000, 51000)
    b = calculate_charges("EQ", "intraday", 50000, 51000)
    assert a == b
    assert a["version"]
