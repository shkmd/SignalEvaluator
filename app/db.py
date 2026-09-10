import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    telegram_message_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_telegram_msg
    ON signals(telegram_chat_id, telegram_message_id)
    WHERE telegram_chat_id IS NOT NULL AND telegram_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS monitored_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    title TEXT,
    username TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS auto_trade_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'paper',
    min_score REAL NOT NULL DEFAULT 70,
    quantity REAL NOT NULL DEFAULT 1,
    max_open_positions INTEGER NOT NULL DEFAULT 5,
    max_daily_loss REAL
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker TEXT NOT NULL UNIQUE,
    credentials_json TEXT,
    connected INTEGER NOT NULL DEFAULT 0,
    connected_at TEXT
);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_signal(
    signal: dict,
    evaluation: dict,
    channel: str,
    source: str = "manual",
    telegram_chat_id: int = None,
    telegram_message_id: int = None,
) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO signals
            (created_at, channel, raw_text, symbol, resolved_symbol, instrument, strike,
             signal_type, entry_low, entry_high, sl, targets, score, verdict, direction,
             red_flags, evaluation_json, outcome, source, telegram_chat_id, telegram_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                channel or "unknown",
                signal.get("raw_text"),
                signal.get("symbol"),
                signal.get("resolved_symbol"),
                signal.get("instrument"),
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


def list_signals(channel: str = None, outcome: str = None, limit: int = 200) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
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


def get_signal(signal_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_outcome(signal_id: int, outcome: str, note: str = None) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE signals SET outcome = ?, outcome_note = ?, outcome_updated_at = ? WHERE id = ?",
            (outcome, note, datetime.now(timezone.utc).isoformat(), signal_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def upsert_dialogs(dialogs: list) -> None:
    """dialogs: list of {telegram_chat_id, title, username}. Preserves existing enabled flags."""
    conn = _conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        for d in dialogs:
            conn.execute(
                """
                INSERT INTO monitored_channels (telegram_chat_id, title, username, enabled, last_synced_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    last_synced_at = excluded.last_synced_at
                """,
                (d["telegram_chat_id"], d.get("title"), d.get("username"), now),
            )
        conn.commit()
    finally:
        conn.close()


def list_monitored_channels(enabled_only: bool = False) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM monitored_channels"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY title COLLATE NOCASE"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_channel_enabled(channel_id: int, enabled: bool) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE monitored_channels SET enabled = ? WHERE id = ?", (1 if enabled else 0, channel_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_enabled_chat_ids() -> set:
    conn = _conn()
    try:
        rows = conn.execute("SELECT telegram_chat_id FROM monitored_channels WHERE enabled = 1").fetchall()
        return {r["telegram_chat_id"] for r in rows}
    finally:
        conn.close()


def get_channel_title(chat_id: int) -> str:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT title FROM monitored_channels WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        return row["title"] if row else None
    finally:
        conn.close()


def channel_stats() -> list:
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
            GROUP BY channel
            ORDER BY total DESC
            """
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


# ---- Auto-trade settings (single row) ----

DEFAULT_SETTINGS = {
    "enabled": False,
    "mode": "paper",
    "min_score": 70,
    "quantity": 1,
    "max_open_positions": 5,
    "max_daily_loss": None,
}


def get_auto_trade_settings() -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM auto_trade_settings WHERE id = 1").fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO auto_trade_settings (id, enabled, mode, min_score, quantity, max_open_positions, max_daily_loss)
                VALUES (1, 0, 'paper', 70, 1, 5, NULL)
                """
            )
            conn.commit()
            row = conn.execute("SELECT * FROM auto_trade_settings WHERE id = 1").fetchone()
        return dict(row)
    finally:
        conn.close()


def save_auto_trade_settings(settings: dict) -> dict:
    current = get_auto_trade_settings()
    current.update({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS})
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE auto_trade_settings
            SET enabled = ?, mode = ?, min_score = ?, quantity = ?, max_open_positions = ?, max_daily_loss = ?
            WHERE id = 1
            """,
            (
                1 if current["enabled"] else 0,
                current["mode"],
                current["min_score"],
                current["quantity"],
                current["max_open_positions"],
                current["max_daily_loss"],
            ),
        )
        conn.commit()
        return get_auto_trade_settings()
    finally:
        conn.close()


# ---- Orders / positions ----

def insert_order(order: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO orders
            (created_at, signal_id, mode, symbol, resolved_symbol, instrument, strike, side,
             quantity, entry_price, sl, target, status, broker)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
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
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_orders(status: str = None, mode: str = None, limit: int = 200) -> list:
    conn = _conn()
    try:
        query = "SELECT * FROM orders WHERE 1=1"
        params = []
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


def count_open_positions(mode: str = "paper") -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE status = 'open' AND mode = ?", (mode,)
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


def daily_realized_pnl(mode: str = "paper") -> float:
    conn = _conn()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT SUM(pnl) AS total FROM orders WHERE mode = ? AND closed_at LIKE ?",
            (mode, f"{today}%"),
        ).fetchone()
        return row["total"] or 0
    finally:
        conn.close()


# ---- Broker accounts ----

def list_broker_accounts() -> list:
    conn = _conn()
    try:
        rows = conn.execute("SELECT id, broker, connected, connected_at FROM broker_accounts").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_broker_account(broker: str, credentials: dict, connected: bool = True) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO broker_accounts (broker, credentials_json, connected, connected_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(broker) DO UPDATE SET
                credentials_json = excluded.credentials_json,
                connected = excluded.connected,
                connected_at = excluded.connected_at
            """,
            (broker, json.dumps(credentials), 1 if connected else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def disconnect_broker_account(broker: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("UPDATE broker_accounts SET connected = 0 WHERE broker = ?", (broker,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
