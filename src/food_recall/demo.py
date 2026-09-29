"""核桃油追召场景演示：

python3 -m src.food_recall.demo
"""

from __future__ import annotations

import json
from datetime import date

from .model import (
    Channel, Disposition, Label, MaterialLot, PackageBatch,
    ProductionBatch, RecheckRequest, Shipment,
)
from .service import RecallError, RecallRegistry


def main() -> None:
    today = date(2026, 9, 29)  # 系统停机后恢复日，复检期限照常推进
    r = RecallRegistry(home_region="甲省A市")

    # —— 建档：来源异常的大豆油，标称核桃油；一个生产批拆两个标签包装批
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

    # —— 抽检、封存、复检申请
    r.record_sampling("P1", "棕榈酸", "≤0.4%（核桃油）", "8.2%",
                      "李抽检", date(2026, 9, 15))
    r.seal("M1", 100, "王执法", date(2026, 9, 15), "封存尚未出库原料")
    r.seal("B1", 100, "王执法", date(2026, 9, 15), "封存未出库的B1包装批")
    r.submit_recheck(RecheckRequest(
        "R1", "P1", "李抽检", date(2026, 9, 16), date(2026, 9, 23)))

    # —— 多渠道回报与现场处置（退货/销毁/放行）
    r.dispose("S1", Disposition.RETURNED, 200, "王执法", date(2026, 9, 17),
              "中间商退货现场登记")
    r.submit_receipt("C2", "RC-2", "S2", Disposition.DESTROYED, 150,
                     date(2026, 9, 18))
    r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 200,
                     date(2026, 9, 18))
    r.submit_receipt("C1", "RC-1", "S1", Disposition.RETURNED, 200,
                     date(2026, 9, 18))  # 相同回执重放，不重复扣减

    # —— 抽检人不能批准自己的复检
    try:
        r.decide_recheck("R1", "李抽检", False, date(2026, 9, 19))
    except RecallError as exc:
        print(f"[回避] {exc}")

    # —— 跨地区流向由上级协调
    print(f"[协调] C2结案检查：{r.can_close_channel('C2', today)[1]}")
    r.coordinate("S2", "张处长", date(2026, 9, 20), "省局协调乙省B市局协办")

    # 停机期间期限连续计算，恢复后先催办，再换人补作决定
    print(f"[催办] 逾期未决复检：{[x.id for x in r.overdue_rechecks(today)]}")
    r.decide_recheck("R1", "赵复审", False, today)

    print("\n===== 批次 P1 办案总览 =====")
    print(json.dumps(r.overview("P1", today), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
