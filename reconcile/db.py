"""SQLite 连接与建表。所有金额以 TEXT 存储十进制字符串，不使用浮点。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 导入批次
CREATE TABLE IF NOT EXISTS import_batch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    import_time     TEXT NOT NULL,
    file_type       TEXT NOT NULL CHECK (file_type IN ('order','payment','refund')),
    file_name       TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'imported',
    UNIQUE (file_type, file_sha256)
);

-- 原始订单（按订单号保留首次导入的权威版本）
CREATE TABLE IF NOT EXISTS raw_order (
    order_id        TEXT PRIMARY KEY,
    currency        TEXT NOT NULL,
    receivable      TEXT NOT NULL,
    source_line     TEXT,
    first_batch_id  INTEGER NOT NULL REFERENCES import_batch(id)
);

CREATE TABLE IF NOT EXISTS order_conflict (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL,
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    payload         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 支付事件：按流水号保留首次导入的权威版本
CREATE TABLE IF NOT EXISTS payment_event (
    txn_id          TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    event_time      TEXT NOT NULL,
    source_line     TEXT,
    first_batch_id  INTEGER NOT NULL REFERENCES import_batch(id)
);

-- 退款事件：主键自增（同原支付可对应多笔部分退款）
CREATE TABLE IF NOT EXISTS refund_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_txn_id   TEXT NOT NULL,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    event_time      TEXT NOT NULL,
    source_line     TEXT,
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    content_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refund_origin ON refund_event(origin_txn_id);

-- 流水号去重与冲突（支付、退款共用）
CREATE TABLE IF NOT EXISTS event_conflict (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id          TEXT NOT NULL,
    event_type      TEXT NOT NULL CHECK (event_type IN ('payment','refund')),
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    canonical       TEXT,
    duplicate       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflict_txn ON event_conflict(txn_id);

-- 导入行级错误
CREATE TABLE IF NOT EXISTS import_row_error (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    file_type       TEXT NOT NULL,
    row_number      INTEGER NOT NULL,
    raw_line        TEXT NOT NULL,
    error           TEXT NOT NULL
);

-- 订单对账结果（版本化，version 递增，只有 current=1 参与当前页面）
CREATE TABLE IF NOT EXISTS recon_result (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            TEXT NOT NULL,
    version             INTEGER NOT NULL,
    current             INTEGER NOT NULL CHECK (current IN (0,1)),
    currency            TEXT NOT NULL,
    receivable          TEXT NOT NULL,
    gross_paid          TEXT NOT NULL,
    valid_refund        TEXT NOT NULL,
    excess_refund       TEXT NOT NULL,
    net_received        TEXT NOT NULL,
    difference          TEXT NOT NULL,
    status              TEXT NOT NULL,
    reasons             TEXT NOT NULL,
    settlement_date     TEXT,
    canonical_snapshot  TEXT NOT NULL,
    change_note         TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (order_id, version)
);
CREATE INDEX IF NOT EXISTS idx_result_current ON recon_result(current, settlement_date, status);
CREATE INDEX IF NOT EXISTS idx_result_order   ON recon_result(order_id, version);

-- 对账计算明细（每笔有效支付/退款一行，可回溯原始记录）
CREATE TABLE IF NOT EXISTS recon_detail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id       INTEGER NOT NULL REFERENCES recon_result(id),
    order_id        TEXT NOT NULL,
    version         INTEGER NOT NULL,
    line_type       TEXT NOT NULL CHECK (line_type IN ('payment','refund','summary')),
    event_ref       TEXT,
    txn_id          TEXT,
    ref_txn_id      TEXT,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    event_time      TEXT,
    settlement_date TEXT,
    included        INTEGER NOT NULL,
    note            TEXT,
    source_line     TEXT
);
CREATE INDEX IF NOT EXISTS idx_detail_result ON recon_detail(result_id);

-- 结算日汇总（版本化；跨日退款会在退款发生日期产生调整行）
CREATE TABLE IF NOT EXISTS daily_settlement (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_date     TEXT NOT NULL,
    version             INTEGER NOT NULL,
    current             INTEGER NOT NULL CHECK (current IN (0,1)),
    currency            TEXT NOT NULL,
    order_count         INTEGER NOT NULL,
    match_count         INTEGER NOT NULL,
    mismatch_count      INTEGER NOT NULL DEFAULT 0,
    exception_count     INTEGER NOT NULL,
    receivable_total    TEXT NOT NULL,
    net_total           TEXT NOT NULL,
    adjustment_total    TEXT NOT NULL DEFAULT '0',
    canonical_snapshot  TEXT NOT NULL,
    change_note         TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (settlement_date, version, currency)
);
CREATE INDEX IF NOT EXISTS idx_daily_current ON daily_settlement(current, settlement_date);

-- 挂账类异常（无法直接归属某订单）
CREATE TABLE IF NOT EXISTS standalone_exception (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_type  TEXT NOT NULL,
    ref_type        TEXT NOT NULL,
    ref_id          TEXT NOT NULL,
    order_id        TEXT,
    amount          TEXT,
    currency        TEXT,
    event_time      TEXT,
    reason          TEXT NOT NULL,
    source_line     TEXT,
    batch_id        INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_standalone_type ON standalone_exception(exception_type);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
