"""数字追溯：把交接文件里的每个数字并排查回三层事实。

* 原单层：未改写的 ``raw_*`` 记录（含被拒绝原件与冻结证据）。
* 当前事实层：重建后的采购归属、收货状态、库存桶与货位可用量。
* 使用层：该事实被哪次班前清单使用、对应承诺是否被取代、领用凭证与补救结果。
"""
from __future__ import annotations

import sqlite3

from .picking import remediation_queue
from .procurement import procurement_rows


def _dict_rows(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _in_clause(values: list[str]) -> tuple[str, list[str]]:
    if not values:
        return ("1 = 0", [])
    placeholders = ", ".join("?" for _ in values)
    return (f"IN ({placeholders})", values)


def trace_request(conn: sqlite3.Connection, request_id: str) -> dict[str, object]:
    """追溯一个采购请求：原单、采购行、收货/调拨经历、销欠与补单数字。"""
    raw_request = _dict_rows(
        conn, "SELECT * FROM raw_request WHERE request = ? ORDER BY arrival, raw_id",
        (request_id,),
    )
    if not raw_request:
        return {"request": request_id, "found": False}

    purchase_raw = _dict_rows(
        conn, "SELECT * FROM raw_purchase WHERE request = ? ORDER BY arrival, raw_id",
        (request_id,),
    )
    po_lines = sorted({row["po_line"] for row in purchase_raw})
    po_clause, po_params = _in_clause(po_lines)

    receipts: list[dict] = []
    for po_line in po_lines:
        receipts.extend(_dict_rows(
            conn,
            "SELECT * FROM raw_receipt WHERE po_line = ? ORDER BY arrival, raw_id",
            (po_line,),
        ))
    transfers = _dict_rows(
        conn, "SELECT * FROM raw_transfer WHERE request = ? ORDER BY arrival, raw_id",
        (request_id,),
    )
    current_lines = _dict_rows(
        conn, f"SELECT * FROM cur_purchase_line WHERE po_line {po_clause}", tuple(po_params),
    )
    assignments = _dict_rows(
        conn,
        "SELECT * FROM cur_purchase_assignment WHERE request = ? ORDER BY po_line",
        (request_id,),
    )
    current_receipts = _dict_rows(
        conn, f"SELECT * FROM cur_receipt WHERE po_line {po_clause}", tuple(po_params),
    )
    current_transfers = _dict_rows(
        conn, "SELECT * FROM cur_transfer WHERE request = ?", (request_id,),
    )
    freeze_keys = [*po_lines, request_id]
    freeze_clause, freeze_params = _in_clause(freeze_keys)
    freezes = _dict_rows(
        conn,
        f"SELECT * FROM freeze_event WHERE entity_key {freeze_clause}",
        tuple(freeze_params),
    )
    fact_row = next(
        (row for row in procurement_rows(conn) if row["request"] == request_id), None
    )
    return {
        "request": request_id,
        "found": True,
        "raw": {"request": raw_request, "purchase": purchase_raw,
                "receipts": receipts, "transfers": transfers},
        "current_fact": {
            "purchase_lines": current_lines,
            "assignments": assignments,
            "receipts": current_receipts,
            "transfers": current_transfers,
            "procurement_totals": fact_row,
        },
        "freezes": freezes,
    }


def trace_pick(conn: sqlite3.Connection, pick: str) -> dict[str, object]:
    """追溯一张拣配单：需求原件、当前需求、历次班次承诺、领用凭证与补救结果。"""
    raw_picks = _dict_rows(
        conn, "SELECT * FROM raw_pick WHERE pick = ? ORDER BY arrival, raw_id", (pick,),
    )
    if not raw_picks:
        return {"pick": pick, "found": False}
    raw_receipts = _dict_rows(
        conn, "SELECT * FROM raw_pick_receipt WHERE pick = ? ORDER BY arrival, raw_id", (pick,),
    )
    current_lines = _dict_rows(
        conn, "SELECT * FROM cur_pick_line WHERE pick = ? ORDER BY station, sku", (pick,),
    )
    promises = _dict_rows(
        conn,
        "SELECT p.*, b.evidence_kind AS bucket_evidence_kind, "
        "b.evidence_id AS bucket_evidence_id "
        "FROM shift_promise p LEFT JOIN stock_bucket b ON b.bucket_id = p.bucket_id "
        "WHERE p.pick = ? ORDER BY p.shift_id, p.station, p.sku",
        (pick,),
    )
    decisions = _dict_rows(
        conn, "SELECT * FROM shift_decision WHERE pick = ? ORDER BY shift_id, station, sku",
        (pick,),
    )
    issues = _dict_rows(
        conn, "SELECT * FROM issue_ledger WHERE pick = ? ORDER BY arrival, raw_id", (pick,),
    )
    holdings = _dict_rows(
        conn, "SELECT * FROM holding_ledger WHERE pick = ? ORDER BY holding_id", (pick,),
    )
    remediation = [row for row in remediation_queue(conn) if row["pick"] == pick]
    return {
        "pick": pick,
        "found": True,
        "raw": {"picks": raw_picks, "pick_receipts": raw_receipts},
        "current_fact": {"pick_lines": current_lines},
        "shift_usage": {"decisions": decisions, "promises": promises},
        "verified_outcome": {"issues": issues, "holdings": holdings,
                             "remediation": remediation},
    }
