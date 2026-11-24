"""采购视角的履约事实：销欠、待核实与补单建议。

口径（与 ``docs/exchanges.md`` 对齐）：

* 只有具备明确行级对应的**合格未冻结收货**（``state=available``）才能销欠；
  同一外部单行被多人转发只算一次（导入去重 + 重建冻结保证）。
* 一个外部行服务多个请求时，合格收货按请求号升序消化；购买表同一行的请求归属
  也按请求号升序排列。
* ``timeout`` 只代表连接器未见回执，供应商仍可能接受，其未确认余量单列
  「待核实」，绝不进入可补单量。
* 只有明确取消或明确拒收、且没有其他有效保障的缺量才建议补单；补单量还要
  扣减已确认调拨与已发出结果未知的同一采购需求余量。
* 已冻结的采购行不参与任何数字，只进入冻结视图。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RequestFact:
    request: str
    station: str
    sku: str
    target_units: int
    connector_state: str
    confirmed_units: int = 0
    canceled_units: int = 0
    refused_units: int = 0
    qualified_received: int = 0
    backordered: int = 0
    reorderable: int = 0
    pending_verification: int = 0
    confirmed_transfer_units: int = 0
    in_flight_unknown: int = 0
    reorderable_canceled: int = 0
    reorderable_refused: int = 0
    po_lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _qualified_receipts_by_po(conn: sqlite3.Connection) -> dict[str, int]:
    """每个采购行当前合格、未冻结、已上架的收货总量（修订取当前版本）。"""
    totals: dict[str, int] = defaultdict(int)
    rows = conn.execute(
        "SELECT po_line, units FROM cur_receipt WHERE frozen = 0 AND state = 'available'"
    ).fetchall()
    for row in rows:
        totals[row["po_line"]] += row["units"]
    return totals


def _refused_by_po(conn: sqlite3.Connection) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    rows = conn.execute(
        "SELECT po_line, units FROM cur_receipt WHERE frozen = 0 AND state = 'refused'"
    ).fetchall()
    for row in rows:
        totals[row["po_line"]] += row["units"]
    return totals


def build_request_facts(conn: sqlite3.Connection) -> list[RequestFact]:
    """汇总每个采购请求当前可追溯的履约事实。"""
    receipts_available = _qualified_receipts_by_po(conn)
    receipts_refused = _refused_by_po(conn)

    assignments = conn.execute(
        "SELECT a.po_line, a.request, a.state, a.requested_units, l.frozen AS line_frozen "
        "FROM cur_purchase_assignment a "
        "JOIN cur_purchase_line l ON l.po_line = a.po_line "
        "ORDER BY a.po_line, a.request"
    ).fetchall()

    # 按采购行汇总当前修订下的请求归属（行级对应）
    line_requests: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in assignments:
        if not row["line_frozen"]:
            line_requests[row["po_line"]].append(row)

    # 合格收货在同一采购行的请求间按请求号升序消化
    qualified_by_request: dict[str, int] = defaultdict(int)
    refused_by_request: dict[str, int] = defaultdict(int)
    for po_line in sorted(line_requests):
        rows = sorted(line_requests[po_line], key=lambda item: item["request"])
        confirmed_rows = [item for item in rows if item["state"] == "confirmed"]
        supply_remaining = receipts_available.get(po_line, 0)
        for item in confirmed_rows:
            take = min(supply_remaining, item["requested_units"])
            qualified_by_request[item["request"]] += take
            supply_remaining -= take
        # 拒收量同样按请求号升序归属到已确认请求，作为「明确拒收」证据
        refused_remaining = receipts_refused.get(po_line, 0)
        for item in confirmed_rows:
            take = min(refused_remaining, item["requested_units"])
            refused_by_request[item["request"]] += take
            refused_remaining -= take

    facts: dict[str, RequestFact] = {}
    for row in conn.execute("SELECT * FROM cur_request ORDER BY request"):
        facts[row["request"]] = RequestFact(
            request=row["request"],
            station=row["station"],
            sku=row["sku"],
            target_units=row["units"],
            connector_state=row["state"],
        )

    for po_line, rows in line_requests.items():
        for item in rows:
            fact = facts.get(item["request"])
            if fact is None:
                continue
            fact.po_lines.append(po_line)
            if item["state"] == "confirmed":
                fact.confirmed_units += item["requested_units"]
            else:
                fact.canceled_units += item["requested_units"]

    for request_id, units in qualified_by_request.items():
        if request_id in facts:
            facts[request_id].qualified_received = units
    for request_id, units in refused_by_request.items():
        if request_id in facts:
            facts[request_id].refused_units = units

    # 已确认调拨（含在途与已入位，未冻结）——它是对该采购需求的有效内部保障
    transfer_rows = conn.execute(
        "SELECT request, units, state FROM cur_transfer WHERE frozen = 0"
    ).fetchall()
    transfers_by_request: dict[str, int] = defaultdict(int)
    for row in transfer_rows:
        transfers_by_request[row["request"]] += row["units"]

    for fact in facts.values():
        fact.confirmed_transfer_units = transfers_by_request.get(fact.request, 0)
        # 已发起但结果未知：目标量中尚未收到任何采购确认（confirmed 或 canceled）的部分
        answered = fact.confirmed_units + fact.canceled_units
        fact.in_flight_unknown = max(fact.target_units - answered, 0)
        fact.pending_verification = fact.in_flight_unknown
        # 欠交：已确认仍未形成合格收货的余量（hold/refused/在途都不销欠）
        fact.backordered = max(fact.confirmed_units - fact.qualified_received, 0)

        # 可补单只承认两类「明确落空且无其他保障」的缺量：
        #   1) 供应商明确取消的余量；2) 明确拒收且未被后续合格收货覆盖的余量。
        # 仍处于已确认状态的欠交（hold、在途、延迟上架等）有在途保障，不能补单。
        # 每类都要再扣减已确认调拨（内部保障）与结果未知余量（可能被供应商接受）。
        def _net_reorderable(gross_gap: int) -> int:
            return max(gross_gap - fact.confirmed_transfer_units - fact.in_flight_unknown, 0)

        fact.reorderable_canceled = _net_reorderable(fact.canceled_units)
        # 拒收若已被同一请求的其他合格收货覆盖，则不再形成缺量
        refused_uncovered = max(
            fact.refused_units
            - max(fact.qualified_received - (fact.confirmed_units - fact.refused_units), 0),
            0,
        )
        fact.reorderable_refused = _net_reorderable(refused_uncovered)
        fact.reorderable = fact.reorderable_canceled + fact.reorderable_refused

        if fact.connector_state == "timeout" and fact.pending_verification:
            fact.notes.append("连接器未见回执，供应商仍可能接受，余量待核实不得补单")
        if fact.backordered and fact.reorderable == 0:
            fact.notes.append("欠交仍有在途或内部保障，暂不建议补单")

    return sorted(facts.values(), key=lambda item: item.request)


def procurement_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """机器可读的采购履约行（补货交接文件的数据源）。"""
    rows = []
    for fact in build_request_facts(conn):
        rows.append({
            "request": fact.request,
            "station": fact.station,
            "sku": fact.sku,
            "target_units": fact.target_units,
            "connector_state": fact.connector_state,
            "confirmed_units": fact.confirmed_units,
            "canceled_units": fact.canceled_units,
            "refused_units": fact.refused_units,
            "qualified_received": fact.qualified_received,
            "backordered": fact.backordered,
            "confirmed_transfer_units": fact.confirmed_transfer_units,
            "in_flight_unknown": fact.in_flight_unknown,
            "pending_verification": fact.pending_verification,
            "reorderable_canceled": fact.reorderable_canceled,
            "reorderable_refused": fact.reorderable_refused,
            "reorderable_units": fact.reorderable,
            "po_lines": "|".join(sorted(set(fact.po_lines))),
            "notes": "；".join(fact.notes),
        })
    return rows
