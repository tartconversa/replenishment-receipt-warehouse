#!/usr/bin/env bash
# 可反复运行的端到端演示：清空库后按接收批次重放完整业务周期。
#
# 时间线（arrival 见 examples/ 各文件）：
#   S1  截至批次 3 的班前清单 → 班组领取 W3
#   批次 4/5 采购更正与收货修订（G711 从 12 改为 7，另隔离 5）
#   S2  截至批次 5 重评未领需求；已领的 K31 进入补救队列（短 1）
#   批次 6 新上架 G714、调拨 T03 入位、班组退回 2 件
#   S3  截至批次 6 重新配齐 K32/K33/K34，K31 短数随退回清零
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DB="${DB:-data/warehouse.db}"
OUT="${OUT:-out/demo}"
rm -f "$DB"
rm -rf "$OUT"
mkdir -p data

python -I run.py --db "$DB" init-db
python -I run.py --db "$DB" import examples
python -I run.py --db "$DB" rebuild

python -I run.py --db "$DB" publish-shift --shift-id S1 \
  --as-of 2026-10-05T08:00:00Z --through-arrival 3 --output-dir "$OUT/shifts"
python -I run.py --db "$DB" register-receipts
python -I run.py --db "$DB" rebuild

python -I run.py --db "$DB" publish-shift --shift-id S2 \
  --as-of 2026-10-05T09:30:00Z --through-arrival 5 --output-dir "$OUT/shifts"

python -I run.py --db "$DB" publish-shift --shift-id S3 \
  --as-of 2026-10-05T09:45:00Z --through-arrival 6 --output-dir "$OUT/shifts"
python -I run.py --db "$DB" register-receipts
python -I run.py --db "$DB" rebuild

python -I run.py --db "$DB" export-procurement --output "$OUT/procurement.csv"
python -I run.py --db "$DB" export-remediation --output "$OUT/remediation.csv"

echo
echo "演示完成。机器可读交接文件位于 $OUT："
find "$OUT" -type f | sort
