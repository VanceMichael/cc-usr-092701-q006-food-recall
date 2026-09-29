"""追召服务测试：五项约束逐条覆盖。"""

import unittest
from datetime import date, timedelta

from src.food_recall import scenario
from src.food_recall.model import DomainError, NodeStatus, RecallService


def make_service(today=date(2026, 9, 20)):
    """最小谱系：原料 MAT-X → 生产批 P1 → 两个包装批，两渠道两订单。"""
    svc = RecallService(home_region="本地", today=today)
    svc.register_material("MAT-X", "基料", "甲供应商")
    svc.register_material("MAT-Y", "辅料", "乙供应商")
    svc.register_label("L1", "品牌A", "核桃油", "来源X")
    svc.register_label("L2", "品牌A", "调和油", "来源X")
    svc.register_production("P1", ["MAT-X", "MAT-Y"], date(2026, 9, 1), "1号线")
    svc.register_packaging("KG1", "P1", date(2026, 9, 2), 100, ["L1"])
    svc.register_packaging("KG2", "P1", date(2026, 9, 2), 50, ["L2"])
    svc.register_channel("C1", "中间商", "批发平台")
    svc.register_channel("C2", "直播间", "直播平台")
    svc.ship("O1", "KG1", "C1", "中间商门店", 60, date(2026, 9, 3), "本地")
    svc.ship("O2", "KG1", "C2", "直播间店铺", 30, date(2026, 9, 3), "外地")
    return svc


class LineageTest(unittest.TestCase):
    def test_one_production_batch_splits_into_packages(self):
        svc = make_service()
        self.assertEqual(
            {p.production_batch_id for p in svc.packagings.values()}, {"P1"})

    def test_shipment_cannot_exceed_packaging_quantity(self):
        svc = make_service()
        # 已出货 90，再出 20 超过产量 100
        with self.assertRaisesRegex(DomainError, "超量"):
            svc.ship("O3", "KG1", "C1", "门店", 20, date(2026, 9, 4), "本地")

    def test_material_abnormality_spreads_through_lineage(self):
        svc = make_service()
        # 同品牌但用其他原料生产的合格货
        svc.register_production("P2", ["MAT-Y"], date(2026, 9, 4), "2号线")
        svc.register_packaging("KG3", "P2", date(2026, 9, 5), 20, ["L1"])
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        affected = svc.affected_packages()
        self.assertIn("KG1", affected)  # 直接抽检
        self.assertNotIn("KG2", affected)
        self.assertNotIn("KG3", affected)

        # 原料 MAT-X 异常 → KG1、KG2（同生产批）受影响；KG3 不受影响
        svc.mark_material_abnormal("MAT-X", "稽查员", "来源不明")
        affected = svc.affected_packages()
        self.assertEqual(set(affected), {"KG1", "KG2"})
        # 关键：同品牌 L1 的 KG3 是合格货，不能按品牌一并查封
        self.assertTrue(any(
            l.label_id == "L1" and l.status is NodeStatus.NORMAL
            for l in svc.labels.values()))
        basis = svc.risk_basis("KG2")
        self.assertTrue(any(b["type"] == "原料来源异常" for b in basis))

    def test_label_abnormality_spreads_to_packages_carrying_it(self):
        svc = make_service()
        svc.mark_label_abnormal("L2", "稽查员", "假冒标签")
        affected = svc.affected_packages()
        self.assertEqual(set(affected), {"KG2"})

    def test_risk_basis_is_deduped_across_sources(self):
        svc = make_service()
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        svc.mark_material_abnormal("MAT-X", "稽查员", "来源不明")
        # KG1 同时有抽检和原料两条依据，批次本身只出现一次
        self.assertEqual(sorted(svc.affected_packages()), ["KG1", "KG2"])
        types = {b["type"] for b in svc.risk_basis("KG1")}
        self.assertEqual(types, {"抽检不符合", "原料来源异常"})


class ConservationTest(unittest.TestCase):
    def test_production_conservation_invariant(self):
        svc = make_service()
        row = svc.conservation("KG1")
        self.assertEqual(row["produced"], row["in_stock"] + row["shipped"])
        self.assertEqual((row["shipped"]),
                         row["returned"] + row["destroyed"]
                         + row["released"] + row["outstanding"])

    def test_seal_unshipped_stock(self):
        svc = make_service()
        # 未出库 10 件，只能封存这 10 件
        with self.assertRaisesRegex(DomainError, "封存数量"):
            svc.seal_stock("KG1", 11, "监管员")
        svc.seal_stock("KG1", 10, "监管员")
        row = svc.conservation("KG1")
        self.assertEqual(row["sealed"], 10)
        self.assertEqual(row["controlled"], 10)
        self.assertEqual(row["unsealed"], 0)
        # 已封存但尚未销毁/放行，仍属待最终处置
        self.assertEqual(row["stock_pending"], 10)
        svc.destroy_stock("KG1", 10, "监管员")
        self.assertEqual(svc.conservation("KG1")["stock_pending"], 0)

    def test_receipts_merge_across_channels_and_conservation_holds(self):
        svc = make_service()
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        svc.seal_stock("KG1", 10, "监管员")
        # C1 出货 60：退 40 + 销毁 15 = 55，余 5
        svc.submit_receipt("C1", "KG1", "R1", 40, 15, "联络员")
        # C2 出货 30：退 30
        svc.submit_receipt("C2", "KG1", "R2", 30, 0, "联络员")
        row = svc.conservation("KG1")
        self.assertEqual(row["returned"], 70)
        self.assertEqual(row["destroyed"], 15)
        self.assertEqual(row["outstanding"], 5)
        self.assertEqual(row["shipped"],
                         row["returned"] + row["destroyed"] + row["outstanding"])
        self.assertEqual(row["controlled"], 10 + 70 + 15)

    def test_duplicate_platform_receipt_is_idempotent(self):
        svc = make_service()
        first = svc.submit_receipt("C1", "KG1", "R1", 40, 15, "联络员")
        second = svc.submit_receipt("C1", "KG1", "R1", 40, 15, "联络员")
        self.assertIs(first, second)
        row = svc.conservation("KG1")
        # 没有重复扣减
        self.assertEqual(row["returned"], 40)
        self.assertEqual(row["destroyed"], 15)

    def test_contradictory_receipt_suspends_channel_and_blocks_closure(self):
        svc = make_service()
        svc.submit_receipt("C1", "KG1", "R1", 40, 15, "联络员")
        # 同一回执号、不同内容
        with self.assertRaisesRegex(DomainError, "内容矛盾"):
            svc.submit_receipt("C1", "KG1", "R1", 50, 5, "联络员")
        self.assertTrue(svc.channels["C1"].suspended)
        # 挂起后不能继续回报
        with self.assertRaisesRegex(DomainError, "挂起"):
            svc.submit_receipt("C1", "KG1", "R9", 1, 0, "联络员")
        blockers = svc.closure_blockers()
        self.assertTrue(any("回执矛盾" in b for b in blockers))
        with self.assertRaisesRegex(DomainError, "暂不能结案"):
            svc.close_case()

    def test_receipt_over_shipment_is_rejected(self):
        svc = make_service()
        # C2 只出货 30，报退 31 拦截
        with self.assertRaisesRegex(DomainError, "回报超量"):
            svc.submit_receipt("C2", "KG1", "R2", 31, 0, "联络员")

    def test_retest_release_counts_in_conservation(self):
        svc = make_service()
        svc.seal_stock("KG1", 10, "监管员")
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        svc.apply_retest("KG1", "S1", "企业")
        svc.approve_retest("rt-KG1-S1", "授权人乙")
        # 复检合格：放行在库 10 件
        svc.decide_retest("rt-KG1-S1", True, 10, "复检员丙")
        row = svc.conservation("KG1")
        self.assertEqual(row["stock_released"], 10)
        self.assertEqual(row["stock_pending"], 0)


class EvidenceAndRetestTest(unittest.TestCase):
    def test_sampler_cannot_approve_own_retest(self):
        svc = make_service()
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        svc.apply_retest("KG1", "S1", "企业")
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            svc.approve_retest("rt-KG1-S1", "检测员甲")
        # 换人可以批准
        svc.approve_retest("rt-KG1-S1", "授权人乙")
        self.assertEqual(svc.retests["rt-KG1-S1"].approved_by, "授权人乙")

    def test_sampler_cannot_make_retest_decision_either(self):
        svc = make_service()
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        svc.apply_retest("KG1", "S1", "企业")
        svc.approve_retest("rt-KG1-S1", "授权人乙")
        with self.assertRaisesRegex(DomainError, "原抽检人员"):
            svc.decide_retest("rt-KG1-S1", True, 0, "检测员甲")

    def test_retest_deadline_keeps_running_during_outage(self):
        svc = make_service(today=date(2026, 9, 20))
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 19))
        app = svc.apply_retest("KG1", "S1", "企业")
        svc.approve_retest("rt-KG1-S1", "授权人乙")
        deadline = app.deadline
        self.assertEqual(deadline, date(2026, 9, 27))
        # 停机 5 天再恢复，期限不顺延
        svc.advance(date(2026, 9, 25), running=False)
        svc.advance(date(2026, 9, 27), running=True)
        self.assertEqual(app.deadline, deadline)
        # 催办已在 9-23、9-26 发出（自然日，不停）
        self.assertEqual(app.reminded_on, [date(2026, 9, 23), date(2026, 9, 26)])
        # 逾期后不能再出结论，进入超期程序，且列入结案障碍
        svc.advance(date(2026, 9, 28), running=True)
        with self.assertRaisesRegex(DomainError, "复检已逾期"):
            svc.decide_retest("rt-KG1-S1", True, 0, "复检员丙")
        self.assertTrue(any("rt-KG1-S1" in b and "逾期" in b
                            for b in svc.closure_blockers()))

    def test_evidence_chain_records_every_decision_with_responsible(self):
        svc = make_service()
        svc.record_sample("S1", "KG1", "棕榈酸", "≤8%", "12%", True,
                          "检测员甲", date(2026, 9, 6))
        svc.seal_stock("KG1", 10, "监管员丁")
        svc.destroy_stock("KG1", 4, "监管员丁")
        chain = svc.evidence_chain()
        actions = [(e["action"], e["responsible"]) for e in chain]
        self.assertIn(("抽样", "检测员甲"), actions)
        self.assertIn(("封存", "监管员丁"), actions)
        self.assertIn(("销毁", "监管员丁"), actions)
        self.assertTrue(all(e["responsible"] for e in chain))

    def test_destroy_only_from_sealed_stock(self):
        svc = make_service()
        with self.assertRaisesRegex(DomainError, "已封存"):
            svc.destroy_stock("KG1", 1, "监管员")
        svc.seal_stock("KG1", 10, "监管员")
        svc.destroy_stock("KG1", 10, "监管员")
        with self.assertRaisesRegex(DomainError, "已封存"):
            svc.destroy_stock("KG1", 1, "监管员")


class CrossRegionTest(unittest.TestCase):
    def test_cross_region_flow_requires_superior_coordination(self):
        svc = make_service()
        blockers = svc.closure_blockers()
        self.assertTrue(any("跨地区" in b and "上级协调" in b for b in blockers))
        svc.coordinate_cross_region("O2", "省局协调人", "外地市局", "协查")
        self.assertFalse(any("O2" in b for b in svc.closure_blockers()))

    def test_open_package_shows_coordination_state(self):
        svc = make_service()
        view = svc.open_package("KG1")
        remote = [d for d in view["destinations"] if d["cross_region"]]
        self.assertEqual(len(remote), 1)
        self.assertFalse(remote[0]["coordinated"])
        svc.coordinate_cross_region("O2", "省局协调人", "外地市局", "协查")
        view = svc.open_package("KG1")
        remote = [d for d in view["destinations"] if d["cross_region"]]
        self.assertTrue(remote[0]["coordinated"])


class CaseViewTest(unittest.TestCase):
    def test_open_package_shows_basis_destinations_control_and_decisions(self):
        svc = scenario.build()
        view = svc.open_package("PKG-A")
        self.assertTrue(view["is_affected"])
        self.assertTrue(any(b["type"] == "抽检不符合" for b in view["risk_basis"]))
        self.assertTrue(any(b["type"] == "原料来源异常" for b in view["risk_basis"]))
        # 实际去向：中间商 + 直播两个销售主体
        sellers = {d["seller"] for d in view["destinations"]}
        self.assertEqual(len(sellers), 2)
        # 已控制数量：封存 100 + 中间商回报 550 + 直播回报 200 = 850
        self.assertEqual(view["quantities"]["controlled"], 850)
        # 每个决定都有责任人
        self.assertTrue(view["decisions"])
        self.assertTrue(all(d["responsible"] for d in view["decisions"]))
        # 风险依据中直接抽检带来责任人
        sampled = [b for b in view["risk_basis"] if b["type"] == "抽检不符合"]
        self.assertEqual(sampled[0]["responsible"], "检测员李进")

    def test_safe_same_brand_package_is_not_affected(self):
        svc = scenario.build()
        view = svc.open_package("PKG-C")
        self.assertFalse(view["is_affected"])
        self.assertEqual(view["risk_basis"], [])
        self.assertNotIn("PKG-C", svc.affected_packages())

    def test_scenario_full_closure_path(self):
        svc = scenario.build()
        # 矛盾回执前先把直播 PKG-A 剩余 100 件追回
        # （场景中已退 200，出货 300，余 100）
        svc.submit_receipt("CH-LIVE", "PKG-A", "DB-7790", 100, 0,
                           "渠道联络员周琦", on=date(2026, 9, 21))
        # 中间商出货 600，已回报 550，剩 50 件销毁回报
        svc.submit_receipt("CH-WS", "PKG-A", "WS-1002", 0, 50,
                           "渠道联络员周琦", on=date(2026, 9, 21))
        # PKG-B 剩 50 件由旗舰店销毁回报
        svc.submit_receipt("CH-MALL", "PKG-B", "DB-8002", 0, 50,
                           "渠道联络员周琦", on=date(2026, 9, 21))
        # 复检：回避抽检人后批准，期限内出合格结论放行（此处 0 件）
        svc.approve_retest("rt-PKG-A-SMP-2026-031", "复检授权人钱敏")
        svc.decide_retest("rt-PKG-A-SMP-2026-031", False, 0, "复检员韩冰")
        # 在库封存 100 件销毁
        svc.destroy_stock("PKG-A", 100, "监管员王敏")
        # 跨区协调
        svc.coordinate_cross_region(
            "ORD-02", "省局协调处孙浩", "岭东市市场监管局", "协查控制")
        # 此时仍有在库：PKG-A 0；PKG-B 在库 100 件未处置 → 先封存销毁
        svc.seal_stock("PKG-B", 100, "监管员王敏")
        svc.destroy_stock("PKG-B", 100, "监管员王敏")
        # 全部守恒、无挂起、复检结案、跨区已协调 → 可以结案
        svc.close_case()
        self.assertTrue(any(e.action == "结案" for e in svc.events))

    def test_scenario_outstanding_quantity_matches_shipments(self):
        svc = scenario.build()
        a = svc.conservation("PKG-A")
        # 出货 900 = 中间商 550 + 直播 200 + 未追回 150
        self.assertEqual(a["shipped"], 900)
        self.assertEqual(a["outstanding"], 150)
        # 各渠道小计也守恒
        ws = next(c for c in a["per_channel"] if c["channel"] == "万家中间商")
        live = next(c for c in a["per_channel"] if c["channel"] == "山润直播间店铺")
        self.assertEqual((ws["returned"], ws["destroyed"], ws["outstanding"]),
                         (400, 150, 50))
        self.assertEqual((live["returned"], live["outstanding"]), (200, 100))


if __name__ == "__main__":
    unittest.main()
