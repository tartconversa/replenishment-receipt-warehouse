"""派生层重建：修订覆盖、冲突冻结、库存桶可用性。"""
from __future__ import annotations

from _base import WarehouseTestCase


class RebuildTest(WarehouseTestCase):
    def test_unknown_request_purchase_line_is_frozen_only_for_that_line(self):
        freezes = {
            (row["entity_kind"], row["entity_key"])
            for row in self.conn.execute("SELECT entity_kind, entity_key FROM freeze_event")
        }
        # P500-1 引用了请求表里不存在的 R22：冻结该行，但不波及其他采购行
        self.assertIn(("purchase_line", "P500-1"), freezes)
        self.assertNotIn(("purchase_line", "P193-1"), freezes)
        self.assertNotIn(("purchase_line", "P180-1"), freezes)

    def test_split_and_merge_confirmations_are_not_conflicts(self):
        # P193-1 同时确认 R18(4) 与 R20(5) 是合法的行级拆分/合并
        rows = self.conn.execute(
            "SELECT request, requested_units FROM cur_purchase_assignment "
            "WHERE po_line = 'P193-1' ORDER BY request"
        ).fetchall()
        self.assertEqual([(row["request"], row["requested_units"]) for row in rows],
                         [("R18", 4), ("R20", 5)])

    def test_higher_revision_replaces_older_one(self):
        # P180-2 rev2 canceled 覆盖 rev1 confirmed
        line = self.conn.execute(
            "SELECT latest_revision FROM cur_purchase_line WHERE po_line = 'P180-2'"
        ).fetchone()
        self.assertEqual(line["latest_revision"], 2)
        assignments = self.conn.execute(
            "SELECT state, requested_units FROM cur_purchase_assignment WHERE po_line = 'P180-2'"
        ).fetchall()
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["state"], "canceled")

    def test_only_available_unfrozen_stock_enters_bucket(self):
        buckets = {
            (row["evidence_kind"], row["evidence_id"]): row
            for row in self.conn.execute("SELECT * FROM stock_bucket")
        }
        self.assertIn(("receipt", "G711"), buckets)       # available 修订后 7 件
        self.assertEqual(buckets[("receipt", "G711")]["units_total"], 7)
        self.assertIn(("receipt", "G714"), buckets)       # 9 件 available
        self.assertNotIn(("receipt", "G713"), buckets)    # refused 不入池
        self.assertNotIn(("receipt", "G711-R"), buckets)  # hold 不入池
        # 调拨入位后入池，in_transit 阶段两端都不可用（同一 transfer 的早期状态被覆盖）
        self.assertEqual(buckets[("transfer", "T03")]["units_total"], 2)

    def test_rebuild_is_deterministic_and_repeatable(self):
        first = [dict(row) for row in self.conn.execute(
            "SELECT station, sku, units_total, evidence_kind, evidence_id FROM stock_bucket ORDER BY 1,2,3,4,5"
        )]
        self.rebuild()
        second = [dict(row) for row in self.conn.execute(
            "SELECT station, sku, units_total, evidence_kind, evidence_id FROM stock_bucket ORDER BY 1,2,3,4,5"
        )]
        self.assertEqual(first, second)

    def test_conflicting_same_revision_freezes_line(self):
        self.conn.execute(
            "INSERT INTO raw_purchase (arrival, po_line, revision, request, requested_units, "
            "state, forwarder, content_hash) VALUES "
            "(8, 'P900-1', 1, 'R17', 5, 'confirmed', '甲', 'h-a'), "
            "(8, 'P900-1', 1, 'R17', 9, 'confirmed', '乙', 'h-b')"
        )
        self.conn.commit()
        self.rebuild()
        frozen = self.conn.execute(
            "SELECT 1 FROM freeze_event WHERE entity_kind = 'purchase_line' AND entity_key = 'P900-1'"
        ).fetchone()
        self.assertIsNotNone(frozen)
        # 冻结行不产生库存桶关联（其若有收货也一并冻结）
        self.assertTrue(self.conn.execute(
            "SELECT frozen FROM cur_purchase_line WHERE po_line = 'P900-1'"
        ).fetchone()["frozen"])
