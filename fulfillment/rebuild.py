"""从原始层完整重建当前事实层。

重建是确定性的、可反复运行的：每次清空派生表后按 ``arrival`` 与原件顺序重放。
无法可靠归属的冲突按最小影响面冻结，并在 ``freeze_event`` 留痕；冻结只影响
被点名的供给与需求，其他货位照常出单。持久层（班次、领用）不在此过程中改写，
库存桶的可用量会结合持有的最新状态重新计算。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

DERIVED_TABLES = (
    "cur_request",
    "cur_purchase_line",
    "cur_purchase_assignment",
    "cur_receipt",
    "cur_transfer",
    "stock_bucket",
    "cur_pick_line",
    "freeze_event",
)


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} ORDER BY arrival, raw_id"
    ).fetchall()


def _freeze(conn: sqlite3.Connection, kind: str, key: str, reason: str,
            evidence: list[int]) -> None:
    conn.execute(
        "INSERT INTO freeze_event (entity_kind, entity_key, reason, evidence) "
        "VALUES (?, ?, ?, ?)",
        (kind, key, reason, ",".join(str(item) for item in evidence)),
    )


def rebuild(conn: sqlite3.Connection) -> dict[str, int]:
    """重建全部派生表，返回各实体的冻结数量统计。"""
    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")

    frozen_requests: set[str] = set()
    frozen_po_lines: set[str] = set()
    frozen_receipts: set[str] = set()
    frozen_transfers: set[str] = set()
    frozen_pick_lines: set[tuple[str, str, str]] = set()

    requests = _rows(conn, "raw_request")
    purchases = _rows(conn, "raw_purchase")
    receipts = _rows(conn, "raw_receipt")
    transfers = _rows(conn, "raw_transfer")
    picks = _rows(conn, "raw_pick")

    known_requests = {row["request"] for row in requests}
    request_by_id = {row["request"]: row for row in requests}

    # ------------------------------------------------------------ 采购请求
    seen_request: dict[str, sqlite3.Row] = {}
    request_conflicts: dict[str, list[int]] = defaultdict(list)
    for row in requests:
        prior = seen_request.get(row["request"])
        if prior is None:
            seen_request[row["request"]] = row
        elif tuple(prior[field] for field in ("station", "sku", "units", "state")) != tuple(
            row[field] for field in ("station", "sku", "units", "state")
        ):
            request_conflicts[row["request"]].append(row["raw_id"])
    for request_id, evidence in request_conflicts.items():
        frozen_requests.add(request_id)
        _freeze(conn, "request", request_id, "同一请求出现互相矛盾的原件", evidence)
    for request_id, row in seen_request.items():
        conn.execute(
            "INSERT INTO cur_request (request, station, sku, units, state, arrival, frozen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, row["station"], row["sku"], row["units"], row["state"],
             row["arrival"], int(request_id in frozen_requests)),
        )

    # ------------------------------------------------------------ 采购确认
    # 同一 (po_line, revision) 的多份原件：转发人不是身份，内容一致只去重一次。
    # 同一外部行可以正常拆分给多个请求，也可以合并回复多个请求；只有同一请求在
    # 同一修订下出现互相矛盾的数量/状态，或归属 SKU 不一致，才冻结整行。
    po_revision_owners: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    for row in purchases:
        po_revision_owners[(row["po_line"], row["revision"])].append(row)

    po_latest_revision: dict[str, int] = {}
    po_freeze_evidence: dict[str, list[int]] = defaultdict(list)
    po_revision_skus: dict[tuple[str, int], str] = {}
    for (po_line, revision), rows in po_revision_owners.items():
        po_latest_revision[po_line] = max(po_latest_revision.get(po_line, revision), revision)
        per_request: dict[str, set[tuple[int, str]]] = defaultdict(set)
        for row in rows:
            per_request[row["request"]].add((row["requested_units"], row["state"]))
        if any(len(signatures) > 1 for signatures in per_request.values()):
            frozen_po_lines.add(po_line)
            po_freeze_evidence[po_line].extend(row["raw_id"] for row in rows)
            continue
        skus = {request_by_id[row["request"]]["sku"]
                for row in rows if row["request"] in request_by_id}
        if len(skus) > 1:
            frozen_po_lines.add(po_line)
            po_freeze_evidence[po_line].extend(row["raw_id"] for row in rows)
            continue
        if skus:
            po_revision_skus[(po_line, revision)] = next(iter(skus))
        for row in rows:
            if row["request"] not in known_requests:
                frozen_po_lines.add(po_line)
                po_freeze_evidence[po_line].append(row["raw_id"])

    # 当前修订：取每个 po_line 的最高 revision；旧修订全部被业务修订覆盖。
    assignments: dict[tuple[str, str], dict[str, object]] = {}
    po_skus: dict[str, str] = {}
    for (po_line, revision), rows in po_revision_owners.items():
        if revision != po_latest_revision[po_line]:
            continue
        for row in rows:
            key = (po_line, row["request"])
            # 同一 (po_line, request) 的重复转发已在上一阶段按签名去重；
            # 真矛盾会冻结整行，这里保留首份归属即可。
            if key not in assignments:
                assignments[key] = {
                    "state": row["state"],
                    "requested_units": row["requested_units"],
                }
        if (po_line, revision) in po_revision_skus:
            po_skus[po_line] = po_revision_skus[(po_line, revision)]

    for po_line, evidence in po_freeze_evidence.items():
        _freeze(conn, "purchase_line", po_line, "采购行转发内容冲突或引用未知请求",
                sorted(set(evidence)))

    for po_line, revision in po_latest_revision.items():
        conn.execute(
            "INSERT INTO cur_purchase_line (po_line, sku, latest_revision, frozen) "
            "VALUES (?, ?, ?, ?)",
            (po_line, po_skus.get(po_line), revision, int(po_line in frozen_po_lines)),
        )
    for (po_line, request_id), item in assignments.items():
        conn.execute(
            "INSERT INTO cur_purchase_assignment (po_line, request, state, requested_units) "
            "VALUES (?, ?, ?, ?)",
            (po_line, request_id, item["state"], item["requested_units"]),
        )

    # ------------------------------------------------------------ 收货变动
    receipt_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in receipts:
        receipt_groups[row["receipt"]].append(row)

    current_receipt: dict[str, sqlite3.Row] = {}
    receipt_conflicts: dict[str, list[int]] = defaultdict(list)
    for receipt_id, rows in receipt_groups.items():
        latest = max(row["revision"] for row in rows)
        latest_rows = [row for row in rows if row["revision"] == latest]
        # 同一修订号下的重复件必须一致；不同 revision 是正常的业务更正序列
        signatures = {
            (row["po_line"], row["station"], row["sku"], row["units"], row["state"])
            for row in latest_rows
        }
        if len(signatures) > 1:
            receipt_conflicts[receipt_id].extend(row["raw_id"] for row in latest_rows)
        current_receipt[receipt_id] = latest_rows[0]

    for receipt_id, row in current_receipt.items():
        is_frozen = receipt_id in receipt_conflicts
        po_line = conn.execute(
            "SELECT po_line, frozen FROM cur_purchase_line WHERE po_line = ?",
            (row["po_line"],),
        ).fetchone()
        reason = None
        if po_line is None:
            reason = "收货引用了不存在的采购行"
            is_frozen = True
        elif po_line["frozen"]:
            reason = "收货关联的采购行已冻结"
            is_frozen = True
        else:
            sku_row = conn.execute(
                "SELECT sku FROM cur_purchase_line WHERE po_line = ?", (row["po_line"],)
            ).fetchone()
            if sku_row and sku_row["sku"] and sku_row["sku"] != row["sku"]:
                reason = "收货 SKU 与采购行归属不一致"
                is_frozen = True
        if is_frozen:
            frozen_receipts.add(receipt_id)
            evidence = receipt_conflicts.get(receipt_id, [row["raw_id"]])
            _freeze(conn, "receipt", receipt_id,
                    reason or "同一收货凭据的当前修订互相矛盾", evidence)
        conn.execute(
            "INSERT INTO cur_receipt (receipt, latest_revision, po_line, station, sku, "
            "units, state, frozen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt_id, row["revision"], row["po_line"], row["station"], row["sku"],
             row["units"], row["state"], int(is_frozen)),
        )

    # ------------------------------------------------------------ 调拨
    transfer_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in transfers:
        transfer_groups[row["transfer"]].append(row)
    current_transfer: dict[str, sqlite3.Row] = {}
    transfer_conflicts: dict[str, list[int]] = defaultdict(list)
    for transfer_id, rows in transfer_groups.items():
        last_seen: dict[str, sqlite3.Row] = {}
        for row in rows:  # 同状态重复到达只去重，状态推进按最后一次
            prior = last_seen.get(row["state"])
            if prior is not None and tuple(
                prior[field] for field in ("request", "origin", "destination", "sku", "units")
            ) != tuple(row[field] for field in ("request", "origin", "destination", "sku", "units")):
                transfer_conflicts[transfer_id].append(row["raw_id"])
            last_seen[row["state"]] = row
        current_transfer[transfer_id] = rows[-1]

    for transfer_id, row in current_transfer.items():
        is_frozen = transfer_id in transfer_conflicts
        reason = None
        if row["request"] not in known_requests:
            reason = "调拨引用了不存在的采购请求"
            is_frozen = True
        elif request_by_id[row["request"]]["sku"] != row["sku"]:
            reason = "调拨 SKU 与请求不一致"
            is_frozen = True
        if is_frozen:
            frozen_transfers.add(transfer_id)
            _freeze(conn, "transfer", transfer_id,
                    reason or "同一调拨凭据出现互相矛盾的原件",
                    transfer_conflicts.get(transfer_id, [row["raw_id"]]))
        conn.execute(
            "INSERT INTO cur_transfer (transfer, request, origin, destination, sku, units, "
            "state, frozen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, row["request"], row["origin"], row["destination"], row["sku"],
             row["units"], row["state"], int(is_frozen)),
        )

    # ------------------------------------------------------------ 班组需求
    pick_groups: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in picks:
        pick_groups[(row["pick"], row["station"], row["sku"])].append(row)
    for key, rows in pick_groups.items():
        signatures = {(row["units"], row["priority"], row["due"]) for row in rows}
        current = max(rows, key=lambda item: (item["arrival"], item["raw_id"]))
        is_frozen = len(signatures) > 1
        if is_frozen:
            frozen_pick_lines.add(key)
            _freeze(conn, "pick_line", "|".join(key),
                    "同一拣配行出现互相矛盾的原件",
                    [row["raw_id"] for row in rows])
        conn.execute(
            "INSERT INTO cur_pick_line (pick, station, sku, units, priority, due, arrival, frozen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key[0], key[1], key[2], current["units"], current["priority"],
             current["due"], current["arrival"], int(is_frozen)),
        )

    _build_stock_buckets(conn)

    conn.commit()
    return {
        "requests": len(frozen_requests),
        "purchase_lines": len(frozen_po_lines),
        "receipts": len(frozen_receipts),
        "transfers": len(frozen_transfers),
        "pick_lines": len(frozen_pick_lines),
    }


def _build_stock_buckets(conn: sqlite3.Connection) -> None:
    """根据当前事实构建库存桶，并结合持久持有量计算逐桶可用量。

    只有验收合格且完成上架（receipt.state = available）或调拨已入位
    （transfer.state = available）且未冻结的数量进入货位池；hold、refused、
    in_transit 与冻结数量都不可用。同凭据同货位同 SKU 的合格数量合并为一个桶，
    保证逐件可追溯到上架凭据。
    """
    held_by_evidence: dict[tuple[str, str, str, str], int] = defaultdict(int)
    holding_rows = conn.execute(
        "SELECT evidence_kind, evidence_id, station, sku, "
        "SUM(units_held - units_returned) AS held "
        "FROM holding_ledger GROUP BY evidence_kind, evidence_id, station, sku"
    ).fetchall()
    for row in holding_rows:
        held_by_evidence[(row["evidence_kind"], row["evidence_id"],
                          row["station"], row["sku"])] = max(row["held"], 0)

    # 已发布且未被取代的承诺同样独占库存，发布新班次时必须视为不可再分配
    promised_by_evidence: dict[tuple[str, str, str, str], int] = defaultdict(int)
    promise_rows = conn.execute(
        "SELECT evidence_kind, evidence_id, station, sku, SUM(units) AS units "
        "FROM shift_promise WHERE superseded = 0 "
        "GROUP BY evidence_kind, evidence_id, station, sku"
    ).fetchall()
    for row in promise_rows:
        promised_by_evidence[(row["evidence_kind"], row["evidence_id"],
                              row["station"], row["sku"])] += row["units"]

    bucket_seq = 0

    def add_bucket(kind: str, evidence_id: str, station: str, sku: str,
                   units: int, po_line: str | None, arrival: int) -> None:
        nonlocal bucket_seq
        bucket_seq += 1
        key = (kind, evidence_id, station, sku)
        occupied = held_by_evidence.get(key, 0) + promised_by_evidence.get(key, 0)
        conn.execute(
            "INSERT INTO stock_bucket (bucket_id, station, sku, units_total, units_free, "
            "evidence_kind, evidence_id, po_line, raw_arrival) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bucket_seq, station, sku, units, max(units - occupied, 0),
             kind, evidence_id, po_line, arrival),
        )

    receipt_rows = conn.execute(
        "SELECT r.*, raw.arrival FROM cur_receipt r "
        "JOIN (SELECT receipt, MAX(arrival) AS arrival FROM raw_receipt GROUP BY receipt) raw "
        "ON raw.receipt = r.receipt "
        "WHERE r.frozen = 0 AND r.state = 'available' ORDER BY r.receipt"
    ).fetchall()
    for row in receipt_rows:
        add_bucket("receipt", row["receipt"], row["station"], row["sku"],
                   row["units"], row["po_line"], row["arrival"])

    transfer_rows = conn.execute(
        "SELECT t.*, raw.arrival FROM cur_transfer t "
        "JOIN (SELECT transfer, MAX(arrival) AS arrival FROM raw_transfer GROUP BY transfer) raw "
        "ON raw.transfer = t.transfer "
        "WHERE t.frozen = 0 AND t.state = 'available' ORDER BY t.transfer"
    ).fetchall()
    for row in transfer_rows:
        add_bucket("transfer", row["transfer"], row["destination"], row["sku"],
                   row["units"], None, row["arrival"])
