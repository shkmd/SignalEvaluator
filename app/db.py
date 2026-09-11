import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    verification_token TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS telegram_credentials (
    user_id INTEGER PRIMARY KEY,
    api_id TEXT,
    api_hash TEXT,
    session_name TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'unknown',
    raw_text TEXT,
    symbol TEXT,
    resolved_symbol TEXT,
    instrument TEXT,
    strike REAL,
    signal_type TEXT,
    entry_low REAL,
    entry_high REAL,
    sl REAL,
    targets TEXT,
    score REAL,
    verdict TEXT,
    direction TEXT,
    red_flags TEXT,
    evaluation_json TEXT,
    outcome TEXT NOT NULL DEFAULT 'pending',
    outcome_note TEXT,
    outcome_updated_at TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    telegram_chat_id INTEGER,
    telegram_message_id INTEGER,
    lot_size INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_telegram_msg
    ON signals(user_id, telegram_chat_id, telegram_message_id)
    WHERE telegram_chat_id IS NOT NULL AND telegram_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS monitored_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    title TEXT,
    username TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    UNIQUE(user_id, telegram_chat_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    signal_id INTEGER,
    mode TEXT NOT NULL DEFAULT 'paper',
    symbol TEXT,
    resolved_symbol TEXT,
    instrument TEXT,
    strike REAL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL,
    sl REAL,
    target REAL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL,
    exit_reason TEXT,
    closed_at TEXT,
    pnl REAL,
    broker TEXT,
    broker_order_id TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS auto_trade_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'paper',
    min_score REAL NOT NULL DEFAULT 70,
    quantity REAL NOT NULL DEFAULT 1,
    max_open_positions INTEGER NOT NULL DEFAULT 5,
    max_daily_loss REAL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    broker TEXT NOT NULL,
    credentials_json TEXT,
    connected INTEGER NOT NULL DEFAULT 0,
    connected_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    UNIQUE(user_id, broker)
);

-- ---- F&O Directional Scanner (Phase 1) ----
-- Shared/global data: the F&O universe and scan results are the same market data for
-- every user, so (unlike signals/orders/settings above) these tables are NOT user-scoped.

CREATE TABLE IF NOT EXISTS fo_universe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    resolved_symbol TEXT NOT NULL,
    company_name TEXT,
    sector TEXT,
    industry TEXT,
    futures_eligible INTEGER NOT NULL DEFAULT 1,
    lot_size INTEGER,
    expiries_json TEXT,
    is_index INTEGER NOT NULL DEFAULT 0,
    last_updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stocks_total INTEGER NOT NULL DEFAULT 0,
    stocks_scanned INTEGER NOT NULL DEFAULT 0,
    ce_qualified_count INTEGER NOT NULL DEFAULT 0,
    pe_qualified_count INTEGER NOT NULL DEFAULT 0,
    near_ce_count INTEGER NOT NULL DEFAULT 0,
    near_pe_count INTEGER NOT NULL DEFAULT 0,
    not_qualified_count INTEGER NOT NULL DEFAULT 0,
    unavailable_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    triggered_by_user_id INTEGER,
    rule_version TEXT NOT NULL DEFAULT '1.0'
);

CREATE TABLE IF NOT EXISTS scanner_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    resolved_symbol TEXT NOT NULL,
    company_name TEXT,
    sector TEXT,
    classification TEXT NOT NULL,
    ce_conditions_json TEXT,
    pe_conditions_json TEXT,
    ce_passed_count INTEGER,
    ce_total_count INTEGER,
    pe_passed_count INTEGER,
    pe_total_count INTEGER,
    ce_score REAL,
    pe_score REAL,
    snapshot_json TEXT,
    data_timestamp TEXT,
    error_reason TEXT,
    FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_scanner_results_run ON scanner_results(scan_run_id);
CREATE INDEX IF NOT EXISTS idx_scanner_results_classification ON scanner_results(scan_run_id, classification);

CREATE TABLE IF NOT EXISTS scanner_signal_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    strike_preference TEXT NOT NULL DEFAULT 'ATM',
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS telegram_broadcast_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    target_chat_id INTEGER,
    target_chat_title TEXT,
    min_score REAL NOT NULL DEFAULT 60,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = _conn()
    try:
        # One-time migration: an older single-tenant version of this app used the same
        # table names without a user_id column. If that schema is detected, the data
        # predates multi-tenancy and can't be attributed to any account -- reset those
        # tables so the current (user_id-scoped) schema can be created cleanly.
        cols = conn.execute("PRAGMA table_info(signals)").fetchall()
        if cols and not any(c["name"] == "user_id" for c in cols):
            conn.executescript(
                """
                DROP TABLE IF EXISTS signals;
                DROP TABLE IF EXISTS monitored_channels;
                DROP TABLE IF EXISTS orders;
                DROP TABLE IF EXISTS auto_trade_settings;
                DROP TABLE IF EXISTS broker_accounts;
                """
            )
            conn.commit()

        conn.executescript(SCHEMA)
        conn.commit()

        # Additive column migrations for existing installs (never destructive -- see the
        # user_id migration above for why: a DROP-based reset once wiped a user's real data).
        cols = {c["name"] for c in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "lot_size" not in cols:
            conn.execute("ALTER TABLE signals ADD COLUMN lot_size INTEGER")
            conn.commit()

        sss_cols = {c["name"] for c in conn.execute("PRAGMA table_info(scanner_signal_settings)").fetchall()}
        if "strike_preference" not in sss_cols:
            conn.execute("ALTER TABLE scanner_signal_settings ADD COLUMN strike_preference TEXT NOT NULL DEFAULT 'ATM'")
            conn.commit()
    finally:
        conn.close()


# ---- Users / auth ----

def create_user(email: str, password_hash: str, verification_token: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO users (email, password_hash, email_verified, verification_token, created_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (email.lower().strip(), password_hash, verification_token, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def verify_user_email(user_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE users SET email_verified = 1, verification_token = NULL WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def create_session(token: str, user_id: int, expires_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, datetime.now(timezone.utc).isoformat(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(token: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_session(token: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


# ---- Per-user Telegram credentials ----

def get_telegram_credentials(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM telegram_credentials WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_telegram_credentials(user_id: int, api_id: str, api_hash: str) -> None:
    conn = _conn()
    try:
        session_name = f"user_{user_id}"
        conn.execute(
            """
            INSERT INTO telegram_credentials (user_id, api_id, api_hash, session_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET api_id = excluded.api_id, api_hash = excluded.api_hash
            """,
            (user_id, api_id, api_hash, session_name),
        )
        conn.commit()
    finally:
        conn.close()


def list_users_with_telegram_credentials() -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT user_id FROM telegram_credentials WHERE api_id IS NOT NULL AND api_hash IS NOT NULL"
        ).fetchall()
        return [r["user_id"] for r in rows]
    finally:
        conn.close()


# ---- Signals ----

def insert_signal(
    user_id: int,
    signal: dict,
    evaluation: dict,
    channel: str,
    source: str = "manual",
    telegram_chat_id: int = None,
    telegram_message_id: int = None,
) -> int:
    conn = _conn()
    try:
        lot_size = signal.get("lot_size")
        instrument = signal.get("instrument")
        if lot_size is None:
            if instrument == "EQ":
                lot_size = 1
            elif instrument in ("CE", "PE") and signal.get("resolved_symbol"):
                row = conn.execute(
                    "SELECT lot_size FROM fo_universe WHERE resolved_symbol = ?",
                    (signal["resolved_symbol"],),
                ).fetchone()
                if row and row["lot_size"]:
                    lot_size = row["lot_size"]

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO signals
            (user_id, created_at, channel, raw_text, symbol, resolved_symbol, instrument, strike,
             signal_type, entry_low, entry_high, sl, targets, score, verdict, direction,
             red_flags, evaluation_json, outcome, source, telegram_chat_id, telegram_message_id, lot_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                user_id,
                datetime.now(timezone.utc).isoformat(),
                channel or "unknown",
                signal.get("raw_text"),
                signal.get("symbol"),
                signal.get("resolved_symbol"),
                instrument,
                signal.get("strike"),
                signal.get("signal_type"),
                signal.get("entry_low"),
                signal.get("entry_high"),
                signal.get("sl"),
                json.dumps(signal.get("targets") or []),
                evaluation.get("score"),
                evaluation.get("verdict"),
                evaluation.get("direction"),
                json.dumps(evaluation.get("red_flags") or []),
                json.dumps(evaluation),
                source,
                telegram_chat_id,
                telegram_message_id,
                lot_size,
            ),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["targets"] = json.loads(d["targets"]) if d.get("targets") else []
    d["red_flags"] = json.loads(d["red_flags"]) if d.get("red_flags") else []
    if d.get("evaluation_json"):
        d["evaluation"] = json.loads(d["evaluation_json"])
    del d["evaluation_json"]
    return d


def list_signals(user_id: int, channel: str = None, outcome: str = None, limit: int = 200) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM signals WHERE user_id = ?"
        params = [user_id]
        if channel:
            query += " AND channel = ?"
            params.append(channel)
        if outcome:
            query += " AND outcome = ?"
            params.append(outcome)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_signal(user_id: int, signal_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM signals WHERE id = ? AND user_id = ?", (signal_id, user_id)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_outcome(user_id: int, signal_id: int, outcome: str, note: str = None) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE signals SET outcome = ?, outcome_note = ?, outcome_updated_at = ? WHERE id = ? AND user_id = ?",
            (outcome, note, datetime.now(timezone.utc).isoformat(), signal_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---- Monitored channels ----

def upsert_dialogs(user_id: int, dialogs: list) -> None:
    """dialogs: list of {telegram_chat_id, title, username}. Preserves existing enabled flags."""
    conn = _conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        for d in dialogs:
            conn.execute(
                """
                INSERT INTO monitored_channels (user_id, telegram_chat_id, title, username, enabled, last_synced_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(user_id, telegram_chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    last_synced_at = excluded.last_synced_at
                """,
                (user_id, d["telegram_chat_id"], d.get("title"), d.get("username"), now),
            )
        conn.commit()
    finally:
        conn.close()


def list_monitored_channels(user_id: int, enabled_only: bool = False) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM monitored_channels WHERE user_id = ?"
        params = [user_id]
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY title COLLATE NOCASE"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_channel_enabled(user_id: int, channel_id: int, enabled: bool) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE monitored_channels SET enabled = ? WHERE id = ? AND user_id = ?",
            (1 if enabled else 0, channel_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_enabled_chat_ids(user_id: int) -> set:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT telegram_chat_id FROM monitored_channels WHERE enabled = 1 AND user_id = ?", (user_id,)
        ).fetchall()
        return {r["telegram_chat_id"] for r in rows}
    finally:
        conn.close()


def get_channel_title(user_id: int, chat_id: int) -> str:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT title FROM monitored_channels WHERE telegram_chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
        return row["title"] if row else None
    finally:
        conn.close()


def channel_stats(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT channel,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome = 'target_hit' THEN 1 ELSE 0 END) AS targets_hit,
                   SUM(CASE WHEN outcome = 'sl_hit' THEN 1 ELSE 0 END) AS sl_hit,
                   SUM(CASE WHEN outcome = 'partial' THEN 1 ELSE 0 END) AS partial,
                   SUM(CASE WHEN outcome = 'pending' THEN 1 ELSE 0 END) AS pending,
                   AVG(score) AS avg_score
            FROM signals
            WHERE user_id = ?
            GROUP BY channel
            ORDER BY total DESC
            """,
            (user_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            decided = d["total"] - d["pending"]
            d["hit_rate"] = round(100 * d["targets_hit"] / decided, 1) if decided else None
            d["avg_score"] = round(d["avg_score"], 1) if d["avg_score"] is not None else None
            result.append(d)
        return result
    finally:
        conn.close()


# ---- Auto-trade settings (one row per user) ----

DEFAULT_SETTINGS = {
    "enabled": False,
    "mode": "paper",
    "min_score": 70,
    "quantity": 1,
    "max_open_positions": 5,
    "max_daily_loss": None,
}


def get_auto_trade_settings(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM auto_trade_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO auto_trade_settings (user_id, enabled, mode, min_score, quantity, max_open_positions, max_daily_loss)
                VALUES (?, 0, 'paper', 70, 1, 5, NULL)
                """,
                (user_id,),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM auto_trade_settings WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def save_auto_trade_settings(user_id: int, settings: dict) -> dict:
    current = get_auto_trade_settings(user_id)
    current.update({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS})
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE auto_trade_settings
            SET enabled = ?, mode = ?, min_score = ?, quantity = ?, max_open_positions = ?, max_daily_loss = ?
            WHERE user_id = ?
            """,
            (
                1 if current["enabled"] else 0,
                current["mode"],
                current["min_score"],
                current["quantity"],
                current["max_open_positions"],
                current["max_daily_loss"],
                user_id,
            ),
        )
        conn.commit()
        return get_auto_trade_settings(user_id)
    finally:
        conn.close()


# ---- Orders / positions ----

def insert_order(user_id: int, order: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO orders
            (user_id, created_at, signal_id, mode, symbol, resolved_symbol, instrument, strike, side,
             quantity, entry_price, sl, target, status, broker, broker_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                user_id,
                datetime.now(timezone.utc).isoformat(),
                order.get("signal_id"),
                order.get("mode", "paper"),
                order.get("symbol"),
                order.get("resolved_symbol"),
                order.get("instrument"),
                order.get("strike"),
                order["side"],
                order["quantity"],
                order.get("entry_price"),
                order.get("sl"),
                order.get("target"),
                order.get("broker"),
                order.get("broker_order_id"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_orders(user_id: int, status: str = None, mode: str = None, limit: int = 200) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM orders WHERE user_id = ?"
        params = [user_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_order(user_id: int, order_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_all_open_orders(mode: str = "paper") -> list:
    """Cross-user: used by the background position monitor, which checks every open
    paper position regardless of owner."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'open' AND mode = ?", (mode,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_open_positions(user_id: int, mode: str = "paper") -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE status = 'open' AND mode = ? AND user_id = ?", (mode, user_id)
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


def close_order(order_id: int, exit_price: float, exit_reason: str, pnl: float) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            UPDATE orders
            SET status = ?, exit_price = ?, exit_reason = ?, pnl = ?, closed_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (exit_reason, exit_price, exit_reason, pnl, datetime.now(timezone.utc).isoformat(), order_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def daily_realized_pnl(user_id: int, mode: str = "paper") -> float:
    conn = _conn()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT SUM(pnl) AS total FROM orders WHERE mode = ? AND user_id = ? AND closed_at LIKE ?",
            (mode, user_id, f"{today}%"),
        ).fetchone()
        return row["total"] or 0
    finally:
        conn.close()


# ---- Broker accounts ----

def list_broker_accounts(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, broker, connected, connected_at FROM broker_accounts WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_broker_account(user_id: int, broker: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM broker_accounts WHERE user_id = ? AND broker = ?", (user_id, broker)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["credentials"] = json.loads(d.pop("credentials_json") or "{}")
        return d
    finally:
        conn.close()


def get_broker_account_by_user(broker: str, user_id: int = None) -> dict:
    """Any connected account for this broker -- used by shared/global features (like the
    F&O Scanner) that need *a* working connection, not necessarily the requester's own."""
    conn = _conn()
    try:
        if user_id is not None:
            row = conn.execute(
                "SELECT * FROM broker_accounts WHERE broker = ? AND user_id = ? AND connected = 1", (broker, user_id)
            ).fetchone()
            if row:
                d = dict(row)
                d["credentials"] = json.loads(d.pop("credentials_json") or "{}")
                return d
        row = conn.execute(
            "SELECT * FROM broker_accounts WHERE broker = ? AND connected = 1 ORDER BY connected_at DESC LIMIT 1",
            (broker,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["credentials"] = json.loads(d.pop("credentials_json") or "{}")
        return d
    finally:
        conn.close()


def upsert_broker_account(user_id: int, broker: str, credentials: dict, connected: bool = True) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO broker_accounts (user_id, broker, credentials_json, connected, connected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, broker) DO UPDATE SET
                credentials_json = excluded.credentials_json,
                connected = excluded.connected,
                connected_at = excluded.connected_at
            """,
            (user_id, broker, json.dumps(credentials), 1 if connected else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def disconnect_broker_account(user_id: int, broker: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE broker_accounts SET connected = 0 WHERE broker = ? AND user_id = ?", (broker, user_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---- F&O universe ----

def replace_fo_universe(rows: list) -> int:
    """rows: list of {symbol, resolved_symbol, company_name, sector, industry, lot_size,
    futures_eligible, expiries, is_index}. Full replace -- the universe is meant to be
    refreshed wholesale from the data source, not patched incrementally."""
    conn = _conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM fo_universe")
        for r in rows:
            conn.execute(
                """
                INSERT INTO fo_universe
                (symbol, resolved_symbol, company_name, sector, industry, futures_eligible,
                 lot_size, expiries_json, is_index, last_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["symbol"],
                    r.get("resolved_symbol", r["symbol"]),
                    r.get("company_name"),
                    r.get("sector"),
                    r.get("industry"),
                    1 if r.get("futures_eligible", True) else 0,
                    r.get("lot_size"),
                    json.dumps(r.get("expiries") or []),
                    1 if r.get("is_index") else 0,
                    now,
                ),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def list_fo_universe(include_indices: bool = False) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM fo_universe"
        if not include_indices:
            query += " WHERE is_index = 0"
        query += " ORDER BY symbol"
        rows = conn.execute(query).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["expiries"] = json.loads(d.pop("expiries_json") or "[]")
            result.append(d)
        return result
    finally:
        conn.close()


def fo_universe_count() -> int:
    conn = _conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM fo_universe WHERE is_index = 0").fetchone()
        return row["n"]
    finally:
        conn.close()


# ---- Scan runs / results ----

def create_scan_run(stocks_total: int, triggered_by_user_id: int = None) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO scan_runs (started_at, status, stocks_total, triggered_by_user_id) VALUES (?, 'running', ?, ?)",
            (datetime.now(timezone.utc).isoformat(), stocks_total, triggered_by_user_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_scan_run(scan_run_id: int, counts: dict) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE scan_runs SET
                completed_at = ?, status = 'completed', stocks_scanned = ?,
                ce_qualified_count = ?, pe_qualified_count = ?, near_ce_count = ?, near_pe_count = ?,
                not_qualified_count = ?, unavailable_count = ?, conflict_count = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                counts.get("stocks_scanned", 0),
                counts.get("ce_qualified", 0),
                counts.get("pe_qualified", 0),
                counts.get("near_ce", 0),
                counts.get("near_pe", 0),
                counts.get("not_qualified", 0),
                counts.get("unavailable", 0),
                counts.get("conflict", 0),
                scan_run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fail_scan_run(scan_run_id: int, reason: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE scan_runs SET completed_at = ?, status = 'failed' WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), scan_run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_scan_run(scan_run_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_scan_run() -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_scan_runs(limit: int = 20) -> list:
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_scanner_result(scan_run_id: int, result: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO scanner_results
            (scan_run_id, symbol, resolved_symbol, company_name, sector, classification,
             ce_conditions_json, pe_conditions_json, ce_passed_count, ce_total_count,
             pe_passed_count, pe_total_count, ce_score, pe_score, snapshot_json,
             data_timestamp, error_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_run_id,
                result["symbol"],
                result.get("resolved_symbol", result["symbol"]),
                result.get("company_name"),
                result.get("sector"),
                result["classification"],
                json.dumps(result.get("ce_conditions") or []),
                json.dumps(result.get("pe_conditions") or []),
                result.get("ce_passed_count"),
                result.get("ce_total_count"),
                result.get("pe_passed_count"),
                result.get("pe_total_count"),
                result.get("ce_score"),
                result.get("pe_score"),
                json.dumps(result.get("snapshot") or {}),
                result.get("data_timestamp"),
                result.get("error_reason"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _scanner_result_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["ce_conditions"] = json.loads(d.pop("ce_conditions_json") or "[]")
    d["pe_conditions"] = json.loads(d.pop("pe_conditions_json") or "[]")
    d["snapshot"] = json.loads(d.pop("snapshot_json") or "{}")
    return d


def list_scanner_results(scan_run_id: int, classification: str = None, limit: int = 500) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM scanner_results WHERE scan_run_id = ?"
        params = [scan_run_id]
        if classification:
            query += " AND classification = ?"
            params.append(classification)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [_scanner_result_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_scanner_result(scan_run_id: int, symbol: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM scanner_results WHERE scan_run_id = ? AND symbol = ?", (scan_run_id, symbol)
        ).fetchone()
        return _scanner_result_to_dict(row) if row else None
    finally:
        conn.close()


def get_previous_classification(before_scan_run_id: int, symbol: str) -> str:
    """Classification this symbol had in the most recent scan run before the given one --
    used to detect a fresh transition into CE_QUALIFIED/PE_QUALIFIED rather than re-signaling
    a stock that's already been qualified for several scans running."""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT classification FROM scanner_results
            WHERE symbol = ? AND scan_run_id < ?
            ORDER BY scan_run_id DESC LIMIT 1
            """,
            (symbol, before_scan_run_id),
        ).fetchone()
        return row["classification"] if row else None
    finally:
        conn.close()


# ---- Scanner-generated signal settings (per user) ----

def get_scanner_signal_settings(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM scanner_signal_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"user_id": user_id, "enabled": False, "strike_preference": "ATM"}
        return dict(row)
    finally:
        conn.close()


def save_scanner_signal_settings(user_id: int, enabled: bool, strike_preference: str = "ATM") -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO scanner_signal_settings (user_id, enabled, strike_preference) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled, strike_preference = excluded.strike_preference
            """,
            (user_id, 1 if enabled else 0, strike_preference),
        )
        conn.commit()
        return get_scanner_signal_settings(user_id)
    finally:
        conn.close()


def list_users_with_scanner_signals_enabled() -> list:
    conn = _conn()
    try:
        rows = conn.execute("SELECT user_id FROM scanner_signal_settings WHERE enabled = 1").fetchall()
        return [r["user_id"] for r in rows]
    finally:
        conn.close()


# ---- Telegram broadcast settings (per user) ----

def get_telegram_broadcast_settings(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM telegram_broadcast_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"user_id": user_id, "enabled": False, "target_chat_id": None, "target_chat_title": None, "min_score": 60}
        return dict(row)
    finally:
        conn.close()


def save_telegram_broadcast_settings(user_id: int, enabled: bool, target_chat_id: int, target_chat_title: str, min_score: float) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO telegram_broadcast_settings (user_id, enabled, target_chat_id, target_chat_title, min_score)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                target_chat_id = excluded.target_chat_id,
                target_chat_title = excluded.target_chat_title,
                min_score = excluded.min_score
            """,
            (user_id, 1 if enabled else 0, target_chat_id, target_chat_title, min_score),
        )
        conn.commit()
        return get_telegram_broadcast_settings(user_id)
    finally:
        conn.close()
