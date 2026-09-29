"""演示情景：某品牌食用油检出棕榈酸超出核桃油标准后的追召过程。

人物与货物均为虚构。情景覆盖：

- 一个生产批拆成三个包装批，分别贴“核桃油”标签和其他标签
- 抽检 PKG-A 不符合 → 按品牌查封会误伤同品牌合格的 PKG-C
- 基料（大豆油）来源异常 → 真实受影响范围是 PKG-A/PKG-B，而非按品牌
- 中间商与直播两个渠道分别回报，平台回执幂等、矛盾挂起
- 抽检人员申请复检被回避要求拦下，换批准人；停机期间期限照走
- 跨地区订单须上级协调
"""

from __future__ import annotations

from datetime import date

from .model import NodeStatus, RecallService


def build(today: date = date(2026, 9, 20)) -> RecallService:
    svc = RecallService(home_region="浙中市", today=today)

    # 原料：标称核桃油基料，实际掺入大豆油，来源异常
    svc.register_material("MAT-W01", "核桃油基料", "云岭油脂厂")
    svc.register_material("MAT-S02", "大豆油基料", "无名作坊（民房窝点）")
    # 标签：同一批货贴不同标签
    svc.register_label("LBL-W", "山润", "冷榨核桃油 500ml", "标签印刷点A")
    svc.register_label("LBL-M", "山润", "食用植物调和油 500ml", "标签印刷点A")
    svc.register_label("LBL-O", "山润", "核桃油礼盒 500ml", "正规标签供应商B")

    # 生产批 PRD-0912：投入两种原料，拆成三个包装批
    svc.register_production("PRD-0912", ["MAT-W01", "MAT-S02"],
                            date(2026, 9, 12), "1号线")
    svc.register_packaging("PKG-A", "PRD-0912", date(2026, 9, 13),
                           1000, ["LBL-W"])
    svc.register_packaging("PKG-B", "PRD-0912", date(2026, 9, 13),
                           600, ["LBL-M"])
    # PKG-C 是另一条线、用正规原料生产的同品牌合格货
    svc.register_production("PRD-0915", ["MAT-W01"],
                            date(2026, 9, 15), "2号线")
    svc.register_packaging("PKG-C", "PRD-0915", date(2026, 9, 16),
                           400, ["LBL-O"])

    # 渠道
    svc.register_channel("CH-WS", "万家中间商", "线下批发")
    svc.register_channel("CH-LIVE", "山润直播间店铺", "抖播平台")
    svc.register_channel("CH-MALL", "山润旗舰店", "抖播平台")

    # 出货
    svc.ship("ORD-01", "PKG-A", "CH-WS", "万家粮油批发部", 600,
             date(2026, 9, 14), "浙中市")
    svc.ship("ORD-02", "PKG-A", "CH-LIVE", "山润直播间店铺", 300,
             date(2026, 9, 14), "外省·岭东市")
    svc.ship("ORD-03", "PKG-B", "CH-MALL", "山润旗舰店", 500,
             date(2026, 9, 15), "浙中市")
    svc.ship("ORD-04", "PKG-C", "CH-LIVE", "山润直播间店铺", 400,
             date(2026, 9, 17), "浙中市")

    # 抽检：PKG-A 棕榈酸超出核桃油标准（核桃油中棕榈酸通常很低，
    # 大豆油棕榈酸含量明显更高，据此识别掺假）
    svc.record_sample(
        "SMP-2026-031", "PKG-A", "棕榈酸", "≤约8%（核桃油特征指标）",
        "12.6%", True, sampled_by="检测员李进", sampled_on=date(2026, 9, 18))
    # 控制尚未出库的 100 件
    svc.seal_stock("PKG-A", 100, actor="监管员王敏")

    # 核查发现基料来源异常 → PKG-A、PKG-B 全部受影响（按谱系扩散）
    svc.mark_material_abnormal(
        "MAT-S02", actor="稽查员赵磊",
        reason="大豆油基料来自偏僻民房窝点，无进货查验记录，冒充核桃油灌装")

    # 渠道回报：中间商退货 400、销毁 150；直播店先退 200
    svc.submit_receipt("CH-WS", "PKG-A", "WS-1001", 400, 150,
                       actor="渠道联络员周琦", on=date(2026, 9, 20))
    svc.submit_receipt("CH-LIVE", "PKG-A", "DB-7788", 200, 0,
                       actor="渠道联络员周琦", on=date(2026, 9, 20))
    # 同一直播回执再传一次：幂等，不重复扣减
    svc.submit_receipt("CH-LIVE", "PKG-A", "DB-7788", 200, 0,
                       actor="渠道联络员周琦", on=date(2026, 9, 20))
    # PKG-B：旗舰店退货 300、销毁 150，剩 50 件
    svc.submit_receipt("CH-MALL", "PKG-B", "DB-8001", 300, 150,
                       actor="渠道联络员周琦", on=date(2026, 9, 21))

    # 复检：企业申请；抽检人李进不能批准，须他人批准
    svc.apply_retest("PKG-A", "SMP-2026-031", applicant="企业代表陈峰")
    return svc
