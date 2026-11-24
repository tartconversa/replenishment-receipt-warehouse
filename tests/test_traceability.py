"""追溯：原单、当前事实、班次使用与补救结果的并排链路。"""
from __future__ import annotations

from _base import WarehouseTestCase
from fulfillment.picking import publish_shift, register_pick_receipts
from fulfillment.rebuild import rebuild
from fulfillment.traceability import trace_pick, trace_request


class TraceabilityTest(WarehouseTestCase):
    def _full_timeline(self):
        publish_shift(self.conn, self.tmp / "shifts",
                      "2026-10-05T08:00:00Z", 3, "S1")
        register_pick_receipts(self.conn)
        rebuild(self.conn)
        publish_shift(self.conn, self.tmp / "shifts",
                      "2026-10-05T09:30:00Z", 5, "S2")
        publish_shift(self.conn, self.tmp / "shifts",
                      "2026-10-05T09:45:00Z", 6, "S3")
        register_pick_receipts(self.conn)
        rebuild(self.conn)

    def test_request_trace_links_raw_to_current_and_freezes(self):
        view = trace_request(self.conn, "R17")
        self.assertTrue(view["found"])
        raw_po = {row["po_line"] for row in view["raw"]["purchase"]}
        self.assertEqual(raw_po, {"P180-1", "P180-2"})
        # 运输经历保留 refused/hold 等全部原件，不抹掉应交量
        receipt_ids = {row["receipt"] for row in view["raw"]["receipts"]}
        self.assertIn("G711-R", receipt_ids)
        totals = view["current_fact"]["procurement_totals"]
        self.assertEqual(totals["backordered"], 5)
        self.assertEqual(totals["reorderable_units"], 6)

    def test_unknown_request_returns_not_found(self):
        self.assertFalse(trace_request(self.conn, "NOPE")["found"])

    def test_pick_trace_covers_all_three_layers(self):
        self._full_timeline()
        view = trace_pick(self.conn, "K31")
        self.assertTrue(view["found"])
        # 原单层保留需求与班组回执
        self.assertTrue(view["raw"]["picks"])
        self.assertTrue(view["raw"]["pick_receipts"])
        # 使用层能看到 S1 承诺被后续班次取代
        promises = view["shift_usage"]["promises"]
        self.assertTrue(all(row["shift_id"] == "S1" for row in promises))
        self.assertTrue(all(row["superseded"] == 1 for row in promises))
        # 结果层有领用凭证、持有台账与补救记录
        self.assertTrue(view["verified_outcome"]["issues"])
        self.assertTrue(view["verified_outcome"]["holdings"])
        remediation = view["verified_outcome"]["remediation"]
        self.assertTrue(any(row["pick"] == "K31" for row in remediation))

    def test_pick_trace_evidence_points_to_putaway_voucher(self):
        self._full_timeline()
        view = trace_pick(self.conn, "K33")
        evidence = {
            (row["evidence_kind"], row["evidence_id"])
            for row in view["shift_usage"]["promises"]
        }
        # K33 最终由 G714 上架凭据兑现，数字可追到凭据号
        self.assertIn(("receipt", "G714"), evidence)
