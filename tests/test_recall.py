import unittest
from datetime import date

from src.food_recall.model import (
    Channel, Disposition, Label, MaterialLot, PackageBatch,
    ProductionBatch, RecheckRequest, Shipment,
)
from src.food_recall.service import RecallError, RecallRegistry


def build_case():
    """核桃油场景：标称核桃油，原料为来源异常的大豆油，一个生产批拆两个
    包装批（不同标签/品牌），分别经中间商与直播平台卖到本地和外省。"""
    r = RecallRegistry(home_region="甲省A市")
    r.add_material(MaterialLot("M1", "散装大豆油", "无名油料商行", 1000,
                               source_anomalous=True))
    r.add_label(Label("L1", "金核桃纯核桃油", "金核桃公司"))
    r.add_label(Label("L2", "山润核桃油", "山润公司", source_anomalous=True))
    r.add_production(ProductionBatch("P1", "M1", 1000))
    r.add_package(PackageBatch("B1", "P1", "L1", 600))
    r.add_package(PackageBatch("B2", "P1", "L2", 400))
    r.add_channel(Channel("C1", "城东粮油中间商", "中间商", "甲省A市"))
    r.add_channel(Channel("C2", "某直播平台旗舰店", "直播平台", "乙省B市"))
    r.add_shipment(Shipment("S1", "B1", "C1", "城东粮油经营部", 500,
                            "甲省A市", date(2026, 9, 10)))
    r.add_shipment(Shipment("S2", "B2", "C2", "山润食品直播间", 400,
                            "乙省B市", date(2026, 9, 12)))
    return r


class LineageTest(unittest.TestCase):
    def test_one_production_split_into_many_packages(self):
        r = build_case()
        self.assertEqual({p.id for p in r.packages.values()}, {"B1", "B2"})

    def test_split_cannot_exceed_production(self):
        r = build_case()
        with self.assertRaisesRegex(RecallError, "超过产量"):
            r.add_package(PackageBatch("B3", "P1", "L1", 10))

    def test_shipment_cannot_exceed_package_qty(self):
        r = build_case()
        with self.assertRaisesRegex(RecallError, "超过包装量"):
            r.add_shipment(Shipment("S3", "B1", "C1", "城东粮油经营部", 200,
                                    "甲省A市", date(2026, 9, 15)))


class RiskAndAffectedTest(unittest.TestCase):
    def test_sample_and_risk_basis(self):
        r = build_case()
        r.record_sampling("P1", "棕榈酸", "≤0.4%（核桃油）", "8.2%",
                          "李抽检", date(2026, 9, 15))
        basis = r.risk_basis("P1")
        self.assertTrue(any("棕榈酸" in b for b in basis))
        self.assertTrue(any("原料批 M1" in b for b in basis))
        self.assertTrue(any("标签 L2" in b for b in basis))

    def test_indicator_or_material_flags_whole_batch(self):
        # 抽检超标 + 原料异常：真实受影响数量=整批，不能只按品牌查封
        r = build_case()
        r.record_sampling("P1", "棕榈酸", "≤0.4%（核桃油）", "8.2%",
                          "李抽检", date(2026, 9, 15))
        self.assertEqual(set(r.affected_package_ids("P1")), {"B1", "B2"})
        self.assertEqual(r.affected_quantity("P1"), 1000)

    def test_label_only_affects_labelled_package(self):
        # 原料与抽检无异常，仅标签来源异常：只追涉事包装批，合格货物不混入
        r = RecallRegistry("甲省A市")
        r.add_material(MaterialLot("M2", "合格核桃油", "正规油厂", 1000))
        r.add_label(Label("L1", "金核桃纯核桃油", "金核桃公司"))
        r.add_label(Label("L2", "山润核桃油", "山润公司", source_anomalous=True))
        r.add_production(ProductionBatch("P2", "M2", 1000))
        r.add_package(PackageBatch("B1", "P2", "L1", 600))
        r.add_package(PackageBatch("B2", "P2", "L2", 400))
        self.assertEqual(r.affected_package_ids("P2"), ["B2"])
        self.assertEqual(r.affected_quantity("P2"), 400)


class ConservationTest(unittest.TestCase):
    def test_ledger_conserves(self):
        r = build_case()
        r.dispose("S1", Disposition.RETURNED, 200, "王执法", date(2026, 9, 16))
        r.dispose("S1", Disposition.DESTROYED, 120, "王执法", date(2026, 9, 16))
        r.dispose("S1", Disposition.RELEASED, 30, "王执法", date(2026, 9, 18))
        led = r.ledger("S1")
        self.assertEqual((led.returned, led.destroyed, led.released), (200, 120, 30))
        self.assertEqual(led.outstanding, 150)
        self.assertEqual(led.accounted + led.outstanding, led.shipped)

    def test_over_shipment_rejected(self):
        r = build_case()
        r.dispose("S1", Disposition.RETURNED, 400, "王执法", date(2026, 9, 16))
        with self.assertRaisesRegex(RecallError, "守恒"):
            r.dispose("S1", Disposition.DESTROYED, 101, "王执法", date(2026, 9, 16))
        # 被拒绝的登记不得入账
        self.assertEqual(r.ledger("S1").destroyed, 0)

    def test_multiple_channels_merge_into_one_conservation_view(self):
        r = build_case()
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 300,
                         date(2026, 9, 17))
        r.submit_receipt("C2", "RC-2", "S2", Disposition.DESTROYED, 400,
                         date(2026, 9, 17))
        r.dispose("S1", Disposition.RETURNED, 200, "王执法", date(2026, 9, 18))
        self.assertEqual(r.ledger("S1").outstanding, 0)
        self.assertEqual(r.ledger("S2").outstanding, 0)


class ReceiptTest(unittest.TestCase):
    def test_duplicate_receipt_is_idempotent(self):
        r = build_case()
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 100,
                         date(2026, 9, 17))
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 100,
                         date(2026, 9, 17))  # 相同回执重放
        self.assertEqual(r.ledger("S1").returned, 100)

    def test_contradictory_receipt_holds_channel(self):
        r = build_case()
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 100,
                         date(2026, 9, 17))
        with self.assertRaisesRegex(RecallError, "矛盾"):
            r.submit_receipt("C1", "RC-1", "S1", Disposition.DESTROYED, 100,
                             date(2026, 9, 18))
        self.assertTrue(r.channel_states["C1"].held)
        ok, reason = r.can_close_channel("C1", date(2026, 9, 20))
        self.assertFalse(ok)
        self.assertIn("矛盾", reason)
        # 暂停期间新回报一律拒收
        with self.assertRaisesRegex(RecallError, "暂停结案"):
            r.submit_receipt("C1", "RC-9", "S1", Disposition.RETURNED, 10,
                             date(2026, 9, 19))
        r.resolve_hold("C1", "张处长", date(2026, 9, 19),
                       "经平台后台核对，首批100件退货属实")
        self.assertFalse(r.channel_states["C1"].held)

    def test_quantity_conflict_holds_channel(self):
        r = build_case()
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 400,
                         date(2026, 9, 17))
        with self.assertRaisesRegex(RecallError, "出货量"):
            r.submit_receipt("C1", "RC-2", "S1", Disposition.DESTROYED, 200,
                             date(2026, 9, 17))
        self.assertTrue(r.channel_states["C1"].held)


class RecheckTest(unittest.TestCase):
    def _submitted(self):
        r = build_case()
        r.record_sampling("P1", "棕榈酸", "≤0.4%（核桃油）", "8.2%",
                          "李抽检", date(2026, 9, 15))
        r.submit_recheck(RecheckRequest(
            "R1", "P1", "李抽检", date(2026, 9, 16), date(2026, 9, 23)))
        return r

    def test_sampler_cannot_approve_own_recheck(self):
        r = self._submitted()
        with self.assertRaisesRegex(RecallError, "不能批准自己"):
            r.decide_recheck("R1", "李抽检", False, date(2026, 9, 18))

    def test_another_officer_decides(self):
        r = self._submitted()
        r.decide_recheck("R1", "赵复审", False, date(2026, 9, 19))
        self.assertTrue(r.rechecks["R1"].decided)
        with self.assertRaisesRegex(RecallError, "重复批准"):
            r.decide_recheck("R1", "钱复审", True, date(2026, 9, 20))

    def test_deadline_keeps_running_during_outage_and_reminders_fire(self):
        r = self._submitted()
        # 系统停机多日，9/29 恢复时期限已过；催办照常产生
        overdue = r.overdue_rechecks(date(2026, 9, 29))
        self.assertEqual([x.id for x in overdue], ["R1"])
        # 逾期仍须补作决定，证据链标注逾期（停机不顺延）
        r.decide_recheck("R1", "赵复审", False, date(2026, 9, 29))
        note = [e for e in r.evidence if e.target == "P1"
                and e.detail.startswith("维持不合格")][0]
        self.assertIn("逾期", note.detail)


class CoordinationAndCloseTest(unittest.TestCase):
    def test_local_shipment_needs_no_coordination(self):
        r = build_case()
        with self.assertRaisesRegex(RecallError, "无需跨区协调"):
            r.coordinate("S1", "张处长", date(2026, 9, 16))

    def test_cross_region_requires_senior_coordination(self):
        r = build_case()
        r.submit_receipt("C2", "RC-2", "S2", Disposition.DESTROYED, 400,
                         date(2026, 9, 17))
        ok, reason = r.can_close_channel("C2", date(2026, 9, 20))
        self.assertFalse(ok)
        self.assertIn("上级协调", reason)
        r.coordinate("S2", "张处长", date(2026, 9, 18),
                     "省局协调乙省B市局协办")
        ok2, _ = r.can_close_channel("C2", date(2026, 9, 20))
        self.assertTrue(ok2)

    def test_outstanding_blocks_close(self):
        r = build_case()
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 300,
                         date(2026, 9, 17))
        ok, reason = r.can_close_channel("C1", date(2026, 9, 20))
        self.assertFalse(ok)
        self.assertIn("未追回", reason)

    def test_full_close_happy_path(self):
        r = build_case()
        r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 500,
                         date(2026, 9, 17))
        r.close_channel("C1", "王执法", date(2026, 9, 21))
        self.assertIn("C1", r.closed_channels)


class OverviewTest(unittest.TestCase):
    def test_overview_shows_basis_destinations_control_and_responsibility(self):
        r = build_case()
        r.record_sampling("P1", "棕榈酸", "≤0.4%（核桃油）", "8.2%",
                          "李抽检", date(2026, 9, 15))
        r.seal("M1", 100, "王执法", date(2026, 9, 15), "封存库存原料")
        r.seal("B1", 100, "王执法", date(2026, 9, 15), "封存未出库包装批B1")
        r.dispose("S1", Disposition.RETURNED, 200, "王执法", date(2026, 9, 17))
        r.submit_receipt("C2", "RC-2", "S2", Disposition.DESTROYED, 150,
                         date(2026, 9, 18))
        view = r.overview("P1", date(2026, 9, 29))
        self.assertEqual(view["真实受影响数量"], 1000)
        self.assertEqual(view["已控制数量"]["封存"], 200)
        self.assertEqual(view["已控制数量"]["追回控制(退货+销毁)"], 350)
        self.assertTrue(view["风险依据"])
        # 每条出货都满足守恒
        for pkg in view["谱系与去向"]:
            for s in pkg["出货"]:
                self.assertTrue(s["守恒"])
        # 证据链记录了每个决定的责任人
        names = {e["责任人"] for e in view["证据链"]}
        self.assertIn("李抽检", names)
        self.assertIn("王执法", names)
        # 打开批次即可见跨区协调状态
        s2 = next(s for pkg in view["谱系与去向"] for s in pkg["出货"]
                  if s["出货单"] == "S2")
        self.assertTrue(s2["跨区"])
        self.assertFalse(s2["上级已协调"])


if __name__ == "__main__":
    unittest.main()
