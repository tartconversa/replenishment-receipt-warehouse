"""拣配班次：时点快照、整单配齐、独占分配、承诺版本、领用回执与补救。"""
from __future__ import annotations

from _base import WarehouseTestCase
from fulfillment.picking import (
    PickingError,
    publish_shift,
    register_pick_receipts,
    remediation_queue,
)
from fulfillment.rebuild import rebuild


def _shift_decisions(conn, shift_id):
    return {
        (row["pick"], row["station"], row["sku"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM shift_decision WHERE shift_id = ?", (shift_id,)
        )
    }


class ShiftTimelineTest(WarehouseTestCase):
    def publish(self, shift_id, as_of, arrival):
        return publish_shift(
            self.conn, self.tmp / "shifts", as_of, arrival, shift_id
        )

    def test_s1_point_in_time_uses_only_arrival_three_facts(self):
        manifest = self.publish("S1", "2026-10-05T08:00:00Z", 3)
        self.assertEqual(sorted(manifest["released_picks"]), ["K31", "K33"])
        promises = self.conn.execute(
            "SELECT DISTINCT evidence_id FROM shift_promise WHERE shift_id = 'S1'"
        ).fetchall()
        # arrival 6 的 G714 与 T03 入位都不得出现在 arrival 3 的承诺里
        self.assertEqual({row["evidence_id"] for row in promises}, {"G711", "G712"})

    def test_whole_pick_only_released_when_all_lines_covered(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        decisions = _shift_decisions(self.conn, "S1")
        # K31 含 A-01 PEN 与 B-02 TAPE 两行，整单配齐才发布
        self.assertTrue(all(decisions[key]["status"] == "released"
                            for key in decisions if key[0] == "K31"))

    def test_no_stock_unit_promised_to_two_picks(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        # G711 在 arrival 3 总量 12：K31 领 8 + K33 领 4，恰好独占，不能超发
        used = self.conn.execute(
            "SELECT SUM(units) AS u FROM shift_promise "
            "WHERE shift_id = 'S1' AND evidence_id = 'G711'"
        ).fetchone()["u"]
        self.assertEqual(used, 12)

    def test_correction_re_evaluates_uncollected_demands_and_supersedes(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        # arrival 5 更正（G711 改 7 + hold 5）后，K33 尚未领走，应被重新判断为短配
        self.publish("S2", "2026-10-05T09:30:00Z", 5)
        decisions = _shift_decisions(self.conn, "S2")
        self.assertEqual(decisions[("K33", "A-01", "PEN")]["status"], "short")
        self.assertEqual(decisions[("K34", "A-01", "PEN")]["status"], "short")
        # S1 的未领承诺已被明确取代
        active = self.conn.execute(
            "SELECT COUNT(*) AS c FROM shift_promise WHERE shift_id = 'S1' AND superseded = 0"
        ).fetchone()["c"]
        self.assertEqual(active, 0)

    def test_collected_pick_keeps_voucher_and_enters_remediation_on_correction(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        register_pick_receipts(self.conn)
        rebuild(self.conn)
        self.publish("S2", "2026-10-05T09:30:00Z", 5)
        queue = {
            (row["pick"], row["station"], row["sku"]): row
            for row in remediation_queue(self.conn, 5)
        }
        k31 = queue[("K31", "A-01", "PEN")]
        # 承诺 8、更正后凭据仅支撑 7、退回尚未发生 → 短 1；此时货位总量 7 已被持有 8 件
        # 全部占用，没有自由库存可补救，要等新上架或班组退回
        self.assertEqual(k31["committed_units"], 8)
        self.assertEqual(k31["short_units"], 1)
        self.assertEqual(k31["remediable_free_units"], 0)

    def test_returned_goods_become_reallocatable_only_after_crew_receipt(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        register_pick_receipts(self.conn)
        rebuild(self.conn)
        self.publish("S2", "2026-10-05T09:30:00Z", 5)
        # arrival 5 时点退回（arrival 6）还没发生：短数仍在
        self.assertEqual(
            next(row for row in remediation_queue(self.conn, 5)
                 if row["pick"] == "K31" and row["sku"] == "PEN")["short_units"], 1
        )
        self.publish("S3", "2026-10-05T09:45:00Z", 6)
        register_pick_receipts(self.conn)
        rebuild(self.conn)
        queue = {
            (row["pick"], row["station"], row["sku"]): row
            for row in remediation_queue(self.conn, 6)
        }
        # 班组回执退回 2 件后：净持 6，凭据可支撑 6，短数清零
        self.assertEqual(queue[("K31", "A-01", "PEN")]["short_units"], 0)
        # S3 能把 K33/K34/K32 整单配齐（含退回释放与新上架 G714、T03）
        decisions = _shift_decisions(self.conn, "S3")
        self.assertTrue(all(row["status"] == "released" for row in decisions.values()))

    def test_collection_against_missing_or_expanded_promise_is_rejected(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        self.conn.execute(
            "INSERT INTO raw_pick_receipt (arrival, issue, pick, station, sku, units, action, "
            "content_hash) VALUES (4, 'WX', 'K99', 'A-01', 'PEN', 1, 'collected', 'rx')"
        )
        self.conn.commit()
        result = register_pick_receipts(self.conn)
        self.assertEqual(result["rejected"], 1)
        rejected = self.conn.execute(
            "SELECT reason FROM issue_ledger WHERE issue = 'WX'"
        ).fetchone()
        self.assertIn("没有可兑现的当前承诺", rejected["reason"])

    def test_register_receipts_is_idempotent(self):
        self.publish("S1", "2026-10-05T08:00:00Z", 3)
        first = register_pick_receipts(self.conn)
        second = register_pick_receipts(self.conn)
        self.assertEqual(first["accepted"], 3)
        self.assertEqual(second, {"accepted": 0, "rejected": 0, "duplicate": 0})

    def test_due_filter_excludes_expired_demands(self):
        # as_of 晚于全部需求期限：没有可发布候选，决策仅登记过期
        manifest = self.publish("SLATE", "2026-10-06T00:00:00Z", 3)
        self.assertEqual(manifest["released_picks"], [])
        decisions = _shift_decisions(self.conn, "SLATE")
        self.assertTrue(decisions)
        self.assertTrue(all(row["status"] == "expired_demand" for row in decisions.values()))

    def test_shift_id_must_be_unique(self):
        self.publish("SDUP", "2026-10-05T08:00:00Z", 3)
        with self.assertRaises(PickingError):
            self.publish("SDUP", "2026-10-05T08:00:00Z", 3)

    def test_publishing_historical_shift_ignores_later_shift_occupancy(self):
        # 先发布一个晚批次班次，再回头发布早批次班次：后者只能看到早时点的事实，
        # 不能被未来班次的承诺占用干扰（反之亦然，各自独立时点快照）。
        self.publish("LATE", "2026-10-05T09:45:00Z", 6)
        manifest = self.publish("EARLY", "2026-10-05T08:00:00Z", 3)
        evidence = {
            row["evidence_id"] for row in self.conn.execute(
                "SELECT evidence_id FROM shift_promise WHERE shift_id = 'EARLY'"
            )
        }
        self.assertNotIn("G714", evidence)  # arrival 6 才上架
        self.assertIn("G711", evidence)
        self.assertIn("K31", manifest["released_picks"])
