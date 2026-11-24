"""拣配班次发布、承诺版本管理与领用回执登记。

关键规则：

* 班前清单只纳入尚在领料期限内（``due >= as_of``）且未冻结的需求；按货位、
  业务优先级、领料期限、单号排序后逐桶（FIFO，按上架凭据）独占分配，**整单配齐**
  才发给班组，同一件库存不能同时留给两张单。
* 新班次发布时，尚未领走的旧承诺标记为 ``superseded``；班组若拿过期版本来领，
  明确拒绝并记录原因。班组对同一拣配单只能拿到一次仍可兑现的承诺。
* 已领取的行保留领用凭证：供给被更正（上架改隔离等）导致库存桶消失或不足时，
  以具体货位和短数进入补救队列；只有班组回执确认退回的货才能再分给别人。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class PickingError(ValueError):
    """班次发布或领用登记的业务拒绝。"""


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Allocation:
    evidence_kind: str
    evidence_id: str
    station: str
    sku: str
    units: int


# ---------------------------------------------------------------- 班次发布
def _candidate_demands(conn: sqlite3.Connection, as_of: str,
                       through_arrival: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT p.* FROM cur_pick_line p "
        "WHERE p.arrival <= ? AND p.due >= ? AND p.frozen = 0 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM issue_ledger l WHERE l.pick = p.pick "
        "AND l.action = 'collected' AND l.status = 'accepted') "
        "ORDER BY p.station, p.priority, p.due, p.pick, p.sku",
        (through_arrival, as_of),
    ).fetchall()
    return rows


def _point_in_time_buckets(conn: sqlite3.Connection, through_arrival: int) -> list[dict[str, object]]:
    """按接收批次序数重放出截至 ``through_arrival`` 的货位库存桶。

    取每个收货/调拨凭据在该时点已知的最高修订/最后状态；只有 available 且未冻结
    的数量入池。冻结判定复用当前事实层（冲突在任何时点都不可靠）。
    """
    frozen_receipts = {
        row["receipt"] for row in conn.execute("SELECT receipt FROM cur_receipt WHERE frozen = 1")
    }
    frozen_transfers = {
        row["transfer"] for row in conn.execute("SELECT transfer FROM cur_transfer WHERE frozen = 1")
    }
    frozen_po = {
        row["po_line"] for row in conn.execute("SELECT po_line FROM cur_purchase_line WHERE frozen = 1")
    }
    buckets: dict[tuple[str, str, str, str], dict[str, object]] = {}

    receipt_rows = conn.execute(
        "SELECT * FROM raw_receipt WHERE arrival <= ? ORDER BY arrival, raw_id",
        (through_arrival,),
    ).fetchall()
    latest_revision: dict[str, int] = {}
    for row in receipt_rows:
        latest_revision[row["receipt"]] = max(latest_revision.get(row["receipt"], 0), row["revision"])
    seen_receipt: set[str] = set()
    for row in receipt_rows:
        if row["revision"] != latest_revision[row["receipt"]] or row["receipt"] in seen_receipt:
            continue
        seen_receipt.add(row["receipt"])
        if row["state"] != "available" or row["receipt"] in frozen_receipts or row["po_line"] in frozen_po:
            continue
        key = ("receipt", row["receipt"], row["station"], row["sku"])
        buckets[key] = {
            "evidence_kind": "receipt", "evidence_id": row["receipt"],
            "station": row["station"], "sku": row["sku"],
            "units_total": row["units"], "raw_arrival": row["arrival"],
            "po_line": row["po_line"],
        }

    transfer_rows = conn.execute(
        "SELECT * FROM raw_transfer WHERE arrival <= ? ORDER BY arrival, raw_id",
        (through_arrival,),
    ).fetchall()
    last_transfer: dict[str, sqlite3.Row] = {}
    for row in transfer_rows:
        last_transfer[row["transfer"]] = row
    for transfer_id, row in last_transfer.items():
        if row["state"] != "available" or transfer_id in frozen_transfers:
            continue
        key = ("transfer", transfer_id, row["destination"], row["sku"])
        buckets[key] = {
            "evidence_kind": "transfer", "evidence_id": transfer_id,
            "station": row["destination"], "sku": row["sku"],
            "units_total": row["units"], "raw_arrival": row["arrival"],
            "po_line": None,
        }
    return list(buckets.values())


def _held_units_by_evidence(conn: sqlite3.Connection,
                            through_arrival: int | None = None
                            ) -> dict[tuple[str, str, str, str], int]:
    """班组已领走且未退回的数量（按上架凭据聚合），任何时点都不可再分配。"""
    held: dict[tuple[str, str, str, str], int] = defaultdict(int)
    if through_arrival is None:
        sql = (
            "SELECT evidence_kind, evidence_id, station, sku, "
            "SUM(units_held - units_returned) AS units "
            "FROM holding_ledger GROUP BY evidence_kind, evidence_id, station, sku"
        )
        params: tuple[object, ...] = ()
    else:
        sql = (
            "SELECT evidence_kind, evidence_id, station, sku, "
            "SUM(units_held - CASE WHEN return_arrival IS NOT NULL AND return_arrival <= ? "
            "THEN units_returned ELSE 0 END) AS units "
            "FROM holding_ledger WHERE arrival <= ? "
            "GROUP BY evidence_kind, evidence_id, station, sku"
        )
        params = (through_arrival, through_arrival)
    for row in conn.execute(sql, params):
        held[(row["evidence_kind"], row["evidence_id"], row["station"], row["sku"])] = max(row["units"], 0)
    return held


def _active_promised_by_evidence(conn: sqlite3.Connection,
                                 through_arrival: int | None = None
                                 ) -> dict[tuple[str, str, str, str], int]:
    """尚未被取代、尚未领走的承诺占用量（按上架凭据聚合）。"""
    promised: dict[tuple[str, str, str, str], int] = defaultdict(int)
    sql = (
        "SELECT p.evidence_kind, p.evidence_id, p.station, p.sku, SUM(p.units) AS units "
        "FROM shift_promise p JOIN shift_publication s ON s.shift_id = p.shift_id "
        "WHERE p.superseded = 0"
    )
    params: list[object] = []
    if through_arrival is not None:
        sql += " AND s.through_arrival <= ?"
        params.append(through_arrival)
    sql += " GROUP BY 1, 2, 3, 4"
    for row in conn.execute(sql, params):
        promised[(row["evidence_kind"], row["evidence_id"], row["station"], row["sku"])] += row["units"]
    return promised


def publish_shift(conn: sqlite3.Connection, output_dir: Path, as_of: str,
                  through_arrival: int, shift_id: str | None = None) -> dict[str, object]:
    """发布一次班前清单，写出机器可读交接文件并持久化承诺。"""
    _parse_ts(as_of)
    max_arrivals = [
        row["m"] for row in conn.execute(
            "SELECT MAX(arrival) AS m FROM ("
            "SELECT arrival FROM raw_request UNION ALL SELECT arrival FROM raw_purchase "
            "UNION ALL SELECT arrival FROM raw_receipt UNION ALL SELECT arrival FROM raw_transfer "
            "UNION ALL SELECT arrival FROM raw_pick UNION ALL SELECT arrival FROM raw_pick_receipt)"
        )
    ]
    max_arrival = max((item for item in max_arrivals if item is not None), default=0)
    if through_arrival > max_arrival:
        raise PickingError(f"through_arrival={through_arrival} 超出已知接收批次 {max_arrival}")
    shift_id = shift_id or f"S{_utc_now().replace('-', '').replace(':', '').replace('T', '-').rstrip('Z')}"
    if conn.execute("SELECT 1 FROM shift_publication WHERE shift_id = ?", (shift_id,)).fetchone():
        raise PickingError(f"班次号已存在：{shift_id}")

    demands = _candidate_demands(conn, as_of, through_arrival)
    # 按拣配单聚合，保证整单配齐才发布
    pick_lines: dict[str, list[sqlite3.Row]] = {}
    order: list[str] = []
    for row in demands:
        if row["pick"] not in pick_lines:
            pick_lines[row["pick"]] = []
            order.append(row["pick"])
        pick_lines[row["pick"]].append(row)

    # 时点库存桶：总量减去班组持有与未领走承诺，剩余才是本次可承诺量
    snapshot = _point_in_time_buckets(conn, through_arrival)
    held = _held_units_by_evidence(conn, through_arrival)
    promised = _active_promised_by_evidence(conn, through_arrival)
    for bucket in snapshot:
        key = (bucket["evidence_kind"], bucket["evidence_id"],
               bucket["station"], bucket["sku"])
        bucket["units_held"] = held.get(key, 0)
        bucket["units_promised"] = promised.get(key, 0)
        bucket["units_free"] = max(
            bucket["units_total"] - bucket["units_held"] - bucket["units_promised"], 0
        )

    def buckets_for(station: str, sku: str) -> list[dict[str, object]]:
        return sorted(
            (bucket for bucket in snapshot
             if bucket["station"] == station and bucket["sku"] == sku and bucket["units_free"] > 0),
            key=lambda item: (item["raw_arrival"], item["evidence_id"]),
        )

    decisions: list[dict[str, object]] = []
    allocations_by_pick: dict[str, list[Allocation]] = {}
    released_picks: set[str] = set()

    for pick in order:
        lines = pick_lines[pick]
        pick_allocations: list[Allocation] = []
        short = False
        short_reason = None
        for line in lines:
            need = line["units"]
            line_allocs: list[Allocation] = []
            for bucket in buckets_for(line["station"], line["sku"]):
                take = min(bucket["units_free"], need)
                line_allocs.append(Allocation(
                    evidence_kind=bucket["evidence_kind"],
                    evidence_id=bucket["evidence_id"],
                    station=line["station"],
                    sku=line["sku"],
                    units=take,
                ))
                bucket["units_free"] -= take
                need -= take
                if need == 0:
                    break
            promised_qty = line["units"] - need
            if need > 0:
                short = True
                short_reason = f"货位 {line['station']} 的 {line['sku']} 可承诺量不足"
            decisions.append({
                "shift_id": shift_id,
                "pick": pick,
                "station": line["station"],
                "sku": line["sku"],
                "demanded": line["units"],
                "promised": promised_qty,
                "status": "short" if need > 0 else "released",
                "reason": short_reason if need > 0 else None,
            })
            pick_allocations.extend(line_allocs)
        if not short:
            released_picks.add(pick)
            allocations_by_pick[pick] = pick_allocations

    # 短配单不落承诺；记录未纳入原因（过期/冻结）用于清单完整性。
    # 已领取的单由补救队列跟踪，不再出现在新班次的任何决策里。
    collected_picks = {
        row["pick"] for row in conn.execute(
            "SELECT DISTINCT pick FROM issue_ledger WHERE action = 'collected' AND status = 'accepted'"
        )
    }
    for row in conn.execute(
        "SELECT * FROM cur_pick_line WHERE arrival <= ? AND (due < ? OR frozen = 1)",
        (through_arrival, as_of),
    ).fetchall():
        if row["pick"] in collected_picks:
            continue
        decisions.append({
            "shift_id": shift_id,
            "pick": row["pick"],
            "station": row["station"],
            "sku": row["sku"],
            "demanded": row["units"],
            "promised": 0,
            "status": "frozen" if row["frozen"] else "expired_demand",
            "reason": "拣配行已冻结" if row["frozen"] else "已过领料期限",
        })

    # 尚未领走的旧承诺让位于本次新承诺
    superseded = conn.execute(
        "UPDATE shift_promise SET superseded = 1 "
        "WHERE superseded = 0 RETURNING promise_id"
    ).fetchall()

    conn.execute(
        "INSERT INTO shift_publication (shift_id, as_of, through_arrival, output_dir) "
        "VALUES (?, ?, ?, ?)",
        (shift_id, as_of, through_arrival, str(output_dir)),
    )
    for pick in released_picks:
        for allocation in allocations_by_pick[pick]:
            bucket = conn.execute(
                "SELECT bucket_id FROM stock_bucket WHERE evidence_kind = ? AND evidence_id = ? "
                "AND station = ? AND sku = ?",
                (allocation.evidence_kind, allocation.evidence_id,
                 allocation.station, allocation.sku),
            ).fetchone()
            conn.execute(
                "INSERT INTO shift_promise (shift_id, pick, station, sku, units, bucket_id, "
                "evidence_kind, evidence_id, superseded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (shift_id, pick, allocation.station, allocation.sku, allocation.units,
                 bucket["bucket_id"] if bucket else -1,
                 allocation.evidence_kind, allocation.evidence_id),
            )
    for decision in decisions:
        conn.execute(
            "INSERT INTO shift_decision (shift_id, pick, station, sku, demanded, promised, "
            "status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (decision["shift_id"], decision["pick"], decision["station"], decision["sku"],
             decision["demanded"], decision["promised"], decision["status"], decision["reason"]),
        )
    conn.commit()

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = _write_shift_files(conn, output_dir, shift_id, as_of, through_arrival,
                                  released_picks, decisions, superseded)
    return manifest


def _write_shift_files(conn: sqlite3.Connection, output_dir: Path, shift_id: str,
                       as_of: str, through_arrival: int, released_picks: set[str],
                       decisions: list[dict[str, object]],
                       superseded: list[sqlite3.Row]) -> dict[str, object]:
    pick_path = output_dir / f"{shift_id}_pick_handoff.csv"
    replenish_path = output_dir / f"{shift_id}_replenish_handoff.jsonl"
    manifest_path = output_dir / f"{shift_id}_manifest.json"

    with pick_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["shift_id", "pick", "station", "sku", "demanded_units",
                         "promised_units", "evidence_kind", "evidence_id", "status", "reason"])
        # 输出顺序与分配顺序一致：货位 → 业务优先级 → 领料期限 → 单号 → SKU → 凭据
        promise_rows = conn.execute(
            "SELECT p.*, c.priority AS line_priority, c.due AS line_due "
            "FROM shift_promise p "
            "JOIN cur_pick_line c ON c.pick = p.pick AND c.station = p.station AND c.sku = p.sku "
            "WHERE p.shift_id = ? "
            "ORDER BY p.station, c.priority, c.due, p.pick, p.sku, p.evidence_id",
            (shift_id,),
        ).fetchall()
        for row in promise_rows:
            writer.writerow([shift_id, row["pick"], row["station"], row["sku"], row["units"],
                             row["units"], row["evidence_kind"], row["evidence_id"],
                             "released", ""])
        decision_order = sorted(
            (decision for decision in decisions if decision["pick"] not in released_picks),
            key=lambda item: (item["station"], item["pick"], item["sku"]),
        )
        for decision in decision_order:
            writer.writerow([shift_id, decision["pick"], decision["station"], decision["sku"],
                             decision["demanded"], decision["promised"], "", "",
                             decision["status"], decision["reason"] or ""])

    # 补货交接：采购履约事实 + 冻结登记，供采购端使用同一批事实
    with replenish_path.open("w", encoding="utf-8") as stream:
        from .procurement import procurement_rows
        for row in procurement_rows(conn):
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        for row in conn.execute("SELECT * FROM freeze_event ORDER BY freeze_id"):
            stream.write(json.dumps({
                "freeze_id": row["freeze_id"],
                "entity_kind": row["entity_kind"],
                "entity_key": row["entity_key"],
                "reason": row["reason"],
                "evidence_raw_ids": row["evidence"],
            }, ensure_ascii=False) + "\n")

    manifest = {
        "shift_id": shift_id,
        "as_of": as_of,
        "through_arrival": through_arrival,
        "generated_at": _utc_now(),
        "released_picks": sorted(released_picks),
        "superseded_promise_ids": [row["promise_id"] for row in superseded],
        "files": {
            "pick_handoff": pick_path.name,
            "replenish_handoff": replenish_path.name,
        },
        "source_max_arrivals": {
            table: conn.execute(f"SELECT COALESCE(MAX(arrival), 0) AS m FROM {table}").fetchone()["m"]
            for table in ("raw_request", "raw_purchase", "raw_receipt",
                          "raw_transfer", "raw_pick", "raw_pick_receipt")
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------- 领用回执
def _current_promise_lines(conn: sqlite3.Connection, pick: str) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM shift_promise WHERE pick = ? AND superseded = 0", (pick,)
    ).fetchall()
    # 同一行可能跨多个桶，聚合
    aggregated: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["station"], row["sku"])
        item = aggregated.setdefault(key, {
            "shift_id": row["shift_id"], "units": 0,
            "evidence": [],
        })
        item["units"] += row["units"]
        item["evidence"].append((row["evidence_kind"], row["evidence_id"], row["units"]))
    return aggregated  # type: ignore[return-value]


def register_pick_receipts(conn: sqlite3.Connection) -> dict[str, int]:
    """处理尚未入账的班组回执（按 arrival、raw_id 顺序），可反复运行。"""
    accepted = rejected = already = 0
    rows = conn.execute(
        "SELECT * FROM raw_pick_receipt r "
        "WHERE NOT EXISTS (SELECT 1 FROM issue_ledger l WHERE l.raw_id = r.raw_id) "
        "ORDER BY arrival, raw_id"
    ).fetchall()
    for row in rows:
        result = _register_one(conn, row)
        if result == "accepted":
            accepted += 1
        elif result == "duplicate":
            already += 1
        else:
            rejected += 1
    conn.commit()
    return {"accepted": accepted, "rejected": rejected, "duplicate": already}


def _register_one(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    key = (row["station"], row["sku"])
    if row["action"] == "collected":
        promises = _current_promise_lines(conn, row["pick"])
        promise = promises.get(key)
        if promise is None:
            _reject(conn, row, None, "拣配行没有可兑现的当前承诺（可能已被更正或过期）")
            return "rejected"
        if promise["units"] < row["units"]:
            _reject(conn, row, promise["shift_id"],
                    f"当前承诺仅 {promise['units']} 件，领取 {row['units']} 件，拒绝过期/超额版本")
            return "rejected"
        # 同一 issue 同一行重复 collected 视为重复提交，只入账一次
        dup = conn.execute(
            "SELECT 1 FROM issue_ledger WHERE issue = ? AND pick = ? AND station = ? AND sku = ? "
            "AND action = 'collected' AND status = 'accepted'",
            (row["issue"], row["pick"], row["station"], row["sku"]),
        ).fetchone()
        if dup:
            return "duplicate"
        remaining = row["units"]
        for evidence_kind, evidence_id, units in promise["evidence"]:
            if remaining <= 0:
                break
            take = min(units, remaining)
            bucket = conn.execute(
                "SELECT bucket_id FROM stock_bucket WHERE evidence_kind = ? AND evidence_id = ? "
                "AND station = ? AND sku = ?",
                (evidence_kind, evidence_id, row["station"], row["sku"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO issue_ledger (issue, pick, station, sku, units, action, shift_id, "
                "arrival, status, raw_id) VALUES (?, ?, ?, ?, ?, 'collected', ?, ?, 'accepted', ?)",
                (row["issue"], row["pick"], row["station"], row["sku"], take,
                 promise["shift_id"], row["arrival"], row["raw_id"]),
            )
            conn.execute(
                "INSERT INTO holding_ledger (issue, pick, station, sku, units_held, "
                "units_returned, shift_id, bucket_id, evidence_kind, evidence_id, arrival) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (row["issue"], row["pick"], row["station"], row["sku"], take,
                 promise["shift_id"], bucket["bucket_id"] if bucket else -1,
                 evidence_kind, evidence_id, row["arrival"]),
            )
            remaining -= take
        return "accepted"

    # returned：必须有已领取且未退回完的持有
    holdings = conn.execute(
        "SELECT * FROM holding_ledger WHERE issue = ? AND pick = ? AND station = ? AND sku = ? "
        "ORDER BY holding_id",
        (row["issue"], row["pick"], row["station"], row["sku"]),
    ).fetchall()
    open_holdings = [item for item in holdings if item["units_held"] - item["units_returned"] > 0]
    open_units = sum(item["units_held"] - item["units_returned"] for item in open_holdings)
    if not open_holdings or open_units < row["units"]:
        _reject(conn, row, None,
                f"退回 {row['units']} 件但未退回持有仅 {open_units} 件，无班组外的货可分配")
        return "rejected"
    remaining = row["units"]
    for holding in open_holdings:
        if remaining <= 0:
            break
        take = min(holding["units_held"] - holding["units_returned"], remaining)
        conn.execute(
            "UPDATE holding_ledger SET units_returned = units_returned + ?, return_arrival = ? "
            "WHERE holding_id = ?",
            (take, row["arrival"], holding["holding_id"]),
        )
        conn.execute(
            "INSERT INTO issue_ledger (issue, pick, station, sku, units, action, shift_id, "
            "arrival, status, raw_id) VALUES (?, ?, ?, ?, ?, 'returned', ?, ?, 'accepted', ?)",
            (row["issue"], row["pick"], row["station"], row["sku"], take,
             holding["shift_id"], row["arrival"], row["raw_id"]),
        )
        remaining -= take
    return "accepted"


def _reject(conn: sqlite3.Connection, row: sqlite3.Row, shift_id: str | None,
            reason: str) -> None:
    conn.execute(
        "INSERT INTO issue_ledger (issue, pick, station, sku, units, action, shift_id, "
        "arrival, status, reason, raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?)",
        (row["issue"], row["pick"], row["station"], row["sku"], row["units"],
         row["action"], shift_id, row["arrival"], reason, row["raw_id"]),
    )


# ---------------------------------------------------------------- 补救队列
def remediation_queue(conn: sqlite3.Connection,
                      through_arrival: int | None = None) -> list[dict[str, object]]:
    """已领取行的当前短数与可补救量；供给更正导致的欠量在此显式暴露。

    持有的每件货都记着上架凭据。若更正把该凭据改成隔离/冻结、或桶总量降到持有
    余量以下，对应持有量即失去兑现依据，按货位形成短数。只有班组回执确认退回
    （``holding_ledger.units_returned``）的货才会回到可用池。

    ``through_arrival`` 给定时按该批次时点重放上架事实，用于复盘某次班前的补救视图；
    缺省取全部已知批次的当前事实。
    """
    if through_arrival is None:
        through_arrival = conn.execute(
            "SELECT COALESCE(MAX(arrival), 0) AS m FROM ("
            "SELECT arrival FROM raw_receipt UNION ALL SELECT arrival FROM raw_transfer)"
        ).fetchone()["m"]

    # 时点上每个凭据仍可兑现的总量（available、未冻结、最高修订/最后状态）
    snapshot = {
        (bucket["evidence_kind"], bucket["evidence_id"],
         bucket["station"], bucket["sku"]): bucket["units_total"]
        for bucket in _point_in_time_buckets(conn, through_arrival)
    }
    # 只统计该时点之前已经发生的领用；退回若晚于时点则不计入（货仍在班组手里）
    holding_rows = conn.execute(
        "SELECT issue, pick, station, sku, shift_id, evidence_kind, evidence_id, arrival, "
        "units_held, "
        "CASE WHEN return_arrival IS NOT NULL AND return_arrival <= ? "
        "THEN units_returned ELSE 0 END AS returned "
        "FROM holding_ledger h WHERE h.arrival <= ? "
        "ORDER BY pick, station, sku",
        (through_arrival, through_arrival),
    ).fetchall()
    # 同一拣配行可能持有多个凭据、跨多次领取，先按行聚合。
    # committed = 领取时承诺给班组的基线（后续退回只改净持有，不改基线）；
    # backed    = 这些凭据在当前时点仍能支撑该行的量。
    lines: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for holding in holding_rows:
        key = (holding["issue"], holding["pick"], holding["station"], holding["sku"])
        line = lines.setdefault(key, {
            "issue": holding["issue"], "pick": holding["pick"],
            "station": holding["station"], "sku": holding["sku"],
            "shift_id": holding["shift_id"],
            "committed": 0, "net_held": 0, "backed": 0, "evidence": [],
        })
        committed = holding["units_held"]
        returned = holding["returned"]
        net = committed - returned
        line["committed"] += committed
        line["net_held"] += net
        if net <= 0:
            continue
        evidence_key = (holding["evidence_kind"], holding["evidence_id"],
                        holding["station"], holding["sku"])
        capacity = snapshot.get(evidence_key, 0)
        # 同一凭据上若还有未领走的活跃承诺许诺给别人，那部分不能再算作本行保障
        other_promised = conn.execute(
            "SELECT COALESCE(SUM(p.units), 0) AS units FROM shift_promise p "
            "JOIN shift_publication s ON s.shift_id = p.shift_id "
            "WHERE p.evidence_kind = ? AND p.evidence_id = ? AND p.station = ? AND p.sku = ? "
            "AND p.superseded = 0 AND p.pick != ? AND s.through_arrival <= ?",
            (holding["evidence_kind"], holding["evidence_id"],
             holding["station"], holding["sku"], holding["pick"], through_arrival),
        ).fetchone()["units"]
        backing = max(capacity - other_promised, 0)
        line["backed"] += min(net, backing)
        if capacity == 0:
            line["evidence"].append(
                f"{holding['evidence_kind']}:{holding['evidence_id']}=UNAVAILABLE"
            )
        else:
            line["evidence"].append(
                f"{holding['evidence_kind']}:{holding['evidence_id']}="
                f"capacity{capacity},promised_other{other_promised}"
            )

    result = []
    held_map = _held_units_by_evidence(conn, through_arrival)
    promised_map = _active_promised_by_evidence(conn, through_arrival)
    for line in lines.values():
        # 短数 = 班组仍持有的量中，凭据已无法支撑的部分（退回的货已不在班组手里）
        short = max(line["net_held"] - line["backed"], 0)
        free_pool = 0
        if short:
            held_at_station = sum(
                units for (kind, evidence, station, sku), units in held_map.items()
                if station == line["station"] and sku == line["sku"]
            )
            promised_at_station = sum(
                units for (kind, evidence, station, sku), units in promised_map.items()
                if station == line["station"] and sku == line["sku"]
            )
            total_at_station = sum(
                capacity for (kind, evidence, station, sku), capacity in snapshot.items()
                if station == line["station"] and sku == line["sku"]
            )
            free_pool = max(total_at_station - held_at_station - promised_at_station, 0)
        result.append({
            "issue": line["issue"],
            "pick": line["pick"],
            "station": line["station"],
            "sku": line["sku"],
            "shift_id": line["shift_id"],
            "committed_units": line["committed"],
            "held_units": line["net_held"],
            "still_backed_units": min(line["backed"], line["net_held"]),
            "short_units": short,
            "remediable_free_units": min(short, free_pool),
            "evidence": "|".join(line["evidence"]),
        })
    return result
