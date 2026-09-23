import json
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "signals.db"

IST = ZoneInfo("Asia/Kolkata")


def _ist_date_to_utc_bounds(date_str: str) -> tuple:
    """'YYYY-MM-DD' (an IST calendar date, e.g. a trading day) -> (utc_start_iso, utc_end_iso)
    spanning that whole day in IST, converted to UTC -- created_at is stored as a UTC ISO
    timestamp, so a plain string date wouldn't line up with the trading day traders actually
    mean (IST midnight-to-midnight), especially near either end of the day."""
    day_start_ist = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    start_utc = day_start_ist.astimezone(timezone.utc)
    end_utc = (day_start_ist + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc.isoformat(), end_utc.isoformat()

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
    external_ref TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_telegram_msg
    ON signals(user_id, telegram_chat_id, telegram_message_id)
    WHERE telegram_chat_id IS NOT NULL AND telegram_message_id IS NOT NULL;
-- idx_signals_external_ref is created in init_db() below, not here -- on an existing install
-- this script runs before the external_ref column migration adds the column, and an index on
-- a not-yet-existing column would fail the whole executescript.

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

-- Tracks, per (user, channel), whether that channel's dedicated auto-paper-trade has been
-- automatically graduated to real live orders once it proved reliable enough -- see
-- app/graduation.py. status flips back to 'paper' (a demotion, not a delete) if the channel's
-- win rate later drops, so the row is a full history, not just a one-way switch.
CREATE TABLE IF NOT EXISTS channel_graduation (
    user_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paper',
    win_rate_at_change REAL,
    trades_at_change INTEGER,
    changed_at TEXT,
    PRIMARY KEY (user_id, channel),
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
    rule_version TEXT NOT NULL DEFAULT '1.0',
    strategy_id TEXT NOT NULL DEFAULT 'range_expansion_v1'
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

-- Backtests are per-user (a personal analysis exercise), unlike the shared/global scanner
-- tables above -- each user's own runs, over whatever symbols/date range they chose.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    max_hold_days INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    symbols_scanned INTEGER,
    total_trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    time_exits INTEGER,
    win_rate REAL,
    avg_return_pct REAL,
    avg_win_pct REAL,
    avg_loss_pct REAL,
    profit_factor REAL,
    errors_json TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_user ON backtest_runs(user_id);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl REAL NOT NULL,
    target REAL NOT NULL,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    return_pct REAL,
    hold_days INTEGER,
    FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(backtest_run_id);

CREATE TABLE IF NOT EXISTS scanner_signal_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    strike_preference TEXT NOT NULL DEFAULT 'ATM',
    paper_trade_enabled INTEGER NOT NULL DEFAULT 0,
    paper_trade_min_score REAL NOT NULL DEFAULT 70,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Per-user opt-out of a strategy (absence of a row means enabled -- see
-- get_user_strategy_settings) so existing users keep today's behavior with no migration.
CREATE TABLE IF NOT EXISTS user_strategies (
    user_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, strategy_id),
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

-- Dedicated paper-trading for signals auto-evaluated from monitored Telegram channels (see
-- scanner_signal_settings.paper_trade_* for the same concept applied to F&O Scanner signals).
-- Always paper, never live, and scoped to source='telegram' only -- separate from both the
-- general Broker Setup auto-trade toggle and the scanner one, so each source's reliability
-- (via Channel Stats' hit-rate, grouped by channel) can be measured independently.
CREATE TABLE IF NOT EXISTS telegram_paper_trade_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    min_score REAL NOT NULL DEFAULT 70,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Personal alerts (sent to the user's own Telegram Saved Messages, via their already-
-- connected Telethon session -- no target chat to configure, unlike telegram_broadcast_settings
-- which posts to a channel other people see). Two triggers: a new signal clearing min_score,
-- and an open position's price moving within sl_proximity_pct of its stop-loss.
CREATE TABLE IF NOT EXISTS alert_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    min_score REAL NOT NULL DEFAULT 80,
    sl_proximity_pct REAL NOT NULL DEFAULT 2.0,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Chartink can't send auth headers on its webhook alerts, so the token embedded in the
-- webhook URL itself is the only thing identifying which user (and which scan) a hit belongs to.
CREATE TABLE IF NOT EXISTS chartink_settings (
    user_id INTEGER PRIMARY KEY,
    webhook_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- One row per distinct Chartink scan a user's webhook has ever received a hit from --
-- auto-created (disabled-for-trading by default direction='bullish') the first time it fires,
-- then the user assigns it a direction (Chartink itself doesn't say bullish/bearish) and
-- optionally turns on its own dedicated paper-trade, same pattern as telegram_paper_trade_settings
-- and scanner_signal_settings but scoped per-scan instead of being one global toggle.
CREATE TABLE IF NOT EXISTS chartink_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scan_url TEXT NOT NULL,
    scan_name TEXT,
    direction TEXT NOT NULL DEFAULT 'bullish',
    enabled INTEGER NOT NULL DEFAULT 1,
    paper_trade_enabled INTEGER NOT NULL DEFAULT 0,
    paper_trade_min_score REAL NOT NULL DEFAULT 70,
    first_seen_at TEXT NOT NULL,
    last_triggered_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    UNIQUE(user_id, scan_url)
);

-- Confluence auto-trade: only takes a position when the F&O Scanner and a Chartink scan both
-- independently flag the same symbol + direction within window_minutes of each other. Always
-- paper (see app/confluence.py) -- like the other per-source auto-trade toggles, it exists to
-- measure this specific strategy's own reliability via Channel Stats before anyone would trust
-- it with real money.
CREATE TABLE IF NOT EXISTS confluence_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    window_minutes INTEGER NOT NULL DEFAULT 30,
    quantity REAL NOT NULL DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers and writers avoid blocking each other, and busy_timeout makes a
    # writer that does contend retry for a while instead of raising "database is locked"
    # immediately (the default busy_timeout is 0) -- needed now that background jobs like
    # the scanner and backtester write from several threads concurrently.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
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
        if "paper_trade_enabled" not in sss_cols:
            conn.execute("ALTER TABLE scanner_signal_settings ADD COLUMN paper_trade_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE scanner_signal_settings ADD COLUMN paper_trade_min_score REAL NOT NULL DEFAULT 70")
            conn.commit()

        sr_cols = {c["name"] for c in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
        if "strategy_id" not in sr_cols:
            conn.execute("ALTER TABLE scan_runs ADD COLUMN strategy_id TEXT NOT NULL DEFAULT 'range_expansion_v1'")
            conn.commit()

        ats_cols = {c["name"] for c in conn.execute("PRAGMA table_info(auto_trade_settings)").fetchall()}
        if "position_sizing_enabled" not in ats_cols:
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN position_sizing_enabled INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        if "auto_graduate_enabled" not in ats_cols:
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN auto_graduate_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN auto_graduate_min_trades INTEGER NOT NULL DEFAULT 15")
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN auto_graduate_min_win_rate REAL NOT NULL DEFAULT 75")
            conn.commit()

        orders_cols = {c["name"] for c in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "sl_alert_sent" not in orders_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN sl_alert_sent INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        if "trailing_enabled" not in orders_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN trailing_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE orders ADD COLUMN trail_pct REAL")
            conn.execute("ALTER TABLE orders ADD COLUMN lock_trigger_pct REAL")
            conn.execute("ALTER TABLE orders ADD COLUMN lock_pct REAL")
            conn.execute("ALTER TABLE orders ADD COLUMN peak_price REAL")
            conn.execute("ALTER TABLE orders ADD COLUMN profit_locked INTEGER NOT NULL DEFAULT 0")
            conn.commit()

        if "default_trailing_enabled" not in ats_cols:
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN default_trailing_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN default_trail_pct REAL NOT NULL DEFAULT 2")
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN default_lock_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN default_lock_trigger_pct REAL NOT NULL DEFAULT 5")
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN default_lock_pct REAL NOT NULL DEFAULT 2")
            conn.commit()
        if "live_auto_exit_enabled" not in ats_cols:
            # Defaults OFF -- nobody with a live-trading setup already configured should have
            # their behavior silently change to "the monitor loop now places real exit orders
            # on my behalf" just because of a deploy.
            conn.execute("ALTER TABLE auto_trade_settings ADD COLUMN live_auto_exit_enabled INTEGER NOT NULL DEFAULT 0")
            conn.commit()

        cols = {c["name"] for c in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "external_ref" not in cols:
            conn.execute("ALTER TABLE signals ADD COLUMN external_ref TEXT")
            conn.commit()
        # Always ensured (not just on first add) -- a fresh install gets the column via SCHEMA
        # directly, with no ALTER TABLE branch above ever running, so the index still needs to
        # be created unconditionally here rather than only inside the migration branch.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_external_ref "
            "ON signals(user_id, source, external_ref) WHERE external_ref IS NOT NULL"
        )
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


def update_password_hash(user_id: int, password_hash: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_all_sessions_for_user(user_id: int, except_token: str = None) -> int:
    conn = _conn()
    try:
        if except_token:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user_id, except_token))
        else:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount
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
    external_ref: str = None,
) -> int:
    conn = _conn()
    try:
        lot_size = signal.get("lot_size")
        instrument = signal.get("instrument")
        if lot_size is None:
            if instrument == "EQ":
                lot_size = 1
            elif instrument in ("CE", "PE") and signal.get("resolved_symbol"):
                lot_size = get_lot_size(signal["resolved_symbol"])

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO signals
            (user_id, created_at, channel, raw_text, symbol, resolved_symbol, instrument, strike,
             signal_type, entry_low, entry_high, sl, targets, score, verdict, direction,
             red_flags, evaluation_json, outcome, source, telegram_chat_id, telegram_message_id, lot_size, external_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
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
                external_ref,
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


def list_signals(
    user_id: int,
    channel: str = None,
    outcome: str = None,
    source: str = None,
    signal_type: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 200,
) -> list:
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
        if source:
            query += " AND source = ?"
            params.append(source)
        if signal_type:
            query += " AND signal_type = ?"
            params.append(signal_type)
        if date_from:
            start_iso, _ = _ist_date_to_utc_bounds(date_from)
            query += " AND created_at >= ?"
            params.append(start_iso)
        if date_to:
            _, end_iso = _ist_date_to_utc_bounds(date_to)
            query += " AND created_at < ?"
            params.append(end_iso)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_distinct_channels(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT channel FROM signals WHERE user_id = ? ORDER BY channel", (user_id,)
        ).fetchall()
        return [r["channel"] for r in rows]
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


# ---- Chartink webhook ----

def get_or_create_chartink_token(user_id: int) -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT webhook_token FROM chartink_settings WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return row["webhook_token"]
        token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO chartink_settings (user_id, webhook_token, created_at) VALUES (?, ?, ?)",
            (user_id, token, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def regenerate_chartink_token(user_id: int) -> str:
    conn = _conn()
    try:
        token = secrets.token_urlsafe(24)
        conn.execute(
            """
            INSERT INTO chartink_settings (user_id, webhook_token, created_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET webhook_token = excluded.webhook_token
            """,
            (user_id, token, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_user_id_by_chartink_token(token: str) -> int:
    conn = _conn()
    try:
        row = conn.execute("SELECT user_id FROM chartink_settings WHERE webhook_token = ?", (token,)).fetchone()
        return row["user_id"] if row else None
    finally:
        conn.close()


def get_or_create_chartink_scan(user_id: int, scan_url: str, scan_name: str) -> dict:
    """Upserts the (user, scan) row on every webhook hit -- refreshes scan_name/last_triggered_at
    but preserves the user's own direction/enabled/paper-trade settings once they've set them."""
    conn = _conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO chartink_scans (user_id, scan_url, scan_name, first_seen_at, last_triggered_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, scan_url) DO UPDATE SET
                scan_name = excluded.scan_name,
                last_triggered_at = excluded.last_triggered_at
            """,
            (user_id, scan_url, scan_name, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM chartink_scans WHERE user_id = ? AND scan_url = ?", (user_id, scan_url)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_chartink_scans(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM chartink_scans WHERE user_id = ? ORDER BY last_triggered_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_chartink_scan(
    user_id: int,
    scan_id: int,
    direction: str = None,
    enabled: bool = None,
    paper_trade_enabled: bool = None,
    paper_trade_min_score: float = None,
) -> bool:
    conn = _conn()
    try:
        sets, params = [], []
        if direction is not None:
            sets.append("direction = ?")
            params.append(direction)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if paper_trade_enabled is not None:
            sets.append("paper_trade_enabled = ?")
            params.append(1 if paper_trade_enabled else 0)
        if paper_trade_min_score is not None:
            sets.append("paper_trade_min_score = ?")
            params.append(paper_trade_min_score)
        if not sets:
            return False
        params.extend([scan_id, user_id])
        cur = conn.execute(
            f"UPDATE chartink_scans SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---- Confluence auto-trade (per user) ----

def get_confluence_settings(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM confluence_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"user_id": user_id, "enabled": False, "window_minutes": 30, "quantity": 1}
        return dict(row)
    finally:
        conn.close()


def save_confluence_settings(user_id: int, enabled: bool, window_minutes: int, quantity: float) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO confluence_settings (user_id, enabled, window_minutes, quantity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                window_minutes = excluded.window_minutes,
                quantity = excluded.quantity
            """,
            (user_id, 1 if enabled else 0, window_minutes, quantity),
        )
        conn.commit()
        return get_confluence_settings(user_id)
    finally:
        conn.close()


def find_recent_signal(user_id: int, resolved_symbol: str, direction: str, source: str, since_iso: str) -> dict:
    """Most recent signal from `source` for this exact symbol+direction, no older than
    since_iso -- the other half of a confluence match (see app/confluence.py)."""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM signals
            WHERE user_id = ? AND resolved_symbol = ? AND direction = ? AND source = ? AND created_at >= ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, resolved_symbol, direction, source, since_iso),
        ).fetchone()
        return _row_to_dict(row) if row else None
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


def reliability_dashboard(user_id: int) -> dict:
    """Unified view of every auto-paper-trade source (F&O Scanner, each Telegram channel,
    manual) side by side: signal-level hit rate (from signals.outcome, same as channel_stats)
    plus trade-level P&L (from closed paper orders, joined via signal_id) -- so "which source
    is actually worth trusting" has one answer instead of three separate pages. Only paper
    trades are counted since that's what every auto-trade source uses to measure itself;
    live orders are a manual, deliberate act and don't belong in a reliability score."""
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT s.channel AS channel,
                   s.source AS source,
                   COUNT(DISTINCT s.id) AS total_signals,
                   SUM(CASE WHEN s.outcome = 'target_hit' THEN 1 ELSE 0 END) AS targets_hit,
                   SUM(CASE WHEN s.outcome = 'sl_hit' THEN 1 ELSE 0 END) AS sl_hit,
                   SUM(CASE WHEN s.outcome = 'pending' THEN 1 ELSE 0 END) AS pending,
                   AVG(s.score) AS avg_score,
                   COUNT(o.id) AS trades_closed,
                   SUM(CASE WHEN o.pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(o.pnl) AS total_pnl,
                   AVG(o.pnl) AS avg_pnl
            FROM signals s
            LEFT JOIN orders o ON o.signal_id = s.id AND o.status = 'closed' AND o.mode = 'paper'
            WHERE s.user_id = ?
            GROUP BY s.channel, s.source
            """,
            (user_id,),
        ).fetchall()

        sources = []
        for r in rows:
            d = dict(r)
            decided = d["total_signals"] - d["pending"]
            d["hit_rate"] = round(100 * d["targets_hit"] / decided, 1) if decided else None
            d["trades_closed"] = d["trades_closed"] or 0
            d["wins"] = d["wins"] or 0
            d["losses"] = d["trades_closed"] - d["wins"]
            d["win_rate"] = round(100 * d["wins"] / d["trades_closed"], 1) if d["trades_closed"] else None
            d["total_pnl"] = round(d["total_pnl"], 2) if d["total_pnl"] is not None else 0.0
            d["avg_pnl"] = round(d["avg_pnl"], 2) if d["avg_pnl"] is not None else None
            d["avg_score"] = round(d["avg_score"], 1) if d["avg_score"] is not None else None
            sources.append(d)
        sources.sort(key=lambda d: d["total_pnl"], reverse=True)

        curve_rows = conn.execute(
            """
            SELECT date(closed_at) AS day, SUM(pnl) AS day_pnl
            FROM orders
            WHERE user_id = ? AND status = 'closed' AND mode = 'paper' AND pnl IS NOT NULL
            GROUP BY day
            ORDER BY day ASC
            """,
            (user_id,),
        ).fetchall()
        equity_curve = []
        running = 0.0
        for r in curve_rows:
            running += r["day_pnl"]
            equity_curve.append({"day": r["day"], "cumulative_pnl": round(running, 2)})

        traded = [d for d in sources if d["trades_closed"] > 0]
        total_trades = sum(d["trades_closed"] for d in traded)
        total_wins = sum(d["wins"] for d in traded)
        summary = {
            "total_trades": total_trades,
            "total_pnl": round(sum(d["total_pnl"] for d in traded), 2),
            "win_rate": round(100 * total_wins / total_trades, 1) if total_trades else None,
            "best_channel": max(traded, key=lambda d: d["total_pnl"])["channel"] if traded else None,
            "worst_channel": min(traded, key=lambda d: d["total_pnl"])["channel"] if len(traded) > 1 else None,
        }

        return {"sources": sources, "equity_curve": equity_curve, "summary": summary}
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
    "position_sizing_enabled": False,
    "auto_graduate_enabled": False,
    "auto_graduate_min_trades": 15,
    "auto_graduate_min_win_rate": 75,
    "default_trailing_enabled": False,
    "default_trail_pct": 2,
    "default_lock_enabled": False,
    "default_lock_trigger_pct": 5,
    "default_lock_pct": 2,
    "live_auto_exit_enabled": False,
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
            SET enabled = ?, mode = ?, min_score = ?, quantity = ?, max_open_positions = ?, max_daily_loss = ?,
                position_sizing_enabled = ?, auto_graduate_enabled = ?, auto_graduate_min_trades = ?,
                auto_graduate_min_win_rate = ?, default_trailing_enabled = ?, default_trail_pct = ?,
                default_lock_enabled = ?, default_lock_trigger_pct = ?, default_lock_pct = ?,
                live_auto_exit_enabled = ?
            WHERE user_id = ?
            """,
            (
                1 if current["enabled"] else 0,
                current["mode"],
                current["min_score"],
                current["quantity"],
                current["max_open_positions"],
                current["max_daily_loss"],
                1 if current["position_sizing_enabled"] else 0,
                1 if current["auto_graduate_enabled"] else 0,
                current["auto_graduate_min_trades"],
                current["auto_graduate_min_win_rate"],
                1 if current["default_trailing_enabled"] else 0,
                current["default_trail_pct"],
                1 if current["default_lock_enabled"] else 0,
                current["default_lock_trigger_pct"],
                current["default_lock_pct"],
                1 if current["live_auto_exit_enabled"] else 0,
                user_id,
            ),
        )
        conn.commit()
        return get_auto_trade_settings(user_id)
    finally:
        conn.close()


def channel_reliability(user_id: int, channel: str) -> dict:
    """Historical win rate for one channel's closed paper trades -- the input to risk-based
    position sizing (see trading.size_for_reliability()). Deliberately narrow (just this one
    channel) rather than reusing reliability_dashboard(), which computes every channel at once
    and would be wasteful to call on every single order placement."""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(o.id) AS trades_closed,
                   SUM(CASE WHEN o.pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM signals s
            JOIN orders o ON o.signal_id = s.id AND o.status = 'closed' AND o.mode = 'paper'
            WHERE s.user_id = ? AND s.channel = ?
            """,
            (user_id, channel),
        ).fetchone()
        trades_closed = row["trades_closed"] or 0
        wins = row["wins"] or 0
        win_rate = round(100 * wins / trades_closed, 1) if trades_closed else None
        return {"trades_closed": trades_closed, "win_rate": win_rate}
    finally:
        conn.close()


# ---- Channel graduation (paper -> live, per user+channel) ----

def get_channel_graduation(user_id: int, channel: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM channel_graduation WHERE user_id = ? AND channel = ?", (user_id, channel)
        ).fetchone()
        return dict(row) if row else {"user_id": user_id, "channel": channel, "status": "paper"}
    finally:
        conn.close()


def set_channel_graduation(user_id: int, channel: str, status: str, win_rate: float, trades: int) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO channel_graduation (user_id, channel, status, win_rate_at_change, trades_at_change, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel) DO UPDATE SET
                status = excluded.status,
                win_rate_at_change = excluded.win_rate_at_change,
                trades_at_change = excluded.trades_at_change,
                changed_at = excluded.changed_at
            """,
            (user_id, channel, status, win_rate, trades, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return get_channel_graduation(user_id, channel)
    finally:
        conn.close()


def list_channel_graduations(user_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM channel_graduation WHERE user_id = ? ORDER BY changed_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
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
             quantity, entry_price, sl, target, status, broker, broker_order_id,
             trailing_enabled, trail_pct, lock_trigger_pct, lock_pct, peak_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
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
                1 if order.get("trailing_enabled") else 0,
                order.get("trail_pct"),
                order.get("lock_trigger_pct"),
                order.get("lock_pct"),
                order.get("entry_price"),  # peak_price starts at entry
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_orders(
    user_id: int, status: str = None, mode: str = None, date_from: str = None, date_to: str = None, limit: int = 200
) -> list:
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
        if date_from:
            start_iso, _ = _ist_date_to_utc_bounds(date_from)
            query += " AND created_at >= ?"
            params.append(start_iso)
        if date_to:
            _, end_iso = _ist_date_to_utc_bounds(date_to)
            query += " AND created_at < ?"
            params.append(end_iso)
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


def order_exists_for_signal(user_id: int, signal_id: int) -> bool:
    """One signal should never produce more than one order, no matter how many independent
    auto-trade paths are enabled at once (general Broker-Setup auto-trade, the F&O Scanner's
    own dedicated paper-trade, a Telegram channel's dedicated paper-trade) -- see
    trading.place_paper_order()/place_live_order(), which both call this before inserting."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM orders WHERE user_id = ? AND signal_id = ? LIMIT 1", (user_id, signal_id)
        ).fetchone()
        return row is not None
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


def update_order_risk_settings(
    user_id: int, order_id: int, trailing_enabled: bool, trail_pct: float, lock_trigger_pct: float, lock_pct: float
) -> dict:
    """Per-position risk-manager override -- lets a user turn on trailing/profit-lock (or
    change the %s) on an already-open position, not just at entry time via the per-user
    defaults. Resets profit_locked to 0 so a freshly-lowered lock_trigger_pct gets a chance to
    fire again rather than staying permanently skipped from before the edit."""
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE orders
            SET trailing_enabled = ?, trail_pct = ?, lock_trigger_pct = ?, lock_pct = ?, profit_locked = 0
            WHERE id = ? AND user_id = ?
            """,
            (1 if trailing_enabled else 0, trail_pct, lock_trigger_pct, lock_pct, order_id, user_id),
        )
        conn.commit()
        return get_order(user_id, order_id)
    finally:
        conn.close()


def update_order_risk_state(order_id: int, sl: float, peak_price: float, profit_locked: bool) -> None:
    """Persists the tick-by-tick outcome of trading.apply_risk_management() -- called from the
    60s position-monitor loop, never directly by a user action."""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE orders SET sl = ?, peak_price = ?, profit_locked = ? WHERE id = ?",
            (sl, peak_price, 1 if profit_locked else 0, order_id),
        )
        conn.commit()
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

def get_lot_size(resolved_symbol: str) -> int:
    """Real NSE lot size for an F&O underlying, or None if not found (e.g. an equity signal,
    or a symbol not in the cached universe) -- the single lookup insert_signal() and
    trading.place_paper_order()'s options lot-rounding both use, so there's one place that
    knows how to resolve this rather than two copies of the same query."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT lot_size FROM fo_universe WHERE resolved_symbol = ?", (resolved_symbol,)
        ).fetchone()
        return row["lot_size"] if row and row["lot_size"] else None
    finally:
        conn.close()


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

def create_scan_run(stocks_total: int, triggered_by_user_id: int = None, strategy_id: str = "range_expansion_v1") -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO scan_runs (started_at, status, stocks_total, triggered_by_user_id, strategy_id) VALUES (?, 'running', ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), stocks_total, triggered_by_user_id, strategy_id),
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


def get_latest_scan_run(strategy_id: str = None) -> dict:
    conn = _conn()
    try:
        if strategy_id:
            row = conn.execute(
                "SELECT * FROM scan_runs WHERE strategy_id = ? ORDER BY id DESC LIMIT 1", (strategy_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_scan_runs(limit: int = 20, strategy_id: str = None) -> list:
    conn = _conn()
    try:
        if strategy_id:
            rows = conn.execute(
                "SELECT * FROM scan_runs WHERE strategy_id = ? ORDER BY id DESC LIMIT ?", (strategy_id, limit)
            ).fetchall()
        else:
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


def create_backtest_run(
    user_id: int, strategy_id: str, symbols: list, start_date: str, end_date: str, max_hold_days: int
) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO backtest_runs
            (user_id, strategy_id, symbols_json, start_date, end_date, max_hold_days, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (user_id, strategy_id, json.dumps(symbols), start_date, end_date, max_hold_days,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_backtest_run(backtest_run_id: int, stats: dict) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE backtest_runs SET
                completed_at = ?, status = 'completed', symbols_scanned = ?, total_trades = ?,
                wins = ?, losses = ?, time_exits = ?, win_rate = ?, avg_return_pct = ?,
                avg_win_pct = ?, avg_loss_pct = ?, profit_factor = ?, errors_json = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                stats.get("symbols_scanned"),
                stats.get("total_trades"),
                stats.get("wins"),
                stats.get("losses"),
                stats.get("time_exits"),
                stats.get("win_rate"),
                stats.get("avg_return_pct"),
                stats.get("avg_win_pct"),
                stats.get("avg_loss_pct"),
                stats.get("profit_factor"),
                json.dumps(stats.get("errors") or {}),
                backtest_run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fail_backtest_run(backtest_run_id: int, reason: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE backtest_runs SET completed_at = ?, status = 'failed', errors_json = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), json.dumps({"_run": reason}), backtest_run_id),
        )
        conn.commit()
    finally:
        conn.close()


def insert_backtest_trade(backtest_run_id: int, trade: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO backtest_trades
            (backtest_run_id, symbol, direction, entry_date, entry_price, sl, target,
             exit_date, exit_price, exit_reason, return_pct, hold_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                backtest_run_id,
                trade["symbol"],
                trade["direction"],
                trade["entry_date"],
                trade["entry_price"],
                trade["sl"],
                trade["target"],
                trade.get("exit_date"),
                trade.get("exit_price"),
                trade.get("exit_reason"),
                trade.get("return_pct"),
                trade.get("hold_days"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_backtest_trades(backtest_run_id: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE backtest_run_id = ? ORDER BY entry_date, id", (backtest_run_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_backtest_runs(user_id: int, limit: int = 20) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_runs WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["symbols"] = json.loads(d.pop("symbols_json") or "[]")
            d["errors"] = json.loads(d.pop("errors_json") or "{}")
            result.append(d)
        return result
    finally:
        conn.close()


def get_backtest_run(user_id: int, backtest_run_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ? AND user_id = ?", (backtest_run_id, user_id)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["symbols"] = json.loads(d.pop("symbols_json") or "[]")
        d["errors"] = json.loads(d.pop("errors_json") or "{}")
        return d
    finally:
        conn.close()


def get_previous_classification(before_scan_run_id: int, symbol: str, strategy_id: str = None) -> str:
    """Classification this symbol had in the most recent scan run before the given one --
    used to detect a fresh transition into CE_QUALIFIED/PE_QUALIFIED rather than re-signaling
    a stock that's already been qualified for several scans running. Scoped to the same
    strategy when given, since different strategies' condition sets aren't comparable --
    "already qualified last time" must mean under the same rules."""
    conn = _conn()
    try:
        if strategy_id:
            row = conn.execute(
                """
                SELECT sr.classification FROM scanner_results sr
                JOIN scan_runs run ON run.id = sr.scan_run_id
                WHERE sr.symbol = ? AND sr.scan_run_id < ? AND run.strategy_id = ?
                ORDER BY sr.scan_run_id DESC LIMIT 1
                """,
                (symbol, before_scan_run_id, strategy_id),
            ).fetchone()
        else:
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
            return {
                "user_id": user_id, "enabled": False, "strike_preference": "ATM",
                "paper_trade_enabled": False, "paper_trade_min_score": 70,
            }
        d = dict(row)
        d["paper_trade_enabled"] = bool(d["paper_trade_enabled"])
        return d
    finally:
        conn.close()


def save_scanner_signal_settings(
    user_id: int,
    enabled: bool,
    strike_preference: str = "ATM",
    paper_trade_enabled: bool = False,
    paper_trade_min_score: float = 70,
) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO scanner_signal_settings
                (user_id, enabled, strike_preference, paper_trade_enabled, paper_trade_min_score)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                strike_preference = excluded.strike_preference,
                paper_trade_enabled = excluded.paper_trade_enabled,
                paper_trade_min_score = excluded.paper_trade_min_score
            """,
            (user_id, 1 if enabled else 0, strike_preference, 1 if paper_trade_enabled else 0, paper_trade_min_score),
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


# ---- Strategy settings (per user; absence of a row means enabled) ----

def get_user_strategy_settings(user_id: int, all_strategy_ids: list) -> dict:
    """Returns {strategy_id: enabled} for every known strategy. A strategy with no row for
    this user defaults to enabled=True, so existing users see no behavior change until they
    actively uncheck something on the Strategy tab."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT strategy_id, enabled FROM user_strategies WHERE user_id = ?", (user_id,)).fetchall()
        overrides = {r["strategy_id"]: bool(r["enabled"]) for r in rows}
        return {sid: overrides.get(sid, True) for sid in all_strategy_ids}
    finally:
        conn.close()


def save_user_strategy_setting(user_id: int, strategy_id: str, enabled: bool) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO user_strategies (user_id, strategy_id, enabled) VALUES (?, ?, ?)
            ON CONFLICT(user_id, strategy_id) DO UPDATE SET enabled = excluded.enabled
            """,
            (user_id, strategy_id, 1 if enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()


def list_enabled_strategy_ids_for_user(user_id: int, all_strategy_ids: list) -> list:
    settings = get_user_strategy_settings(user_id, all_strategy_ids)
    return [sid for sid, enabled in settings.items() if enabled]


def is_strategy_enabled_for_user(user_id: int, strategy_id: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT enabled FROM user_strategies WHERE user_id = ? AND strategy_id = ?", (user_id, strategy_id)
        ).fetchone()
        return bool(row["enabled"]) if row else True  # no row = enabled by default
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


# ---- Telegram auto-paper-trade settings (per user) ----

def get_telegram_paper_trade_settings(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM telegram_paper_trade_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"user_id": user_id, "enabled": False, "min_score": 70}
        return dict(row)
    finally:
        conn.close()


def save_telegram_paper_trade_settings(user_id: int, enabled: bool, min_score: float) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO telegram_paper_trade_settings (user_id, enabled, min_score)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                min_score = excluded.min_score
            """,
            (user_id, 1 if enabled else 0, min_score),
        )
        conn.commit()
        return get_telegram_paper_trade_settings(user_id)
    finally:
        conn.close()


# ---- Personal alerts (per user) ----

def get_alert_settings(user_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM alert_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"user_id": user_id, "enabled": False, "min_score": 80, "sl_proximity_pct": 2.0}
        return dict(row)
    finally:
        conn.close()


def save_alert_settings(user_id: int, enabled: bool, min_score: float, sl_proximity_pct: float) -> dict:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO alert_settings (user_id, enabled, min_score, sl_proximity_pct)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                min_score = excluded.min_score,
                sl_proximity_pct = excluded.sl_proximity_pct
            """,
            (user_id, 1 if enabled else 0, min_score, sl_proximity_pct),
        )
        conn.commit()
        return get_alert_settings(user_id)
    finally:
        conn.close()


def mark_sl_alert_sent(order_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE orders SET sl_alert_sent = 1 WHERE id = ?", (order_id,))
        conn.commit()
    finally:
        conn.close()
