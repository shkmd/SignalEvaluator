"""CRUD for TradeMind's tables. Reuses app.db's connection helper (same WAL/busy_timeout
setup already tuned for concurrent access) but every function here is separate from the main
db module's own functions -- see schema.py for why the tables aren't merged."""
from datetime import datetime, timezone

from app.db import _conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Broker accounts ----

def create_broker_account(user_id: int, broker: str, nickname: str, masked_client_id: str,
                           account_purpose: str, starting_capital: float, classification: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO tm_broker_accounts
                (user_id, broker, nickname, masked_client_id, account_purpose, starting_capital,
                 current_capital, classification, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, broker, nickname, masked_client_id, account_purpose, starting_capital,
             starting_capital, classification, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_broker_accounts(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tm_broker_accounts WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_broker_account(user_id: int, account_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM tm_broker_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_broker_account_synced(account_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE tm_broker_accounts SET last_synced_at = ? WHERE id = ?", (_now(), account_id))
        conn.commit()
    finally:
        conn.close()


# ---- Import jobs ----

def create_import_job(user_id: int, broker_account_id: int, file_name: str, file_checksum: str,
                       parser_version: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO tm_import_jobs
                (user_id, broker_account_id, file_name, file_checksum, parser_version, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'processing', ?)
            """,
            (user_id, broker_account_id, file_name, file_checksum, parser_version, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def find_import_job_by_checksum(user_id: int, broker_account_id: int, file_checksum: str) -> dict:
    """A prior successful import of the exact same file (same bytes) is a no-op re-upload,
    not a new import -- caught before any row parsing happens."""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM tm_import_jobs
            WHERE user_id = ? AND broker_account_id = ? AND file_checksum = ? AND status = 'completed'
            """,
            (user_id, broker_account_id, file_checksum),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def finish_import_job(job_id: int, rows_processed: int, rows_rejected: int, duplicate_rows: int,
                       trades_created: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE tm_import_jobs
            SET status = 'completed', rows_processed = ?, rows_rejected = ?, duplicate_rows = ?,
                trades_created = ?, completed_at = ?
            WHERE id = ?
            """,
            (rows_processed, rows_rejected, duplicate_rows, trades_created, _now(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_import_job(job_id: int, error_message: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE tm_import_jobs SET status = 'failed', error_message = ?, completed_at = ? WHERE id = ?",
            (error_message, _now(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_import_jobs(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tm_import_jobs WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rollback_import_job(user_id: int, job_id: int) -> dict:
    """Deletes every trade and execution this import produced, and marks the job rolled_back.
    The executions/trades this job created are the only rows it touches -- other imports'
    data is untouched even if they share a broker account."""
    conn = _conn()
    try:
        job = conn.execute(
            "SELECT * FROM tm_import_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
        ).fetchone()
        if not job:
            return {"rolled_back": False, "reason": "Import job not found."}
        conn.execute("DELETE FROM tm_trade_journal WHERE trade_id IN "
                     "(SELECT id FROM tm_trades WHERE import_job_id = ? AND user_id = ?)", (job_id, user_id))
        conn.execute("DELETE FROM tm_rule_evaluations WHERE trade_id IN "
                     "(SELECT id FROM tm_trades WHERE import_job_id = ? AND user_id = ?)", (job_id, user_id))
        conn.execute("DELETE FROM tm_trades WHERE import_job_id = ? AND user_id = ?", (job_id, user_id))
        conn.execute("DELETE FROM tm_executions WHERE import_job_id = ? AND user_id = ?", (job_id, user_id))
        conn.execute("UPDATE tm_import_jobs SET status = 'rolled_back', completed_at = ? WHERE id = ?",
                      (_now(), job_id))
        conn.commit()
        return {"rolled_back": True}
    finally:
        conn.close()


# ---- Executions ----

def insert_executions(user_id: int, broker_account_id: int, import_job_id: int, executions: list) -> dict:
    """Skips (rather than errors on) any execution whose fingerprint already exists for this
    user -- that's the duplicate-import guard from the spec, enforced by the DB's own unique
    index so a race between two imports can't double-insert either."""
    conn = _conn()
    inserted_ids = []
    duplicate_count = 0
    try:
        for ex in executions:
            existing = conn.execute(
                "SELECT id FROM tm_executions WHERE user_id = ? AND fingerprint = ?",
                (user_id, ex["fingerprint"]),
            ).fetchone()
            if existing:
                duplicate_count += 1
                continue
            cur = conn.execute(
                """
                INSERT INTO tm_executions
                    (user_id, broker_account_id, import_job_id, symbol, isin, exchange, segment, side,
                     quantity, price, broker_trade_id, broker_order_id, executed_at, fingerprint, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, broker_account_id, import_job_id, ex["symbol"], ex.get("isin"), ex["exchange"],
                 ex["segment"], ex["side"], ex["quantity"], ex["price"], ex.get("broker_trade_id"),
                 ex.get("broker_order_id"), ex["executed_at"], ex["fingerprint"], _now()),
            )
            ex["id"] = cur.lastrowid
            inserted_ids.append(cur.lastrowid)
        conn.commit()
        return {"inserted": len(inserted_ids), "duplicates": duplicate_count}
    finally:
        conn.close()


def list_all_executions_for_account(user_id: int, broker_account_id: int) -> list:
    """Every execution ever imported for this account, not just this import batch -- trade
    grouping must be re-run against the full history so a trade that started in a previous
    import and finishes in this one groups correctly."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tm_executions WHERE user_id = ? AND broker_account_id = ? AND trade_id IS NULL "
            "ORDER BY executed_at",
            (user_id, broker_account_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def link_executions_to_trade(execution_ids: list, trade_id: int) -> None:
    if not execution_ids:
        return
    conn = _conn()
    try:
        conn.executemany(
            "UPDATE tm_executions SET trade_id = ? WHERE id = ?", [(trade_id, eid) for eid in execution_ids]
        )
        conn.commit()
    finally:
        conn.close()


# ---- Trades ----

def insert_trade(user_id: int, broker_account_id: int, import_job_id: int, trade: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO tm_trades
                (user_id, broker_account_id, import_job_id, symbol, isin, exchange, segment, side,
                 quantity, avg_entry_price, avg_exit_price, entry_time, exit_time, gross_pnl, charges,
                 net_pnl, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?)
            """,
            (user_id, broker_account_id, import_job_id, trade["symbol"], trade.get("isin"),
             trade["exchange"], trade["segment"], trade["side"], trade["quantity"],
             trade["avg_entry_price"], trade["avg_exit_price"], trade["entry_time"], trade["exit_time"],
             trade["gross_pnl"], trade["charges"], trade["net_pnl"], _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_trades(user_id: int, broker_account_id: int = None, limit: int = 500) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM tm_trades WHERE user_id = ?"
        params = [user_id]
        if broker_account_id:
            query += " AND broker_account_id = ?"
            params.append(broker_account_id)
        query += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trade(user_id: int, trade_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM tm_trades WHERE id = ? AND user_id = ?", (trade_id, user_id)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_trade_plan(user_id: int, trade_id: int, planned_sl: float, planned_target: float,
                       planned_risk: float, setup: str, strategy: str) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE tm_trades SET planned_sl = ?, planned_target = ?, planned_risk = ?, setup = ?, strategy = ?
            WHERE id = ? AND user_id = ?
            """,
            (planned_sl, planned_target, planned_risk, setup, strategy, trade_id, user_id),
        )
        conn.commit()
        return get_trade(user_id, trade_id)
    finally:
        conn.close()


def trades_on_same_day(user_id: int, exit_date: str) -> list:
    """exit_date: 'YYYY-MM-DD'. Used by the day-level rule engine (max_daily_loss,
    max_trades_per_day)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tm_trades WHERE user_id = ? AND date(exit_time) = ?", (user_id, exit_date)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- Journal ----

def upsert_journal(user_id: int, trade_id: int, journal: dict) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO tm_trade_journal
                (trade_id, user_id, setup_followed, sl_followed, exit_plan_followed, emotional_state,
                 mistakes, what_went_well, what_went_wrong, lesson_learned, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                setup_followed = excluded.setup_followed,
                sl_followed = excluded.sl_followed,
                exit_plan_followed = excluded.exit_plan_followed,
                emotional_state = excluded.emotional_state,
                mistakes = excluded.mistakes,
                what_went_well = excluded.what_went_well,
                what_went_wrong = excluded.what_went_wrong,
                lesson_learned = excluded.lesson_learned,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (trade_id, user_id, 1 if journal.get("setup_followed") else 0,
             1 if journal.get("sl_followed") else 0, 1 if journal.get("exit_plan_followed") else 0,
             journal.get("emotional_state"), journal.get("mistakes"), journal.get("what_went_well"),
             journal.get("what_went_wrong"), journal.get("lesson_learned"), journal.get("notes"), _now()),
        )
        conn.commit()
        return get_journal(user_id, trade_id)
    finally:
        conn.close()


def get_journal(user_id: int, trade_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM tm_trade_journal WHERE trade_id = ? AND user_id = ?", (trade_id, user_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---- Rules ----

def create_rule(user_id: int, rule_type: str, threshold_value: float) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO tm_rules (user_id, rule_type, threshold_value, created_at) VALUES (?, ?, ?, ?)",
            (user_id, rule_type, threshold_value, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_rules(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM tm_rules WHERE user_id = ? ORDER BY created_at", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_rule_enabled(user_id: int, rule_id: int, enabled: bool) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE tm_rules SET enabled = ? WHERE id = ? AND user_id = ?", (1 if enabled else 0, rule_id, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def delete_rule(user_id: int, rule_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM tm_rule_evaluations WHERE rule_id = ? AND user_id = ?", (rule_id, user_id))
        conn.execute("DELETE FROM tm_rules WHERE id = ? AND user_id = ?", (rule_id, user_id))
        conn.commit()
    finally:
        conn.close()


def save_rule_evaluations(user_id: int, trade_date: str, evaluations: list) -> None:
    """Replaces any prior evaluations for these exact (trade_id, rule_id) pairs -- rules can
    be re-evaluated (e.g. after the trade's plan is edited) without accumulating stale rows."""
    if not evaluations:
        return
    conn = _conn()
    try:
        for ev in evaluations:
            conn.execute(
                "DELETE FROM tm_rule_evaluations WHERE user_id = ? AND trade_id = ? AND rule_id = ?",
                (user_id, ev["trade_id"], ev["rule_id"]),
            )
            conn.execute(
                """
                INSERT INTO tm_rule_evaluations
                    (trade_id, user_id, rule_id, rule_type, trade_date, compliant, detail, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ev["trade_id"], user_id, ev["rule_id"], ev["rule_type"], trade_date,
                 1 if ev["compliant"] else 0, ev["detail"], _now()),
            )
        conn.commit()
    finally:
        conn.close()


def list_rule_evaluations_for_trade(user_id: int, trade_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tm_rule_evaluations WHERE user_id = ? AND trade_id = ?", (user_id, trade_id)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def compliance_summary(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total, SUM(CASE WHEN compliant = 1 THEN 1 ELSE 0 END) AS compliant
            FROM tm_rule_evaluations WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        total = row["total"] or 0
        compliant = row["compliant"] or 0
        return {
            "total_evaluations": total,
            "compliant": compliant,
            "compliance_pct": round(100 * compliant / total, 1) if total else None,
        }
    finally:
        conn.close()


# ---- Dashboard ----

def dashboard_summary(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_trades,
                   SUM(net_pnl) AS net_pnl,
                   SUM(gross_pnl) AS gross_pnl,
                   SUM(charges) AS total_charges,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM tm_trades WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        total_trades = row["total_trades"] or 0
        wins = row["wins"] or 0

        journaled = conn.execute(
            """
            SELECT j.setup_followed, j.sl_followed, j.exit_plan_followed
            FROM tm_trade_journal j JOIN tm_trades t ON t.id = j.trade_id
            WHERE j.user_id = ?
            """,
            (user_id,),
        ).fetchall()
        scores = []
        for j in journaled:
            score = 25
            if j["setup_followed"]:
                score += 25
            if j["sl_followed"]:
                score += 25
            if j["exit_plan_followed"]:
                score += 25
            scores.append(score)
        avg_process_score = round(sum(scores) / len(scores), 1) if scores else None

        return {
            "total_trades": total_trades,
            "net_pnl": round(row["net_pnl"], 2) if row["net_pnl"] is not None else 0,
            "gross_pnl": round(row["gross_pnl"], 2) if row["gross_pnl"] is not None else 0,
            "total_charges": round(row["total_charges"], 2) if row["total_charges"] is not None else 0,
            "win_rate": round(100 * wins / total_trades, 1) if total_trades else None,
            "avg_process_score": avg_process_score,
            "journaled_trades": len(scores),
        }
    finally:
        conn.close()
