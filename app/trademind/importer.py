"""Orchestrates one import: parse -> dedupe-insert executions -> re-group that broker
account's whole (ungrouped) execution history into trades -> price each trade's charges ->
persist trades and link executions back to them -> evaluate rules -> record the job outcome.

This is the only place parser_zerodha, charges and the rule engine are wired together, so the
CSV-upload API route stays a thin wrapper around this function.
"""
import hashlib
from collections import defaultdict

from app.trademind import db as tm_db
from app.trademind.charges import calculate_charges
from app.trademind.parser_zerodha import PARSER_VERSION, ParseError, parse_csv, group_into_trades
from app.trademind.scoring import evaluate_day_rules


def run_import(user_id: int, broker_account_id: int, file_name: str, file_bytes: bytes) -> dict:
    checksum = hashlib.sha256(file_bytes).hexdigest()

    prior = tm_db.find_import_job_by_checksum(user_id, broker_account_id, checksum)
    if prior:
        return {"status": "duplicate_file", "import_job_id": prior["id"],
                "message": "This exact file was already imported successfully."}

    job_id = tm_db.create_import_job(user_id, broker_account_id, file_name, checksum, PARSER_VERSION)

    try:
        parsed = parse_csv(file_bytes, broker_account_id)
    except ParseError as e:
        tm_db.fail_import_job(job_id, str(e))
        return {"status": "failed", "import_job_id": job_id, "message": str(e)}

    insert_result = tm_db.insert_executions(user_id, broker_account_id, job_id, parsed["executions"])

    # Re-group the account's ENTIRE ungrouped execution history (not just this batch) so a
    # position that started in a previous import and finishes in this one groups correctly.
    ungrouped = tm_db.list_all_executions_for_account(user_id, broker_account_id)
    trades = group_into_trades(ungrouped)

    trades_created = 0
    for trade in trades:
        buy_value = sum(m["matched_quantity"] * m["execution"]["price"] for m in trade["entry_executions"])
        sell_value = sum(m["matched_quantity"] * m["execution"]["price"] for m in trade["exit_executions"])
        # No product-type column in the Tradebook export itself -- default to intraday when
        # entry and exit fall on the same calendar day (the common case for Zerodha MIS),
        # else delivery. This is a simplification noted in charges.py's own docstring.
        product = "intraday" if trade["entry_time"][:10] == trade["exit_time"][:10] else "delivery"
        charge_breakdown = calculate_charges(trade["segment"], product, buy_value, sell_value)

        trade_id = tm_db.insert_trade(user_id, broker_account_id, job_id, {
            **trade,
            "charges": charge_breakdown["total"],
            "net_pnl": round(trade["gross_pnl"] - charge_breakdown["total"], 2),
        })

        execution_ids = [m["execution"]["id"] for m in trade["entry_executions"] + trade["exit_executions"]]
        tm_db.link_executions_to_trade(execution_ids, trade_id)
        trades_created += 1

    tm_db.touch_broker_account_synced(broker_account_id)
    tm_db.finish_import_job(job_id, len(parsed["executions"]), len(parsed["rejected"]),
                             insert_result["duplicates"], trades_created)

    evaluate_rules_for_recent_trades(user_id)

    return {
        "status": "completed",
        "import_job_id": job_id,
        "rows_processed": len(parsed["executions"]),
        "rows_rejected": len(parsed["rejected"]),
        "rejected_rows": parsed["rejected"],
        "duplicate_rows": insert_result["duplicates"],
        "trades_created": trades_created,
    }


def evaluate_rules_for_recent_trades(user_id: int) -> None:
    """Re-runs the day-level rules (max_daily_loss, max_trades_per_day) for every exit-date
    touched by trades belonging to this user. Cheap for a single-user import batch; would need
    scoping to just the affected dates for a much larger account, which isn't this slice's
    concern yet."""
    trades = tm_db.list_trades(user_id, limit=5000)
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["exit_time"][:10]].append(t)

    rules = tm_db.list_rules(user_id)
    day_rules = [r for r in rules if r["rule_type"] in ("max_daily_loss", "max_trades_per_day")]
    if not day_rules:
        return

    for day, day_trades in by_day.items():
        evaluations = evaluate_day_rules(day_trades, day_rules)
        tm_db.save_rule_evaluations(user_id, day, evaluations)
