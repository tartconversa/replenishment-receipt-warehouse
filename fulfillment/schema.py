"""履约数据仓的 SQLite 模式。

数据分三层：

1. 原始层 ``raw_*``：只追加的交接原件，按内容哈希去重，任何业务逻辑都不改写它。
2. 派生层 ``cur_*`` / ``dim_*``：每次从原始层完整重建的当前事实，可随时丢弃重算。
3. 持久层 ``shift_*`` / ``issue_*``：班次发布与领用回执产生的事件与承诺，重跑不重复生效。
"""
from __future__ import annotations

import sqlite3

SCHEMA_STATEMENTS = [
    # ------------------------------------------------------------------ 原始层
    """
    CREATE TABLE IF NOT EXISTS raw_request (
        raw_id      INTEGER PRIMARY KEY,
        arrival     INTEGER NOT NULL,
        request     TEXT NOT NULL,
        station     TEXT NOT NULL,
        sku         TEXT NOT NULL,
        units       INTEGER NOT NULL CHECK (units >= 0),
        state       TEXT NOT NULL CHECK (state IN ('sent', 'timeout')),
        content_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_purchase (
        raw_id       INTEGER PRIMARY KEY,
        arrival      INTEGER NOT NULL,
        po_line      TEXT NOT NULL,
        revision     INTEGER NOT NULL CHECK (revision > 0),
        request      TEXT NOT NULL,
        requested_units INTEGER NOT NULL CHECK (requested_units >= 0),
        state        TEXT NOT NULL CHECK (state IN ('confirmed', 'canceled')),
        forwarder    TEXT NOT NULL,
        content_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_receipt (
        raw_id       INTEGER PRIMARY KEY,
        arrival      INTEGER NOT NULL,
        receipt      TEXT NOT NULL,
        revision     INTEGER NOT NULL CHECK (revision > 0),
        po_line      TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL CHECK (units >= 0),
        state        TEXT NOT NULL CHECK (state IN ('available', 'hold', 'refused')),
        content_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_transfer (
        raw_id       INTEGER PRIMARY KEY,
        arrival      INTEGER NOT NULL,
        transfer     TEXT NOT NULL,
        request      TEXT NOT NULL,
        origin       TEXT NOT NULL,
        destination  TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL CHECK (units >= 0),
        state        TEXT NOT NULL CHECK (state IN ('in_transit', 'available')),
        content_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_pick (
        raw_id       INTEGER PRIMARY KEY,
        arrival      INTEGER NOT NULL,
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL CHECK (units > 0),
        priority     INTEGER NOT NULL,
        due          TEXT NOT NULL,
        content_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_pick_receipt (
        raw_id       INTEGER PRIMARY KEY,
        arrival      INTEGER NOT NULL,
        issue        TEXT NOT NULL,
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL CHECK (units >= 0),
        action       TEXT NOT NULL CHECK (action IN ('collected', 'returned')),
        content_hash TEXT NOT NULL UNIQUE
    )
    """,
    # 导入时被结构/语义校验拒绝的原件，保留以便追溯
    """
    CREATE TABLE IF NOT EXISTS raw_rejected (
        raw_id       INTEGER PRIMARY KEY,
        source       TEXT NOT NULL,
        arrival      INTEGER,
        content_hash TEXT NOT NULL UNIQUE,
        payload      TEXT NOT NULL,
        reason       TEXT NOT NULL
    )
    """,
    # ------------------------------------------------------------------ 冻结登记
    """
    CREATE TABLE IF NOT EXISTS freeze_event (
        freeze_id    INTEGER PRIMARY KEY,
        entity_kind  TEXT NOT NULL,          -- purchase_line / receipt / transfer / pick_line / request
        entity_key   TEXT NOT NULL,
        reason       TEXT NOT NULL,
        evidence     TEXT NOT NULL,          -- 关联的 raw_id 列表
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    # ------------------------------------------------------------------ 派生：采购请求
    """
    CREATE TABLE IF NOT EXISTS cur_request (
        request      TEXT PRIMARY KEY,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL,
        state        TEXT NOT NULL,
        arrival      INTEGER NOT NULL,
        frozen       INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ------------------------------------------------------------------ 派生：采购
    """
    CREATE TABLE IF NOT EXISTS cur_purchase_line (
        po_line      TEXT PRIMARY KEY,
        sku          TEXT,
        latest_revision INTEGER NOT NULL,
        frozen       INTEGER NOT NULL DEFAULT 0
    )
    """,
    # 每个采购行在当前修订下的请求归属（confirmed / canceled 都保留，销欠只用 confirmed）
    """
    CREATE TABLE IF NOT EXISTS cur_purchase_assignment (
        po_line      TEXT NOT NULL,
        request      TEXT NOT NULL,
        state        TEXT NOT NULL,
        requested_units INTEGER NOT NULL,
        PRIMARY KEY (po_line, request)
    )
    """,
    # ------------------------------------------------------------------ 派生：收货与货位库存池
    """
    CREATE TABLE IF NOT EXISTS cur_receipt (
        receipt      TEXT PRIMARY KEY,
        latest_revision INTEGER NOT NULL,
        po_line      TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL,
        state        TEXT NOT NULL,
        frozen       INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cur_transfer (
        transfer     TEXT PRIMARY KEY,
        request      TEXT NOT NULL,
        origin       TEXT NOT NULL,
        destination  TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL,
        state        TEXT NOT NULL,
        frozen       INTEGER NOT NULL DEFAULT 0
    )
    """,
    # 可用于指定货位拣配的库存桶；每个桶可逐件追溯到上架凭据
    """
    CREATE TABLE IF NOT EXISTS stock_bucket (
        bucket_id    INTEGER PRIMARY KEY,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units_total  INTEGER NOT NULL,
        units_free   INTEGER NOT NULL,
        evidence_kind TEXT NOT NULL,         -- receipt / transfer
        evidence_id  TEXT NOT NULL,
        po_line      TEXT,
        raw_arrival  INTEGER NOT NULL
    )
    """,
    # ------------------------------------------------------------------ 派生：需求
    """
    CREATE TABLE IF NOT EXISTS cur_pick_line (
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL,
        priority     INTEGER NOT NULL,
        due          TEXT NOT NULL,
        arrival      INTEGER NOT NULL,
        frozen       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pick, station, sku)
    )
    """,
    # ------------------------------------------------------------------ 持久：班次与承诺
    """
    CREATE TABLE IF NOT EXISTS shift_publication (
        shift_id     TEXT PRIMARY KEY,
        as_of        TEXT NOT NULL,
        through_arrival INTEGER NOT NULL,
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        output_dir   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shift_promise (
        promise_id   INTEGER PRIMARY KEY,
        shift_id     TEXT NOT NULL,
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL,
        bucket_id    INTEGER NOT NULL,
        evidence_kind TEXT NOT NULL,
        evidence_id  TEXT NOT NULL,
        superseded   INTEGER NOT NULL DEFAULT 0,
        UNIQUE (shift_id, pick, station, sku, bucket_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shift_decision (
        shift_id     TEXT NOT NULL,
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        demanded     INTEGER NOT NULL,
        promised     INTEGER NOT NULL,
        status       TEXT NOT NULL,          -- released / short / expired_demand / frozen
        reason       TEXT,
        PRIMARY KEY (shift_id, pick, station, sku)
    )
    """,
    # ------------------------------------------------------------------ 持久：领用凭证与补救
    """
    CREATE TABLE IF NOT EXISTS issue_ledger (
        issue        TEXT NOT NULL,
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units        INTEGER NOT NULL,
        action       TEXT NOT NULL,
        shift_id     TEXT,
        arrival      INTEGER NOT NULL,
        status       TEXT NOT NULL,          -- accepted / rejected
        reason       TEXT,
        raw_id       INTEGER NOT NULL,
        PRIMARY KEY (raw_id)
    )
    """,
    # 已领取行级承诺的当前状态；同一件库存只能被一张单持有
    """
    CREATE TABLE IF NOT EXISTS holding_ledger (
        holding_id   INTEGER PRIMARY KEY,
        issue        TEXT NOT NULL,
        pick         TEXT NOT NULL,
        station      TEXT NOT NULL,
        sku          TEXT NOT NULL,
        units_held   INTEGER NOT NULL,
        units_returned INTEGER NOT NULL DEFAULT 0,
        shift_id     TEXT NOT NULL,
        bucket_id    INTEGER NOT NULL,
        evidence_kind TEXT NOT NULL,
        evidence_id  TEXT NOT NULL,
        arrival      INTEGER NOT NULL,
        return_arrival INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bucket ON stock_bucket(station, sku)",
    "CREATE INDEX IF NOT EXISTS idx_promise_pick ON shift_promise(pick)",
    "CREATE INDEX IF NOT EXISTS idx_holding_pick ON holding_ledger(pick)",
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
