"""导入层：结构校验、内容哈希幂等、拒绝留痕。"""
from __future__ import annotations

import json

from _base import WarehouseTestCase, EXAMPLES
from fulfillment.ingest import import_directory, import_file


class IngestTest(WarehouseTestCase):
    def test_all_supplied_files_import_once(self):
        report = import_directory(self.conn, EXAMPLES)
        self.assertTrue(report)
        self.assertEqual(sum(item["rejected"] for item in report.values()), 0)
        totals = sum(item["inserted"] + item["duplicate"] for item in report.values())
        # 再次导入全部应判为重复，不产生新原件
        again = import_directory(self.conn, EXAMPLES)
        self.assertEqual(sum(item["inserted"] for item in again.values()), 0)
        self.assertEqual(sum(item["duplicate"] for item in again.values()), totals)

    def test_duplicate_forward_is_single_raw_record(self):
        # P180-2 rev1 被采购甲/采购乙各转发一次，内容一致，只允许一条原件
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM raw_purchase WHERE po_line = 'P180-2' AND revision = 1"
        ).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_structural_reject_is_kept_not_silently_dropped(self):
        bad = self.tmp / "requests.csv"
        bad.write_text(
            "arrival,request,station,sku,units,state\n"
            "9,R99,Z-01,PEN,2,sent\n"
            "9,R98,Z-01,PEN,-1,sent\n"
            "9,R97,Z-01,PEN,3,unknown\n",
            encoding="utf-8",
        )
        report = import_file(self.conn, bad)
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(report["rejected"], 2)
        rejected = self.conn.execute("SELECT source, reason FROM raw_rejected").fetchall()
        self.assertEqual({row["source"] for row in rejected}, {"requests"})
        # 幂等：重跑拒绝件不重复登记
        import_file(self.conn, bad)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM raw_rejected").fetchone()["c"], 2
        )

    def test_jsonl_and_tsv_are_supported(self):
        tsv = self.tmp / "requests.tsv"
        tsv.write_text(
            "arrival\trequest\tstation\tsku\tunits\tstate\n"
            "7\tR77\tQ-09\tGLUE\t1\tsent\n",
            encoding="utf-8",
        )
        report = import_file(self.conn, tsv)
        self.assertEqual(report["inserted"], 1)
        jsonl = self.tmp / "picks.jsonl"
        jsonl.write_text(json.dumps({
            "arrival": 7, "pick": "K77", "station": "Q-09", "sku": "GLUE",
            "units": 1, "priority": 1, "due": "2026-10-05T09:00:00Z",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(import_file(self.conn, jsonl)["inserted"], 1)
