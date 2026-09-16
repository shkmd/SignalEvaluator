"""TradeMind's own tables, all prefixed tm_ and kept separate from the main app's
signals/orders/broker_accounts tables on purpose: those exist to evaluate and paper/live-trade
a signal going forward, while TradeMind exists to import and journal what already happened on
a real broker account. Same underlying concept (a "trade"), different purpose and shape --
merging them would force one schema to awkwardly serve two unrelated workflows.
"""
from app.db import _conn

SCHEMA_TM = """
CREATE TABLE IF NOT EXISTS tm_broker_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    broker TEXT NOT NULL,
    nickname TEXT NOT NULL,
    masked_client_id TEXT,
    account_purpose TEXT,
    starting_capital REAL,
    current_capital REAL,
    classification TEXT NOT NULL DEFAULT 'manual',
    active INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS tm_import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    broker_account_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    file_checksum TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    rows_processed INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    trades_created INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (broker_account_id) REFERENCES tm_broker_accounts(id)
);

-- One row per individual broker execution (a fill), kept as the immutable source-of-truth
-- record -- tm_trades rows are a derived, re-computable grouping of these, never the other
-- way round, so a mis-grouped trade can always be fixed by re-running the grouping logic
-- without losing data.
CREATE TABLE IF NOT EXISTS tm_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    broker_account_id INTEGER NOT NULL,
    import_job_id INTEGER,
    trade_id INTEGER,
    symbol TEXT NOT NULL,
    isin TEXT,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    broker_trade_id TEXT,
    broker_order_id TEXT,
    executed_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (broker_account_id) REFERENCES tm_broker_accounts(id),
    FOREIGN KEY (import_job_id) REFERENCES tm_import_jobs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tm_executions_fingerprint
    ON tm_executions(user_id, fingerprint);

CREATE TABLE IF NOT EXISTS tm_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    broker_account_id INTEGER NOT NULL,
    import_job_id INTEGER,
    symbol TEXT NOT NULL,
    isin TEXT,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    avg_exit_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT NOT NULL,
    gross_pnl REAL NOT NULL,
    charges REAL NOT NULL DEFAULT 0,
    net_pnl REAL NOT NULL,
    planned_sl REAL,
    planned_target REAL,
    planned_risk REAL,
    r_multiple REAL,
    setup TEXT,
    strategy TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'closed',
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (broker_account_id) REFERENCES tm_broker_accounts(id)
);

CREATE TABLE IF NOT EXISTS tm_trade_journal (
    trade_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    setup_followed INTEGER,
    sl_followed INTEGER,
    exit_plan_followed INTEGER,
    emotional_state TEXT,
    mistakes TEXT,
    what_went_well TEXT,
    what_went_wrong TEXT,
    lesson_learned TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES tm_trades(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS tm_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    rule_type TEXT NOT NULL,
    threshold_value REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS tm_rule_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    user_id INTEGER NOT NULL,
    rule_id INTEGER NOT NULL,
    rule_type TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    compliant INTEGER NOT NULL,
    detail TEXT,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES tm_trades(id),
    FOREIGN KEY (rule_id) REFERENCES tm_rules(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


def init_tm_schema():
    conn = _conn()
    try:
        conn.executescript(SCHEMA_TM)
        conn.commit()
    finally:
        conn.close()
