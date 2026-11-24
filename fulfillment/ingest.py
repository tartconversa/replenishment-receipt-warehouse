"""交接文件导入：结构校验、原件留痕与幂等写入。

导入只做结构层面的校验（表头、类型、枚举、时间格式）。业务冲突（同一键出现
互相矛盾的记录、引用了不存在的实体等）不在导入阶段判定，统一交给
:mod:`fulfillment.rebuild` 冻结，保证原件始终完整保留。

同一条记录重复导入（文件格式或转发人不影响规范化内容）按内容哈希识别并跳过；
结构不合格的记录写入 ``raw_rejected``，同样按哈希幂等。
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .schema import connect, initialize


class StructuralReject(ValueError):
    """一条原件未通过结构校验。"""


@dataclass(frozen=True)
class FieldSpec:
    column: str
    kind: str  # int / text / enum / timestamp
    enum: tuple[str, ...] = ()
    minimum: int | None = None


@dataclass(frozen=True)
class SourceSpec:
    name: str
    table: str
    filenames: tuple[str, ...]
    fields: tuple[FieldSpec, ...]


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "requests", "raw_request", ("requests.csv", "requests.tsv", "requests.jsonl"),
        (
            FieldSpec("arrival", "int", minimum=1),
            FieldSpec("request", "text"),
            FieldSpec("station", "text"),
            FieldSpec("sku", "text"),
            FieldSpec("units", "int", minimum=0),
            FieldSpec("state", "enum", ("sent", "timeout")),
        ),
    ),
    SourceSpec(
        "purchase", "raw_purchase", ("purchase.csv", "purchase.tsv", "purchase.jsonl"),
        (
            FieldSpec("arrival", "int", minimum=1),
            FieldSpec("po_line", "text"),
            FieldSpec("revision", "int", minimum=1),
            FieldSpec("request", "text"),
            FieldSpec("requested_units", "int", minimum=0),
            FieldSpec("state", "enum", ("confirmed", "canceled")),
            FieldSpec("forwarder", "text"),
        ),
    ),
    SourceSpec(
        "receipts", "raw_receipt", ("receipts.csv", "receipts.tsv", "receipts.jsonl"),
        (
            FieldSpec("arrival", "int", minimum=1),
            FieldSpec("receipt", "text"),
            FieldSpec("revision", "int", minimum=1),
            FieldSpec("po_line", "text"),
            FieldSpec("station", "text"),
            FieldSpec("sku", "text"),
            FieldSpec("units", "int", minimum=0),
            FieldSpec("state", "enum", ("available", "hold", "refused")),
        ),
    ),
    SourceSpec(
        "transfers", "raw_transfer", ("transfers.csv", "transfers.tsv", "transfers.jsonl"),
        (
            FieldSpec("arrival", "int", minimum=1),
            FieldSpec("transfer", "text"),
            FieldSpec("request", "text"),
            FieldSpec("origin", "text"),
            FieldSpec("destination", "text"),
            FieldSpec("sku", "text"),
            FieldSpec("units", "int", minimum=0),
            FieldSpec("state", "enum", ("in_transit", "available")),
        ),
    ),
    SourceSpec(
        "picks", "raw_pick", ("picks.csv", "picks.tsv", "picks.jsonl"),
        (
            FieldSpec("arrival", "int", minimum=1),
            FieldSpec("pick", "text"),
            FieldSpec("station", "text"),
            FieldSpec("sku", "text"),
            FieldSpec("units", "int", minimum=1),
            FieldSpec("priority", "int", minimum=0),
            FieldSpec("due", "timestamp"),
        ),
    ),
    SourceSpec(
        "pick_receipts", "raw_pick_receipt",
        ("pick_receipts.csv", "pick_receipts.tsv", "pick_receipts.jsonl"),
        (
            FieldSpec("arrival", "int", minimum=1),
            FieldSpec("issue", "text"),
            FieldSpec("pick", "text"),
            FieldSpec("station", "text"),
            FieldSpec("sku", "text"),
            FieldSpec("units", "int", minimum=0),
            FieldSpec("action", "enum", ("collected", "returned")),
        ),
    ),
)

SPECS_BY_FILENAME = {
    filename: spec for spec in SOURCES for filename in spec.filenames
}
SPECS_BY_TABLE = {spec.table: spec for spec in SOURCES}


# 转发人/记录人不是业务身份：同一事实被多人转发时，去重哈希必须剔除这些列，
# 否则“同一外部单行被多人转发只算一次”的口径无法在原始层成立。
NON_IDENTITY_COLUMNS = {"forwarder"}


def _content_hash(payload: dict[str, str]) -> str:
    identity_payload = {
        key: value for key, value in payload.items() if key not in NON_IDENTITY_COLUMNS
    }
    canonical = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise StructuralReject("时刻必须为 UTC（以 Z 结尾，零偏移）")
    return value


def _coerce(spec: FieldSpec, raw_value: str) -> int | str:
    if raw_value is None or raw_value == "":
        raise StructuralReject(f"字段 {spec.column} 为空")
    if spec.kind == "text":
        return raw_value
    if spec.kind == "timestamp":
        return _parse_timestamp(raw_value)
    if spec.kind == "enum":
        if raw_value not in spec.enum:
            raise StructuralReject(f"字段 {spec.column} 取值非法：{raw_value}")
        return raw_value
    try:
        number = int(raw_value)
    except ValueError as exc:
        raise StructuralReject(f"字段 {spec.column} 不是整数：{raw_value}") from exc
    if spec.minimum is not None and number < spec.minimum:
        raise StructuralReject(f"字段 {spec.column} 小于最小值 {spec.minimum}")
    return number


def read_raw_records(path: Path) -> list[dict[str, str]]:
    """读取 CSV/TSV/JSONL 原件并做表头层面的结构检查。"""
    with path.open(encoding="utf-8", newline="") as stream:
        if path.suffix == ".jsonl":
            records = [json.loads(line) for line in stream if line.strip()]
            if any(not isinstance(record, dict) for record in records):
                raise StructuralReject(f"{path.name} 包含非对象记录")
            return [{key: str(value) for key, value in row.items()} for row in records]
        delimiter = "\t" if path.suffix == ".tsv" else ","
        reader = csv.DictReader(stream, delimiter=delimiter)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise StructuralReject(f"{path.name} 表头无效或有重复列")
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise StructuralReject(f"{path.name} 存在列数与表头不符的记录")
        return [{key: (value if value is not None else "") for key, value in row.items()} for row in rows]


def _validate_row(spec: SourceSpec, row: dict[str, str]) -> dict[str, int | str]:
    columns = {field.column for field in spec.fields}
    if set(row) != columns:
        raise StructuralReject(f"列集合不匹配：得到 {sorted(row)}，期望 {sorted(columns)}")
    return {field.column: _coerce(field, row[field.column]) for field in spec.fields}


def _record_reject(conn: sqlite3.Connection, spec: SourceSpec, arrival: int | None,
                   payload: dict[str, str], reason: str) -> bool:
    digest = _content_hash(payload)
    cursor = conn.execute(
        "SELECT 1 FROM raw_rejected WHERE content_hash = ?", (digest,)
    )
    if cursor.fetchone():
        return False
    conn.execute(
        "INSERT INTO raw_rejected (source, arrival, content_hash, payload, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (spec.name, arrival, digest, json.dumps(payload, ensure_ascii=False), reason),
    )
    return True


def import_file(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    """导入单个交接文件，返回 ``inserted / duplicate / rejected`` 计数。"""
    spec = SPECS_BY_FILENAME.get(path.name)
    if spec is None:
        raise ValueError(f"未识别的交接文件名：{path.name}")
    rows = read_raw_records(path)
    columns = [field.column for field in spec.fields]
    placeholders = ", ".join("?" for _ in columns)
    inserted = duplicate = rejected = 0
    for row in rows:
        digest = _content_hash(row)
        if conn.execute(
            f"SELECT 1 FROM {spec.table} WHERE content_hash = ?", (digest,)
        ).fetchone():
            duplicate += 1
            continue
        try:
            clean = _validate_row(spec, row)
        except StructuralReject as exc:
            arrival_raw = row.get("arrival")
            try:
                arrival_value = int(arrival_raw) if arrival_raw not in (None, "") else None
            except ValueError:
                arrival_value = None
            if _record_reject(conn, spec, arrival_value, row, str(exc)):
                rejected += 1
            continue
        conn.execute(
            f"INSERT INTO {spec.table} ({', '.join(columns)}, content_hash) "
            f"VALUES ({placeholders}, ?)",
            [clean[column] for column in columns] + [digest],
        )
        inserted += 1
    conn.commit()
    return {"inserted": inserted, "duplicate": duplicate, "rejected": rejected}


def import_directory(conn: sqlite3.Connection, directory: Path) -> dict[str, dict[str, int]]:
    """导入目录下所有已识别交接文件，未识别文件直接跳过。"""
    if not directory.is_dir():
        raise FileNotFoundError(f"交接目录不存在：{directory}")
    report = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name in SPECS_BY_FILENAME:
            report[path.name] = import_file(conn, path)
    return report


def open_warehouse(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(str(db_path))
    initialize(conn)
    return conn
