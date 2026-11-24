"""采购履约：行级销欠、待核实、补单建议口径。"""
from __future__ import annotations

from _base import WarehouseTestCase
from fulfillment.procurement import build_request_facts


class ProcurementTest(WarehouseTestCase):
    def facts(self):
        return {fact.request: fact for fact in build_request_facts(self.conn)}

    def test_qualified_receipt_clears_backorder_in_request_order(self):
        facts = self.facts()
        # P180-1 确认 12，G711 当前合格收货 7（rev2 覆盖 rev1 的 12，hold 5 不销欠）
        r17 = facts["R17"]
        self.assertEqual(r17.confirmed_units, 12)
        self.assertEqual(r17.qualified_received, 7)
        self.assertEqual(r17.backordered, 5)

    def test_only_explicit_cancel_without_cover_is_reorderable(self):
        r17 = self.facts()["R17"]
        # P180-2 的 6 件被明确取消 → 可补 6；欠交的 5 件仍有在途保障，不可补
        self.assertEqual(r17.canceled_units, 6)
        self.assertEqual(r17.reorderable_canceled, 6)
        self.assertEqual(r17.reorderable, 6)

    def test_timeout_without_receipt_goes_to_pending_verification(self):
        # R21 已发出但无任何回执：待核实，绝不进入补单量
        r21 = self.facts()["R21"]
        self.assertEqual(r21.pending_verification, 3)
        self.assertEqual(r21.reorderable, 0)

    def test_timeout_request_resolved_by_later_confirmation_leaves_pending(self):
        # R18 连接器标记 timeout，但采购确认 4 且合格收货 4：待核实归零
        r18 = self.facts()["R18"]
        self.assertEqual(r18.connector_state, "timeout")
        self.assertEqual(r18.pending_verification, 0)
        self.assertEqual(r18.backordered, 0)
        self.assertEqual(r18.reorderable, 0)

    def test_refused_kept_in_transport_history_does_not_erase_obligation(self):
        # G713 拒收 4：仍计入采购行的运输经历，且 R18 已被 G714 合格收货覆盖，不产生补单
        refused = self.conn.execute(
            "SELECT COUNT(*) AS c FROM raw_receipt WHERE state = 'refused'"
        ).fetchone()["c"]
        self.assertEqual(refused, 1)
        r18 = self.facts()["R18"]
        self.assertEqual(r18.refused_units, 4)
        self.assertEqual(r18.reorderable_refused, 0)

    def test_confirmed_transfer_is_internal_cover(self):
        # T03 为 R18 确认调拨 2（含在途口径），作为内部保障参与扣减
        r18 = self.facts()["R18"]
        self.assertEqual(r18.confirmed_transfer_units, 2)

    def test_frozen_line_excluded_from_totals(self):
        # P500-1 冻结：R20 只能由 P193-1 的合格收货销欠，数字不受冻结行影响
        r20 = self.facts()["R20"]
        self.assertEqual(r20.confirmed_units, 5)
        self.assertEqual(r20.qualified_received, 5)
        self.assertEqual(r20.backordered, 0)
        self.assertEqual(r20.reorderable, 0)
