"""食品风险批次追召领域服务。

围绕五项约束构建：

1. 批次谱系：原料批 → 生产批 →（拆成多个）灌装/包装批 → 标签 → 订单 → 销售主体
2. 抽检证据：抽样、封存、复检、处置全程留痕，每个决定都有责任人
3. 召回守恒：包装产量 = 在库 + 出货；出货 = 退货 + 销毁 + 复检放行 + 未追回
4. 复检权限：抽检人员不能批准自己抽样的复检；复检期限按自然日推进
5. 跨区协同：跨地区流向须上级协调后才能异地控制/结案

本模块只依赖标准库，数据均为演示用虚构内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from itertools import count


class DomainError(ValueError):
    """业务规则被违反。"""


# --------------------------------------------------------------------------- 基础字典


def _require_identifier(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{what}必须是非空文本")
    return value.strip()


def _require_qty(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DomainError(f"{what}必须是非负整数")
    return value


class NodeStatus(str, Enum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"  # 原料/标签来源异常


# --------------------------------------------------------------------------- 谱系实体


@dataclass
class MaterialBatch:
    """原料批，例如大豆油基料。"""

    material_id: str
    name: str
    supplier: str
    status: NodeStatus = NodeStatus.NORMAL


@dataclass
class LabelStock:
    """标签（品牌/品名），同一批货可能贴多种标签。"""

    label_id: str
    brand: str
    product_name: str
    source: str
    status: NodeStatus = NodeStatus.NORMAL


@dataclass
class ProductionBatch:
    """生产批：一次投料产出，可由多种原料兑成，后续可拆成多个包装批。"""

    batch_id: str
    material_ids: list[str]
    produced_on: date
    line: str


@dataclass
class PackagingBatch:
    """灌装/包装批：一个生产批拆出的最小追召单元。"""

    pkg_id: str
    production_batch_id: str
    filled_on: date
    quantity: int  # 本包装批产量（瓶/桶）
    label_ids: list[str] = field(default_factory=list)


@dataclass
class Shipment:
    """订单出货：包装批卖给某渠道下的某销售主体。"""

    shipment_id: str
    pkg_id: str
    channel_id: str
    seller_id: str  # 销售主体（中间商/直播间店铺）
    quantity: int
    shipped_on: date
    region: str  # 收货地区


@dataclass
class Channel:
    """销售渠道；同一平台的回执按平台回执号去重。"""

    channel_id: str
    name: str
    platform: str
    suspended: bool = False


@dataclass
class Sample:
    """抽样检验结论，是风险依据的一种。"""

    sample_id: str
    pkg_id: str
    indicator: str  # 检测指标，如棕榈酸
    standard_limit: str  # 核桃油标准限值描述
    found_value: str
    nonconforming: bool
    sampled_by: str  # 抽检人员
    sampled_on: date


@dataclass
class Event:
    """证据链上的一个决定/动作。"""

    seq: int
    on: date
    action: str
    actor: str  # 责任人
    target: str
    detail: str = ""
    links: list[int] = field(default_factory=list)  # 关联事件 seq


@dataclass
class Receipt:
    """渠道回报回执（退货/销毁）。"""

    receipt_no: str  # 平台回执号
    platform: str
    channel_id: str
    pkg_id: str
    returned: int
    destroyed: int
    on: date
    fingerprint: tuple  # 内容指纹，用于矛盾判定


@dataclass
class RetestApplication:
    application_id: str
    pkg_id: str
    sample_id: str
    applicant: str
    applied_on: date
    deadline: date  # 出结论的最后期限（自然日，不顺延）
    approved_by: str | None = None
    result: str | None = None  # conforming / nonconforming
    decided_on: date | None = None
    reminded_on: list[date] = field(default_factory=list)


@dataclass
class Coordination:
    """跨地区流向的上级协调记录。"""

    flow_id: str
    shipment_id: str
    superior: str | None = None
    remote_authority: str | None = None
    note: str = ""
    coordinated: bool = False


# --------------------------------------------------------------------------- 追召服务


class RecallService:
    # 复检期限：自受理申请之日起 7 个自然日；停机不计入免责，期限照走
    RETEST_WINDOW_DAYS = 7
    # 催办间隔：每 3 个自然日推一次
    REMINDER_EVERY_DAYS = 3

    def __init__(self, home_region: str, today: date):
        self.home_region = home_region
        self.today = today
        self._running = True

        self.materials: dict[str, MaterialBatch] = {}
        self.labels: dict[str, LabelStock] = {}
        self.productions: dict[str, ProductionBatch] = {}
        self.packagings: dict[str, PackagingBatch] = {}
        self.shipments: dict[str, Shipment] = {}
        self.channels: dict[str, Channel] = {}
        self.samples: dict[str, Sample] = {}
        self.events: list[Event] = []
        self._seq = count(1)

        # 包装批维度的在库控制与处置
        self.sealed_stock: dict[str, int] = {}   # 已封存（未出库部分）
        self.stock_destroyed: dict[str, int] = {}  # 在库销毁
        self.stock_released: dict[str, int] = {}   # 复检放行（在库部分）

        # 渠道回报
        self._receipts: dict[tuple[str, str], Receipt] = {}  # (平台, 回执号)
        self.returned: dict[tuple[str, str], int] = {}   # (pkg, channel)
        self.destroyed: dict[tuple[str, str], int] = {}
        self.released: dict[tuple[str, str], int] = {}   # 复检放行（出货部分）

        self.retests: dict[str, RetestApplication] = {}
        self.coordinations: dict[str, Coordination] = {}

        # 直接被抽检判定风险的包装批
        self.direct_risk_pkgs: set[str] = set()

    # ----- 时钟 / 停机 -----------------------------------------------------

    def advance(self, today: date, running: bool = True) -> None:
        """推进日期。

        停机（running=False）期间复检期限仍按自然日计算；恢复时把停机期间
        漏发的催办按应发日期补推，逾期的复检保持逾期，不顺延。
        """
        if today < self.today:
            raise DomainError("不能把时钟往回拨")
        was_down = not self._running
        self.today = today
        self._running = running
        if running:
            if was_down:
                self._log("系统恢复", "system", "system", "停机结束，期限与催办继续按自然日推进")
            self._refresh_reminders()

    def _refresh_reminders(self) -> None:
        for app in self.retests.values():
            if app.result is not None:
                continue
            due = app.applied_on + timedelta(days=self.REMINDER_EVERY_DAYS)
            while due <= self.today:
                if due not in app.reminded_on:
                    app.reminded_on.append(due)
                    self._log_on(
                        due, "催办", "system", app.pkg_id,
                        f"复检申请 {app.application_id} 催办（应于 {app.deadline} 前出结论）",
                    )
                due += timedelta(days=self.REMINDER_EVERY_DAYS)

    # ----- 谱系登记 ---------------------------------------------------------

    def register_material(self, material_id: str, name: str, supplier: str,
                          status: NodeStatus = NodeStatus.NORMAL) -> None:
        mid = _require_identifier(material_id, "原料批编号")
        if mid in self.materials:
            raise DomainError(f"原料批已存在：{mid}")
        self.materials[mid] = MaterialBatch(mid, name, supplier, status)

    def register_label(self, label_id: str, brand: str, product_name: str,
                       source: str, status: NodeStatus = NodeStatus.NORMAL) -> None:
        lid = _require_identifier(label_id, "标签编号")
        if lid in self.labels:
            raise DomainError(f"标签已存在：{lid}")
        self.labels[lid] = LabelStock(lid, brand, product_name, source, status)

    def register_production(self, batch_id: str, material_ids: list[str],
                            produced_on: date, line: str) -> None:
        bid = _require_identifier(batch_id, "生产批编号")
        if bid in self.productions:
            raise DomainError(f"生产批已存在：{bid}")
        if not material_ids:
            raise DomainError("生产批必须投入至少一种原料")
        for mid in material_ids:
            if mid not in self.materials:
                raise DomainError(f"原料批不存在：{mid}")
        self.productions[bid] = ProductionBatch(bid, list(material_ids), produced_on, line)

    def register_packaging(self, pkg_id: str, production_batch_id: str,
                           filled_on: date, quantity: int,
                           label_ids: list[str]) -> None:
        """一个生产批可拆出多个包装批；每个包装批可贴一个或多个标签。"""
        pid = _require_identifier(pkg_id, "包装批编号")
        if pid in self.packagings:
            raise DomainError(f"包装批已存在：{pid}")
        if production_batch_id not in self.productions:
            raise DomainError(f"生产批不存在：{production_batch_id}")
        qty = _require_qty(quantity, "包装产量")
        if qty <= 0:
            raise DomainError("包装产量必须大于 0")
        for lid in label_ids:
            if lid not in self.labels:
                raise DomainError(f"标签不存在：{lid}")
        self.packagings[pid] = PackagingBatch(
            pid, production_batch_id, filled_on, qty, list(label_ids)
        )
        self.sealed_stock[pid] = 0
        self.stock_destroyed[pid] = 0
        self.stock_released[pid] = 0

    def register_channel(self, channel_id: str, name: str, platform: str) -> None:
        cid = _require_identifier(channel_id, "渠道编号")
        if cid in self.channels:
            raise DomainError(f"渠道已存在：{cid}")
        self.channels[cid] = Channel(cid, name, platform)

    def ship(self, shipment_id: str, pkg_id: str, channel_id: str, seller_id: str,
             quantity: int, shipped_on: date, region: str) -> None:
        sid = _require_identifier(shipment_id, "订单编号")
        if sid in self.shipments:
            raise DomainError(f"订单已存在：{sid}")
        if pkg_id not in self.packagings:
            raise DomainError(f"包装批不存在：{pkg_id}")
        if channel_id not in self.channels:
            raise DomainError(f"渠道不存在：{channel_id}")
        _require_identifier(seller_id, "销售主体")
        qty = _require_qty(quantity, "出货数量")
        if qty <= 0:
            raise DomainError("出货数量必须大于 0")
        if self.shipped_qty(pkg_id) + qty > self.packagings[pkg_id].quantity:
            raise DomainError(
                f"订单 {sid} 超量：包装批 {pkg_id} 出货总量不得超过产量"
            )
        self.shipments[sid] = Shipment(
            sid, pkg_id, channel_id, seller_id, qty, shipped_on, region
        )
        if region != self.home_region:
            self.coordinations[sid] = Coordination(sid, sid)
            self._log("跨区流向登记", "system", sid,
                      f"包装批 {pkg_id} 出货至 {region}，须上级协调")

    # ----- 抽样与风险依据 ---------------------------------------------------

    def record_sample(self, sample_id: str, pkg_id: str, indicator: str,
                      standard_limit: str, found_value: str,
                      nonconforming: bool, sampled_by: str,
                      sampled_on: date) -> Sample:
        sid = _require_identifier(sample_id, "抽样编号")
        if sid in self.samples:
            raise DomainError(f"抽样记录已存在：{sid}")
        if pkg_id not in self.packagings:
            raise DomainError(f"包装批不存在：{pkg_id}")
        _require_identifier(sampled_by, "抽检人员")
        sample = Sample(sid, pkg_id, indicator, standard_limit, found_value,
                        nonconforming, sampled_by, sampled_on)
        self.samples[sid] = sample
        self._log_on(sampled_on, "抽样", sampled_by, pkg_id,
                     f"{indicator} 实测 {found_value}，标准 {standard_limit}，"
                     f"{'不符合' if nonconforming else '符合'}")
        if nonconforming:
            self.direct_risk_pkgs.add(pkg_id)
        return sample

    def mark_material_abnormal(self, material_id: str, actor: str, reason: str) -> None:
        """原料来源异常：扩散到所有使用该原料的生产/包装批。"""
        if material_id not in self.materials:
            raise DomainError(f"原料批不存在：{material_id}")
        self.materials[material_id].status = NodeStatus.ABNORMAL
        self._log("原料异常认定", actor, material_id, reason)

    def mark_label_abnormal(self, label_id: str, actor: str, reason: str) -> None:
        """标签来源异常：扩散到所有贴该标签的包装批。"""
        if label_id not in self.labels:
            raise DomainError(f"标签不存在：{label_id}")
        self.labels[label_id].status = NodeStatus.ABNORMAL
        self._log("标签异常认定", actor, label_id, reason)

    def risk_basis(self, pkg_id: str) -> list[dict]:
        """一个包装批为什么是风险货：直接抽检、原料异常、标签异常。"""
        if pkg_id not in self.packagings:
            raise DomainError(f"包装批不存在：{pkg_id}")
        pkg = self.packagings[pkg_id]
        prod = self.productions[pkg.production_batch_id]
        basis: list[dict] = []
        for sample in self.samples.values():
            if sample.pkg_id == pkg_id and sample.nonconforming:
                basis.append({
                    "type": "抽检不符合",
                    "ref": sample.sample_id,
                    "detail": f"{sample.indicator} 实测 {sample.found_value}，"
                              f"核桃油标准 {sample.standard_limit}",
                    "responsible": sample.sampled_by,
                    "on": sample.sampled_on,
                })
        for mid in prod.material_ids:
            mat = self.materials[mid]
            if mat.status is NodeStatus.ABNORMAL:
                basis.append({
                    "type": "原料来源异常",
                    "ref": mid,
                    "detail": f"{mat.name}（供应商 {mat.supplier}）来源异常，经生产批 {prod.batch_id} 灌入",
                })
        for lid in pkg.label_ids:
            lab = self.labels[lid]
            if lab.status is NodeStatus.ABNORMAL:
                basis.append({
                    "type": "标签来源异常",
                    "ref": lid,
                    "detail": f"标签 {lab.brand}/{lab.product_name}（来源 {lab.source}）异常",
                })
        return basis

    def affected_packages(self) -> dict[str, list[dict]]:
        """全部真实受影响包装批及其风险依据（多来源自动去重）。"""
        affected: set[str] = set(self.direct_risk_pkgs)
        abnormal_materials = {m for m, v in self.materials.items()
                              if v.status is NodeStatus.ABNORMAL}
        abnormal_labels = {l for l, v in self.labels.items()
                           if v.status is NodeStatus.ABNORMAL}
        for pkg in self.packagings.values():
            prod = self.productions[pkg.production_batch_id]
            if abnormal_materials.intersection(prod.material_ids) or \
                    abnormal_labels.intersection(pkg.label_ids):
                affected.add(pkg.pkg_id)
        return {pid: self.risk_basis(pid) for pid in sorted(affected)}

    # ----- 封存与处置（在库） ----------------------------------------------

    def seal_stock(self, pkg_id: str, quantity: int, actor: str) -> None:
        """控制尚未出库的货物。"""
        self._require_pkg(pkg_id)
        qty = _require_qty(quantity, "封存数量")
        available = self._stock_on_hand(pkg_id)
        if qty <= 0 or qty > available:
            raise DomainError(
                f"封存数量须在 1..{available}（未出库且未封存/处置余量）之间"
            )
        self.sealed_stock[pkg_id] += qty
        self._log("封存", actor, pkg_id, f"封存未出库货物 {qty} 件")

    def destroy_stock(self, pkg_id: str, quantity: int, actor: str) -> None:
        self._require_pkg(pkg_id)
        qty = _require_qty(quantity, "销毁数量")
        sealed = self.sealed_stock[pkg_id]
        done = self.stock_destroyed[pkg_id]
        if qty <= 0 or done + qty > sealed:
            raise DomainError(f"只能销毁已封存货物，当前可销毁 {sealed - done} 件")
        self.stock_destroyed[pkg_id] += qty
        self._log("销毁", actor, pkg_id, f"销毁在库封存货物 {qty} 件")

    # ----- 复检（回避 + 期限） ----------------------------------------------

    def apply_retest(self, pkg_id: str, sample_id: str, applicant: str) -> RetestApplication:
        self._require_pkg(pkg_id)
        if sample_id not in self.samples or self.samples[sample_id].pkg_id != pkg_id:
            raise DomainError(f"抽样 {sample_id} 与包装批 {pkg_id} 不匹配")
        _require_identifier(applicant, "申请人")
        app_id = f"rt-{pkg_id}-{sample_id}"
        if app_id in self.retests:
            raise DomainError(f"复检申请已存在：{app_id}")
        deadline = self.today + timedelta(days=self.RETEST_WINDOW_DAYS)
        app = RetestApplication(app_id, pkg_id, sample_id, applicant,
                                self.today, deadline)
        self.retests[app_id] = app
        self._log("复检申请", applicant, pkg_id,
                  f"对抽样 {sample_id} 申请复检，期限至 {deadline}")
        return app

    def approve_retest(self, application_id: str, approver: str) -> None:
        """批准复检：批准人不得是该抽样的抽检人员（自己不能批准自己的复检）。"""
        app = self._get_retest(application_id)
        _require_identifier(approver, "批准人")
        if app.approved_by is not None:
            raise DomainError("该复检已批准")
        sample = self.samples[app.sample_id]
        if approver == sample.sampled_by:
            raise DomainError(
                f"抽检人员 {approver} 不能批准自己抽样（{sample.sample_id}）的复检，"
                "须更换批准人"
            )
        app.approved_by = approver
        self._log("复检批准", approver, app.pkg_id,
                  f"批准复检申请 {application_id}（抽样人 {sample.sampled_by} 已回避）")

    def decide_retest(self, application_id: str, conforming: bool,
                      released_qty: int, actor: str) -> None:
        """登记复检结论；合格则可放行对应数量（在库或出货渠道）。

        出货部分的复检放行由监管人员凭异地/渠道核对登记，且不得超过出货量。
        """
        app = self._get_retest(application_id)
        if app.approved_by is None:
            raise DomainError("复检未经批准，不能出结论")
        if self.today > app.deadline:
            raise DomainError(
                f"复检已逾期（期限 {app.deadline}），须按超期程序另行处置"
            )
        if actor == self.samples[app.sample_id].sampled_by:
            raise DomainError("复检结论不得由原抽检人员作出")
        qty = _require_qty(released_qty, "放行数量")
        app.result = "conforming" if conforming else "nonconforming"
        app.decided_on = self.today
        if not conforming:
            self._log("复检结论", actor, app.pkg_id,
                      f"复检 {app.application_id} 仍不符合，维持召回处置")
            return
        self._log("复检结论", actor, app.pkg_id,
                  f"复检 {app.application_id} 符合，放行 {qty} 件")
        if qty == 0:
            return
        # 优先放行在库已封存货物，其余计入出货部分（须凭渠道核对）
        stock_room = self.sealed_stock[app.pkg_id] - self.stock_destroyed[app.pkg_id] \
            - self.stock_released[app.pkg_id]
        from_stock = min(qty, stock_room)
        if from_stock:
            self.stock_released[app.pkg_id] += from_stock
        from_ship = qty - from_stock
        if from_ship:
            shipped_total = self.shipped_qty(app.pkg_id)
            accounted_ship = self.channel_accounted_qty(app.pkg_id)
            if accounted_ship + from_ship > shipped_total:
                raise DomainError(
                    f"出货部分可放行数量不足：申请放行 {from_ship}，余量 "
                    f"{shipped_total - accounted_ship}"
                )
            # 记到虚拟的“复检放行”桶，按出货订单平摊到各渠道用于守恒展示
            self._distribute_release(app.pkg_id, from_ship)

    def _distribute_release(self, pkg_id: str, qty: int) -> None:
        for shp in self._shipments_of(pkg_id):
            key = (pkg_id, shp.channel_id)
            resolved = (self.returned.get(key, 0) + self.destroyed.get(key, 0)
                        + self.released.get(key, 0))
            room = self._shipped_via(pkg_id, shp.channel_id) - resolved
            take = min(qty, room)
            if take > 0:
                self.released[key] = self.released.get(key, 0) + take
                qty -= take
            if qty == 0:
                break

    # ----- 渠道回报 ---------------------------------------------------------

    def submit_receipt(self, channel_id: str, pkg_id: str, receipt_no: str,
                       returned: int, destroyed: int, actor: str,
                       on: date | None = None) -> Receipt:
        """登记平台/渠道回执。

        - 同一 (平台, 回执号) 内容相同：幂等，不重复扣减；
        - 同一回执号内容矛盾：挂起该渠道，拒绝结案；
        - 累计回报不得超过该渠道该包装批的出货量。
        """
        if channel_id not in self.channels:
            raise DomainError(f"渠道不存在：{channel_id}")
        channel = self.channels[channel_id]
        if channel.suspended:
            raise DomainError(f"渠道 {channel.name} 已因回执矛盾挂起，暂停回报与结案")
        self._require_pkg(pkg_id)
        _require_identifier(receipt_no, "平台回执号")
        on = on or self.today
        ret = _require_qty(returned, "退货数量")
        des = _require_qty(destroyed, "销毁数量")
        if ret + des == 0:
            raise DomainError("回执至少要包含退货或销毁数量")
        key = (channel.platform, receipt_no)
        fingerprint = (pkg_id, ret, des)
        if key in self._receipts:
            prior = self._receipts[key]
            if (prior.pkg_id, prior.returned, prior.destroyed) == fingerprint:
                # 完全相同的回执：幂等返回，绝不重复扣减
                return prior
            channel.suspended = True
            self._log_on(on, "回执矛盾", actor, pkg_id,
                         f"平台 {channel.platform} 回执 {receipt_no} 内容前后不一致："
                         f"原报 退货{prior.returned}/销毁{prior.destroyed}，"
                         f"新报 退货{ret}/销毁{des}；渠道挂起，暂停结案")
            raise DomainError(f"回执 {receipt_no} 内容矛盾，渠道 {channel.name} 已挂起")

        ck = (pkg_id, channel_id)
        shipped_via = self._shipped_via(pkg_id, channel_id)
        if shipped_via == 0:
            raise DomainError(f"渠道 {channel.name} 没有包装批 {pkg_id} 的出货记录")
        already = self.returned.get(ck, 0) + self.destroyed.get(ck, 0) \
            + self.released.get(ck, 0)
        if already + ret + des > shipped_via:
            raise DomainError(
                f"回报超量：渠道 {channel.name} 对 {pkg_id} 出货 {shipped_via}，"
                f"已处理 {already}，本次再报 {ret + des}"
            )
        receipt = Receipt(receipt_no, channel.platform, channel_id, pkg_id,
                          ret, des, on, fingerprint)
        self._receipts[key] = receipt
        self.returned[ck] = self.returned.get(ck, 0) + ret
        self.destroyed[ck] = self.destroyed.get(ck, 0) + des
        self._log_on(on, "渠道回报", actor, pkg_id,
                     f"{channel.name} 回执 {receipt_no}：退货 {ret}，销毁 {des}")
        return receipt

    # ----- 跨地区协调 -------------------------------------------------------

    def coordinate_cross_region(self, flow_id: str, superior: str,
                                remote_authority: str, note: str) -> None:
        if flow_id not in self.coordinations:
            raise DomainError(f"跨区流向不存在：{flow_id}")
        _require_identifier(superior, "上级协调人")
        _require_identifier(remote_authority, "异地监管部门")
        coord = self.coordinations[flow_id]
        coord.superior = superior
        coord.remote_authority = remote_authority
        coord.note = note
        coord.coordinated = True
        self._log("跨区协调", superior, flow_id,
                  f"上级协调 {remote_authority}：{note}")

    # ----- 数量与守恒 -------------------------------------------------------

    def _require_pkg(self, pkg_id: str) -> PackagingBatch:
        if pkg_id not in self.packagings:
            raise DomainError(f"包装批不存在：{pkg_id}")
        return self.packagings[pkg_id]

    def _get_retest(self, application_id: str) -> RetestApplication:
        if application_id not in self.retests:
            raise DomainError(f"复检申请不存在：{application_id}")
        return self.retests[application_id]

    def _shipments_of(self, pkg_id: str) -> list[Shipment]:
        return [s for s in self.shipments.values() if s.pkg_id == pkg_id]

    def shipped_qty(self, pkg_id: str) -> int:
        return sum(s.quantity for s in self._shipments_of(pkg_id))

    def _shipped_via(self, pkg_id: str, channel_id: str) -> int:
        return sum(s.quantity for s in self._shipments_of(pkg_id)
                   if s.channel_id == channel_id)

    def _stock_on_hand(self, pkg_id: str) -> int:
        # 未出库且尚未封存的可控制余量；销毁/放行均取自已封存部分，不重复扣减
        pkg = self.packagings[pkg_id]
        return pkg.quantity - self.shipped_qty(pkg_id) - self.sealed_stock[pkg_id]

    def channel_accounted_qty(self, pkg_id: str) -> int:
        total = 0
        for cid in self.channels:
            key = (pkg_id, cid)
            total += self.returned.get(key, 0) + self.destroyed.get(key, 0) \
                + self.released.get(key, 0)
        return total

    def conservation(self, pkg_id: str) -> dict:
        """返回该包装批的守恒账，供办案人员核对。"""
        pkg = self._require_pkg(pkg_id)
        produced = pkg.quantity
        shipped = self.shipped_qty(pkg_id)
        in_stock = produced - shipped

        returned = destroyed = released = 0
        per_channel = []
        for ch in self.channels.values():
            key = (pkg_id, ch.channel_id)
            r, d, rel = (self.returned.get(key, 0), self.destroyed.get(key, 0),
                         self.released.get(key, 0))
            shipped_here = self._shipped_via(pkg_id, ch.channel_id)
            if shipped_here or r or d or rel:
                outstanding = shipped_here - r - d - rel
                per_channel.append({
                    "channel": ch.name,
                    "platform": ch.platform,
                    "shipped": shipped_here,
                    "returned": r,
                    "destroyed": d,
                    "released": rel,
                    "outstanding": outstanding,
                    "suspended": ch.suspended,
                })
            returned += r
            destroyed += d
            released += rel
        outstanding = shipped - returned - destroyed - released

        sealed = self.sealed_stock[pkg_id]
        stock_destroyed = self.stock_destroyed[pkg_id]
        stock_released = self.stock_released[pkg_id]
        unsealed = in_stock - sealed  # 尚未出库、也尚未封存的风险敞口
        controlled = sealed + returned + destroyed  # 现实控有
        # 在库待最终处置：未封存敞口 + 已封存但还没销毁/放行的部分
        stock_pending = unsealed + (sealed - stock_destroyed - stock_released)

        row = {
            "pkg_id": pkg_id,
            "produced": produced,
            "in_stock": in_stock,
            "shipped": shipped,
            "unsealed": unsealed,
            "sealed": sealed,
            "stock_destroyed": stock_destroyed,
            "stock_released": stock_released,
            "stock_pending": stock_pending,
            "returned": returned,
            "destroyed": destroyed,
            "released": released,
            "outstanding": outstanding,
            "controlled": controlled,
            "per_channel": per_channel,
        }
        # 守恒断言：产量与出货两条恒等式
        assert produced == in_stock + shipped, "产量侧不守恒"
        assert shipped == returned + destroyed + released + outstanding, "出货侧不守恒"
        assert in_stock == stock_destroyed + stock_released + stock_pending, "在库侧不守恒"
        return row

    # ----- 结案检查 ---------------------------------------------------------

    def closure_blockers(self) -> list[str]:
        blockers: list[str] = []
        affected = self.affected_packages()
        for pid in affected:
            row = self.conservation(pid)
            if row["outstanding"] > 0:
                blockers.append(f"包装批 {pid} 仍有 {row['outstanding']} 件未追回")
            if row["stock_pending"] > 0:
                blockers.append(f"包装批 {pid} 在库 {row['stock_pending']} 件尚未处置完毕")
            for ch in row["per_channel"]:
                if ch["suspended"]:
                    blockers.append(
                        f"渠道 {ch['channel']} 因回执矛盾挂起，需核查后才能结案")
        # 渠道挂起独立于批次风险认定：只要有未化解的矛盾回执就不能结案
        for ch in self.channels.values():
            if ch.suspended:
                blockers.append(f"渠道 {ch.name} 因回执矛盾挂起，需核查后才能结案")
        for app in self.retests.values():
            if app.result is None:
                state = "已逾期" if self.today > app.deadline else "待结论"
                blockers.append(f"复检 {app.application_id} {state}（期限 {app.deadline}）")
        for coord in self.coordinations.values():
            if not coord.coordinated:
                shp = self.shipments[coord.shipment_id]
                blockers.append(
                    f"订单 {coord.shipment_id} 流向 {shp.region} 跨地区，尚未经上级协调")
        # 去重保序
        return list(dict.fromkeys(blockers))

    def close_case(self) -> None:
        blockers = self.closure_blockers()
        if blockers:
            raise DomainError("暂不能结案：\n- " + "\n- ".join(blockers))
        self._log("结案", "system", "case", "全部受影响货物账实相符，证据链完整")

    # ----- 案情视图 ---------------------------------------------------------

    def open_package(self, pkg_id: str) -> dict:
        """办案人员打开一批货时看到的完整视图。"""
        pkg = self._require_pkg(pkg_id)
        prod = self.productions[pkg.production_batch_id]
        materials = [self.materials[m] for m in prod.material_ids]
        labels = [self.labels[l] for l in pkg.label_ids]
        destinations = []
        for shp in self._shipments_of(pkg_id):
            ch = self.channels[shp.channel_id]
            destinations.append({
                "shipment_id": shp.shipment_id,
                "channel": ch.name,
                "platform": ch.platform,
                "seller": shp.seller_id,
                "region": shp.region,
                "cross_region": shp.region != self.home_region,
                "coordinated": self.coordinations.get(
                    shp.shipment_id, Coordination("", "")
                ).coordinated,
                "quantity": shp.quantity,
                "shipped_on": shp.shipped_on,
            })
        decisions = [
            {
                "seq": e.seq, "on": e.on.isoformat(), "action": e.action,
                "responsible": e.actor, "target": e.target, "detail": e.detail,
            }
            for e in self.events
            if e.target == pkg_id or self._event_relates(e, pkg_id)
        ]
        return {
            "pkg_id": pkg_id,
            "filled_on": pkg.filled_on.isoformat(),
            "production_batch": {
                "batch_id": prod.batch_id,
                "produced_on": prod.produced_on.isoformat(),
                "line": prod.line,
                "materials": [
                    {"id": m.material_id, "name": m.name, "supplier": m.supplier,
                     "abnormal": m.status is NodeStatus.ABNORMAL}
                    for m in materials
                ],
            },
            "labels": [
                {"id": l.label_id, "brand": l.brand, "product_name": l.product_name,
                 "abnormal": l.status is NodeStatus.ABNORMAL}
                for l in labels
            ],
            "risk_basis": self.risk_basis(pkg_id),
            "is_affected": bool(self.risk_basis(pkg_id)),
            "destinations": destinations,
            "quantities": self.conservation(pkg_id),
            "decisions": decisions,
        }

    def _event_relates(self, event: Event, pkg_id: str) -> bool:
        # 与该包装批的订单、原料、抽样相关的事件也展示
        if event.target in {s.shipment_id for s in self._shipments_of(pkg_id)}:
            return True
        prod = self.productions[self.packagings[pkg_id].production_batch_id]
        if event.target in prod.material_ids:
            return True
        return any(s.sample_id == event.target and s.pkg_id == pkg_id
                   for s in self.samples.values())

    # ----- 证据链 -----------------------------------------------------------

    def _log(self, action: str, actor: str, target: str, detail: str = "") -> Event:
        return self._log_on(self.today, action, actor, target, detail)

    def _log_on(self, on: date, action: str, actor: str, target: str,
                detail: str = "") -> Event:
        event = Event(next(self._seq), on, action, actor, target, detail)
        self.events.append(event)
        return event

    def evidence_chain(self) -> list[dict]:
        return [
            {"seq": e.seq, "on": e.on.isoformat(), "action": e.action,
             "responsible": e.actor, "target": e.target, "detail": e.detail}
            for e in sorted(self.events, key=lambda x: (x.on, x.seq))
        ]
