"""履约数据仓命令行入口。

所有命令都可反复运行：导入按内容哈希幂等；更正通过追加原件后重建派生层生效；
班次发布与领用回执是持久事件，重复运行不会重复占用库存或重复入账。

典型流程::

    python -m fulfillment.cli init-db
    python -m fulfillment.cli import examples
    python -m fulfillment.cli rebuild
    python -m fulfillment.cli publish-shift --shift-id S1 --as-of 2026-10-05T08:00:00Z --through-arrival 3
    python -m fulfillment.cli register-receipts
    python -m fulfillment.cli status
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .ingest import SPECS_BY_FILENAME, import_directory, import_file, open_warehouse
from .picking import PickingError, publish_shift, register_pick_receipts, remediation_queue
from .procurement import procurement_rows
from .rebuild import rebuild
from .traceability import trace_request, trace_pick


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_init_db(args: argparse.Namespace) -> int:
    open_warehouse(args.db)
    print(f"数据库已就绪：{args.db}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    target = Path(args.path)
    if target.is_file():
        if target.name not in SPECS_BY_FILENAME:
            raise SystemExit(f"未识别的交接文件：{target.name}")
        report = {target.name: import_file(conn, target)}
    else:
        report = import_directory(conn, target)
    _print(report)
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    _print({"freeze": rebuild(conn)})
    return 0


def _cmd_publish_shift(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    try:
        manifest = publish_shift(
            conn, Path(args.output_dir), args.as_of, args.through_arrival, args.shift_id
        )
    except PickingError as exc:
        raise SystemExit(f"班次发布被拒绝：{exc}") from exc
    _print(manifest)
    return 0


def _cmd_register_receipts(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    _print(register_pick_receipts(conn))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    payload = {
        "raw_counts": {
            table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in ("raw_request", "raw_purchase", "raw_receipt", "raw_transfer",
                          "raw_pick", "raw_pick_receipt", "raw_rejected")
        },
        "procurement": procurement_rows(conn),
        "stock_buckets": [dict(row) for row in conn.execute(
            "SELECT station, sku, units_total, units_free, evidence_kind, evidence_id "
            "FROM stock_bucket ORDER BY station, sku, bucket_id"
        )],
        "freezes": [dict(row) for row in conn.execute(
            "SELECT entity_kind, entity_key, reason, evidence FROM freeze_event"
        )],
        "remediation": remediation_queue(conn),
        "issues": [dict(row) for row in conn.execute(
            "SELECT issue, pick, station, sku, units, action, status, reason, shift_id "
            "FROM issue_ledger ORDER BY arrival, raw_id"
        )],
        "shifts": [dict(row) for row in conn.execute(
            "SELECT shift_id, as_of, through_arrival, output_dir FROM shift_publication"
        )],
    }
    _print(payload)
    return 0


def _cmd_export_procurement(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    rows = procurement_rows(conn)
    if not rows:
        return 0
    with Path(args.output).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"采购履约交接文件已写出：{args.output}（{len(rows)} 行）")
    return 0


def _cmd_export_remediation(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    rows = remediation_queue(conn)
    with Path(args.output).open("w", encoding="utf-8", newline="") as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        else:
            stream.write("issue,pick,station,sku,shift_id,held_units,still_backed_units,"
                         "short_units,remediable_free_units,evidence\n")
    print(f"补救队列已写出：{args.output}（{len(rows)} 行）")
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    conn = open_warehouse(args.db)
    if args.kind == "request":
        _print(trace_request(conn, args.key))
    else:
        _print(trace_pick(conn, args.key))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自动补货履约数据仓")
    parser.add_argument("--db", default="data/warehouse.db", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库").set_defaults(func=_cmd_init_db)

    p_import = sub.add_parser("import", help="导入一个交接文件或整个交接目录（幂等）")
    p_import.add_argument("path")
    p_import.set_defaults(func=_cmd_import)

    sub.add_parser("rebuild", help="从原始层重建当前事实并登记冻结").set_defaults(func=_cmd_rebuild)

    p_pub = sub.add_parser("publish-shift", help="发布班前拣配与补货交接文件")
    p_pub.add_argument("--shift-id", required=True)
    p_pub.add_argument("--as-of", required=True, help="班前时刻（UTC，如 2026-10-05T08:00:00Z）")
    p_pub.add_argument("--through-arrival", required=True, type=int)
    p_pub.add_argument("--output-dir", default="out/shifts")
    p_pub.set_defaults(func=_cmd_publish_shift)

    sub.add_parser("register-receipts",
                   help="登记尚未入账的班组领用/退回回执（幂等）").set_defaults(
        func=_cmd_register_receipts)

    sub.add_parser("status", help="输出采购、库存、冻结、补救与班次的当前事实").set_defaults(
        func=_cmd_status)

    p_ep = sub.add_parser("export-procurement", help="导出采购履约 CSV")
    p_ep.add_argument("--output", default="out/procurement.csv")
    p_ep.set_defaults(func=_cmd_export_procurement)

    p_er = sub.add_parser("export-remediation", help="导出补救队列 CSV")
    p_er.add_argument("--output", default="out/remediation.csv")
    p_er.set_defaults(func=_cmd_export_remediation)

    p_trace = sub.add_parser("trace", help="并排查原单、当前事实与已核实补救结果")
    p_trace.add_argument("kind", choices=("request", "pick"))
    p_trace.add_argument("key")
    p_trace.set_defaults(func=_cmd_trace)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
