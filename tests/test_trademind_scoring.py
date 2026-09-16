from app.trademind.scoring import compute_process_score, evaluate_trade_rules, evaluate_day_rules


def test_process_score_none_without_journal():
    assert compute_process_score(None) is None


def test_process_score_all_followed_is_100():
    journal = {"setup_followed": True, "sl_followed": True, "exit_plan_followed": True}
    assert compute_process_score(journal) == 100.0


def test_process_score_journaled_but_nothing_followed_is_25():
    journal = {"setup_followed": False, "sl_followed": False, "exit_plan_followed": False}
    assert compute_process_score(journal) == 25.0


def test_profitable_trade_can_have_low_process_score():
    # A trade that made money but broke every rule of the process
    journal = {"setup_followed": False, "sl_followed": False, "exit_plan_followed": False}
    score = compute_process_score(journal)
    assert score < 50  # low process score regardless of whether the trade was profitable


def test_mandatory_stop_loss_violation():
    trade = {"id": 1, "planned_sl": None}
    rules = [{"id": 10, "rule_type": "mandatory_stop_loss", "threshold_value": 0, "enabled": True}]
    results = evaluate_trade_rules(trade, rules)
    assert len(results) == 1
    assert results[0]["compliant"] is False
    assert results[0]["trade_id"] == 1


def test_mandatory_stop_loss_compliant():
    trade = {"id": 1, "planned_sl": 95}
    rules = [{"id": 10, "rule_type": "mandatory_stop_loss", "threshold_value": 0, "enabled": True}]
    results = evaluate_trade_rules(trade, rules)
    assert results[0]["compliant"] is True


def test_min_risk_reward_skipped_when_no_planned_data():
    trade = {"id": 1, "planned_risk": None, "planned_target": None, "avg_entry_price": 100}
    rules = [{"id": 11, "rule_type": "min_risk_reward", "threshold_value": 2, "enabled": True}]
    assert evaluate_trade_rules(trade, rules) == []


def test_min_risk_reward_violation():
    trade = {"id": 1, "planned_risk": 5, "planned_target": 108, "avg_entry_price": 100}
    rules = [{"id": 11, "rule_type": "min_risk_reward", "threshold_value": 2, "enabled": True}]
    results = evaluate_trade_rules(trade, rules)
    assert results[0]["compliant"] is False  # RR = 8/5 = 1.6, below the 2.0 minimum


def test_max_daily_loss_applies_to_every_trade_that_day():
    day_trades = [
        {"id": 1, "net_pnl": -3000},
        {"id": 2, "net_pnl": -2500},
    ]
    rules = [{"id": 20, "rule_type": "max_daily_loss", "threshold_value": 5000, "enabled": True}]
    results = evaluate_day_rules(day_trades, rules)
    assert len(results) == 2
    assert all(r["compliant"] is False for r in results)  # total loss 5500 > 5000 limit


def test_max_trades_per_day_compliant():
    day_trades = [{"id": 1, "net_pnl": 100}, {"id": 2, "net_pnl": -50}]
    rules = [{"id": 21, "rule_type": "max_trades_per_day", "threshold_value": 5, "enabled": True}]
    results = evaluate_day_rules(day_trades, rules)
    assert all(r["compliant"] is True for r in results)


def test_disabled_rule_is_skipped():
    trade = {"id": 1, "planned_sl": None}
    rules = [{"id": 10, "rule_type": "mandatory_stop_loss", "threshold_value": 0, "enabled": False}]
    assert evaluate_trade_rules(trade, rules) == []
