"""Process score (did you follow your own process, independent of whether the trade made
money) and the trading-rule compliance engine. Both are pure functions over plain dicts --
no DB access -- so the actual scoring math is unit-testable without a database.

Deliberately NOT computed for a trade until it has a journal entry: a process score without
journal answers would just be guessing, and section 15 of the spec is explicit that a
profitable trade must never be silently treated as "a good trade" -- scoring only what the
trader actually reported keeps that honest.
"""

PROCESS_SCORE_WEIGHTS = {
    "setup_followed": 25,
    "sl_followed": 25,
    "exit_plan_followed": 25,
    "journaled": 25,
}


def compute_process_score(journal: dict | None) -> float | None:
    """Returns None (not a 0) when there's no journal entry at all -- "not yet scored" is a
    different fact than "scored zero", and the UI must be able to tell them apart."""
    if journal is None:
        return None
    score = PROCESS_SCORE_WEIGHTS["journaled"]  # having journaled at all is itself worth points
    if journal.get("setup_followed"):
        score += PROCESS_SCORE_WEIGHTS["setup_followed"]
    if journal.get("sl_followed"):
        score += PROCESS_SCORE_WEIGHTS["sl_followed"]
    if journal.get("exit_plan_followed"):
        score += PROCESS_SCORE_WEIGHTS["exit_plan_followed"]
    return float(score)


def evaluate_trade_rules(trade: dict, rules: list) -> list:
    """Per-trade rules only (day-level rules like max_daily_loss/max_trades_per_day are
    evaluated separately by evaluate_day_rules(), since they need every trade on the day, not
    just one). Returns one evaluation dict per applicable rule; a rule that needs data this
    trade doesn't have (e.g. min_risk_reward with no planned_target) is skipped, not counted
    as either compliant or violated -- an unanswerable rule is not the same as a broken one."""
    results = []
    for rule in rules:
        if not rule.get("enabled"):
            continue
        rule_type = rule["rule_type"]
        threshold = rule["threshold_value"]

        if rule_type == "mandatory_stop_loss":
            if trade.get("planned_sl") is None:
                results.append(_eval(rule, False, "No stop-loss recorded for this trade.", trade_id=trade["id"]))
            else:
                results.append(_eval(rule, True, f"Stop-loss recorded at {trade['planned_sl']}.", trade_id=trade["id"]))

        elif rule_type == "min_risk_reward":
            planned_risk = trade.get("planned_risk")
            planned_target = trade.get("planned_target")
            entry = trade.get("avg_entry_price")
            if not (planned_risk and planned_target and entry):
                continue
            reward = abs(planned_target - entry)
            rr = reward / planned_risk if planned_risk else 0
            compliant = rr >= threshold
            results.append(_eval(
                rule, compliant, f"Planned R:R was {rr:.2f}, minimum required {threshold}.", trade_id=trade["id"]
            ))

    return results


def evaluate_day_rules(day_trades: list, rules: list) -> list:
    """day_trades: every trade whose exit_time falls on the same calendar day, already
    net_pnl-summed by the caller isn't required -- this function does the summing itself so
    it's the one place "what counts as today's loss" is defined."""
    results = []
    daily_net_pnl = sum(t["net_pnl"] for t in day_trades)
    trade_count = len(day_trades)

    for rule in rules:
        if not rule.get("enabled"):
            continue
        rule_type = rule["rule_type"]
        threshold = rule["threshold_value"]

        if rule_type == "max_daily_loss":
            compliant = daily_net_pnl >= -abs(threshold)
            detail = f"Day's net P&L was {daily_net_pnl:.2f}; max daily loss allowed is {threshold}."
            for t in day_trades:
                results.append(_eval(rule, compliant, detail, trade_id=t["id"]))

        elif rule_type == "max_trades_per_day":
            compliant = trade_count <= threshold
            detail = f"{trade_count} trades this day; max allowed is {int(threshold)}."
            for t in day_trades:
                results.append(_eval(rule, compliant, detail, trade_id=t["id"]))

    return results


def _eval(rule: dict, compliant: bool, detail: str, trade_id: int = None) -> dict:
    return {"rule_id": rule["id"], "rule_type": rule["rule_type"], "trade_id": trade_id,
            "compliant": compliant, "detail": detail}
