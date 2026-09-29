"""追召服务：风险扩散、数量守恒、回执幂等、复检回避、跨区协调与催办。

所有写入动作都向证据链追加一条带责任人和日期的记录；查询不依赖
可变的“系统运行状态”，复检期限按自然日计算，停机后仍继续推进。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Channel,
    ChannelState,
    Disposition,
    Evidence,
    EvidenceType,
    Label,
    MaterialLot,
    PackageBatch,
    ProductionBatch,
    RecheckRequest,
    Shipment,
)


class RecallError(ValueError):
    """业务规则冲突。"""


@dataclass
class ShipmentLedger:
    """单张出货单的去向台账：四项之和恒等于出货量。"""
    shipment_id: str
    shipped: int
    returned: int = 0
    destroyed: int = 0
    released: int = 0

    @property
    def accounted(self) -> int:
        return self.returned + self.destroyed + self.released

    @property
    def outstanding(self) -> int:
        """仍未追回数量。"""
        return self.shipped - self.accounted


class RecallRegistry:
    def __init__(self, home_region: str) -> None:
        self.home_region = home_region
        self.materials: dict[str, MaterialLot] = {}
        self.labels: dict[str, Label] = {}
        self.productions: dict[str, ProductionBatch] = {}
        self.packages: dict[str, PackageBatch] = {}
        self.channels: dict[str, Channel] = {}
        self.shipments: dict[str, Shipment] = {}
        self.evidence: list[Evidence] = []
        self.rechecks: dict[str, RecheckRequest] = {}
        self.channel_states: dict[str, ChannelState] = {}
        self.closed_channels: set[str] = set()
        self._ledgers: dict[str, ShipmentLedger] = {}

    # ------------------------------------------------------------ 建档
    def add_material(self, m: MaterialLot) -> None:
        self.materials[m.id] = m

    def add_label(self, lab: Label) -> None:
        self.labels[lab.id] = lab

    def add_production(self, p: ProductionBatch) -> None:
        if p.material_id not in self.materials:
            raise RecallError(f"生产批 {p.id} 引用了不存在的原料 {p.material_id}")
        self.productions[p.id] = p

    def add_package(self, pkg: PackageBatch) -> None:
        if pkg.production_id not in self.productions:
            raise RecallError(f"包装批 {pkg.id} 引用了不存在的生产批 {pkg.production_id}")
        if pkg.label_id not in self.labels:
            raise RecallError(f"包装批 {pkg.id} 引用了不存在的标签 {pkg.label_id}")
        split_total = pkg.qty + sum(
            x.qty for x in self.packages.values() if x.production_id == pkg.production_id
        )
        produced = self.productions[pkg.production_id].produced_qty
        if split_total > produced:
            raise RecallError(
                f"生产批 {pkg.production_id} 拆出包装合计 {split_total} 超过产量 {produced}"
            )
        self.packages[pkg.id] = pkg

    def add_channel(self, c: Channel) -> None:
        self.channels[c.id] = c
        self.channel_states[c.id] = ChannelState(channel_id=c.id)

    def add_shipment(self, s: Shipment) -> None:
        if s.package_id not in self.packages:
            raise RecallError(f"出货单 {s.id} 引用了不存在的包装批 {s.package_id}")
        if s.channel_id not in self.channels:
            raise RecallError(f"出货单 {s.id} 引用了不存在的渠道 {s.channel_id}")
        shipped = s.qty + sum(
            x.qty for x in self.shipments.values() if x.package_id == s.package_id
        )
        if shipped > self.packages[s.package_id].qty:
            raise RecallError(
                f"包装批 {s.package_id} 出货合计 {shipped} 超过包装量 {self.packages[s.package_id].qty}"
            )
        self.shipments[s.id] = s
        self._ledgers[s.id] = ShipmentLedger(s.id, s.qty)

    # ------------------------------------------------------------ 证据动作
    def _log(self, kind: EvidenceType, at, by: str, target: str, detail: str,
             qty: int = 0, disposition: Disposition | None = None) -> None:
        self.evidence.append(Evidence(kind, at, by, target, detail, qty, disposition))

    def record_sampling(self, production_id: str, indicator: str, limit: str,
                        observed: str, inspector: str, at) -> None:
        p = self._require_production(production_id)
        p.sampling_inspector = inspector
        p.risk_indicator, p.risk_limit, p.risk_observed = indicator, limit, observed
        self._log(EvidenceType.SAMPLING, at, inspector, production_id,
                  f"{indicator}实测{observed}，标准限值{limit}")

    def seal(self, target: str, qty: int, by: str, at, detail: str = "") -> None:
        if target not in self.materials and target not in self.productions \
                and target not in self.packages:
            raise RecallError(f"封存对象 {target} 不存在")
        if qty <= 0:
            raise RecallError("封存数量必须为正")
        self._log(EvidenceType.SEAL, at, by, target, detail or "就地封存", qty)

    def submit_recheck(self, request: RecheckRequest) -> None:
        if request.target not in self.productions and request.target not in self.packages:
            raise RecallError(f"复检对象 {request.target} 不存在")
        if request.deadline < request.submitted_at:
            raise RecallError("复检期限不得早于申请日期")
        self.rechecks[request.id] = request
        self._log(EvidenceType.RECHECK_SUBMIT, request.submitted_at, request.inspector,
                  request.target, f"申请复检，期限 {request.deadline}")

    def decide_recheck(self, request_id: str, approver: str, approved: bool, at) -> None:
        req = self.rechecks.get(request_id)
        if req is None:
            raise RecallError(f"复检申请 {request_id} 不存在")
        if req.decided:
            raise RecallError(f"复检申请 {request_id} 已作出决定，不得重复批准")
        # 抽检人员不能批准自己的复检
        if approver == req.inspector:
            raise RecallError("抽检人员不能批准自己抽取样品的复检，须换人批准")
        req.decided, req.approver, req.approved, req.decided_at = True, approver, approved, at
        overdue = at > req.deadline
        note = "（停机期间期限连续计算，决定已逾期）" if overdue else ""
        self._log(EvidenceType.RECHECK_DECISION, at, approver, req.target,
                  f"{'准许复检/放行' if approved else '维持不合格结论'}{note}")

    def coordinate(self, shipment_id: str, by: str, at, note: str = "") -> None:
        """跨地区流向须由上级协调后方可处置/结案。"""
        s = self._require_shipment(shipment_id)
        if s.region == self.home_region:
            raise RecallError(f"出货单 {shipment_id} 属本辖区，无需跨区协调")
        self.shipments[shipment_id] = Shipment(
            s.id, s.package_id, s.channel_id, s.seller, s.qty, s.region, s.shipped_at, True
        )
        self._log(EvidenceType.COORDINATION, at, by, shipment_id,
                  note or f"上级协调 {self.home_region} -> {s.region} 跨区流向")

    # ------------------------------------------------------------ 去向登记
    def _apply(self, shipment_id: str, disposition: Disposition, qty: int) -> None:
        if qty <= 0:
            raise RecallError("登记数量必须为正")
        led = self._ledgers[shipment_id]
        if led.accounted + qty > led.shipped:
            raise RecallError(
                f"出货单 {shipment_id} 登记去向合计 {led.accounted + qty} 超过出货量 {led.shipped}，"
                "退货+销毁+放行+未追回必须与出货量守恒"
            )
        if disposition is Disposition.RETURNED:
            led.returned += qty
        elif disposition is Disposition.DESTROYED:
            led.destroyed += qty
        else:
            led.released += qty

    def dispose(self, shipment_id: str, disposition: Disposition, qty: int,
                by: str, at, detail: str = "") -> None:
        """执法人员现场登记退货/销毁/复检放行。"""
        self._require_shipment(shipment_id)
        self._apply(shipment_id, disposition, qty)
        self._log(EvidenceType.DISPOSAL, at, by, shipment_id,
                  detail or f"现场登记{disposition.value}", qty, disposition)

    def submit_receipt(self, channel_id: str, receipt_id: str, shipment_id: str,
                       disposition: Disposition, qty: int, at) -> None:
        """平台/中间商回报回执。

        - 同一回执号、内容一致：幂等，不重复扣减；
        - 同一回执号、内容矛盾：暂停该渠道结案；
        - 累计数量与出货量矛盾：同样暂停结案。
        """
        state = self.channel_states.get(channel_id)
        if state is None:
            raise RecallError(f"渠道 {channel_id} 不存在")
        if state.held:
            raise RecallError(f"渠道 {channel_id} 已因回执矛盾暂停结案，须先核查")
        shipment = self._require_shipment(shipment_id)
        if shipment.channel_id != channel_id:
            raise RecallError(f"出货单 {shipment_id} 不属于渠道 {channel_id}")
        payload = (shipment_id, disposition.value, qty)
        if receipt_id in state.receipts:
            if state.receipts[receipt_id] == payload:
                return  # 相同回执幂等返回，不重复扣减
            state.held, state.hold_reason = True, f"回执 {receipt_id} 内容前后矛盾"
            self._log(EvidenceType.RECEIPT, at, channel_id, shipment_id,
                      f"回执 {receipt_id} 内容矛盾，渠道暂停结案")
            raise RecallError(state.hold_reason)
        try:
            self._apply(shipment_id, disposition, qty)
        except RecallError:
            state.held, state.hold_reason = True, f"回执 {receipt_id} 数量与出货量矛盾"
            raise
        state.receipts[receipt_id] = payload
        self._log(EvidenceType.RECEIPT, at, channel_id, shipment_id,
                  f"回执 {receipt_id} 报{disposition.value}{qty}", qty, disposition)

    def resolve_hold(self, channel_id: str, by: str, at, note: str) -> None:
        """矛盾核查清楚后解除暂停（保留决定记录）。"""
        state = self.channel_states.get(channel_id)
        if state is None or not state.held:
            raise RecallError(f"渠道 {channel_id} 当前无暂停记录")
        state.held, state.hold_reason = False, None
        self._log(EvidenceType.RECEIPT, at, by, channel_id, f"解除暂停：{note}")

    # ------------------------------------------------------------ 查询
    def _require_production(self, pid: str) -> ProductionBatch:
        if pid not in self.productions:
            raise RecallError(f"生产批 {pid} 不存在")
        return self.productions[pid]

    def _require_shipment(self, sid: str) -> Shipment:
        if sid not in self.shipments:
            raise RecallError(f"出货单 {sid} 不存在")
        return self.shipments[sid]

    def risk_basis(self, production_id: str) -> list[str]:
        """风险依据：抽检超标、原料来源异常、标签来源异常。"""
        p = self._require_production(production_id)
        basis: list[str] = []
        if p.risk_indicator:
            basis.append(
                f"抽检{p.risk_indicator}实测{p.risk_observed}，超出核桃油标准限值{p.risk_limit}"
                f"（抽样人：{p.sampling_inspector}）"
            )
        m = self.materials[p.material_id]
        if m.source_anomalous:
            basis.append(f"原料批 {m.id}（{m.name}，供应商 {m.supplier}）来源异常")
        for pkg in self.packages.values():
            if pkg.production_id != production_id:
                continue
            lab = self.labels[pkg.label_id]
            if lab.source_anomalous:
                basis.append(f"包装批 {pkg.id} 使用的标签 {lab.id}（{lab.brand}）来源异常")
        return basis

    def affected_package_ids(self, production_id: str) -> list[str]:
        """原料异常/抽检超标 -> 整批；仅标签异常 -> 仅涉事包装批。"""
        self._require_production(production_id)
        whole_batch = bool(self.risk_basis(production_id) and (
            self.productions[production_id].risk_indicator is not None
            or self.materials[self.productions[production_id].material_id].source_anomalous
        ))
        result = []
        for pkg in self.packages.values():
            if pkg.production_id != production_id:
                continue
            label_bad = self.labels[pkg.label_id].source_anomalous
            if whole_batch or label_bad:
                result.append(pkg.id)
        return result

    def affected_quantity(self, production_id: str) -> int:
        """真实受影响数量：按风险扩散范围汇总包装量，而非按品牌整批查封。"""
        return sum(self.packages[pid].qty for pid in self.affected_package_ids(production_id))

    def sealed_qty(self, production_id: str) -> int:
        """已就地封存数量：该生产批及其包装批、对应原料批上的封存证据。"""
        p = self._require_production(production_id)
        # 封存可先于风险定性作出，故按原料批、生产批及全部包装批统计
        targets = {production_id, p.material_id,
                   *(x.id for x in self.packages.values() if x.production_id == production_id)}
        return sum(e.qty for e in self.evidence
                   if e.type is EvidenceType.SEAL and e.target in targets)

    def ledger(self, shipment_id: str) -> ShipmentLedger:
        return self._ledgers[shipment_id]

    def overdue_rechecks(self, today) -> list[RecheckRequest]:
        """催办：逾期未决的复检申请。期限按自然日推进，系统停机不顺延。"""
        return [r for r in self.rechecks.values()
                if not r.decided and r.deadline < today]

    def decisions(self, production_id: str) -> list[Evidence]:
        """该批相关的全部证据链记录（抽样/封存/复检/处置/协调/回执）。"""
        self._require_production(production_id)
        pkg_ids = {x.id for x in self.packages.values() if x.production_id == production_id}
        ship_ids = {s.id for s in self.shipments.values() if s.package_id in pkg_ids}
        chan_ids = {s.channel_id for s in self.shipments.values() if s.id in ship_ids}
        return [e for e in self.evidence
                if e.target in {production_id, self.productions[production_id].material_id}
                or e.target in pkg_ids or e.target in ship_ids or e.target in chan_ids]

    def can_close_channel(self, channel_id: str, today) -> tuple[bool, str]:
        state = self.channel_states.get(channel_id)
        if state is None:
            raise RecallError(f"渠道 {channel_id} 不存在")
        if state.held:
            return False, f"回执矛盾未排除：{state.hold_reason}"
        for s in self.shipments.values():
            if s.channel_id != channel_id:
                continue
            if s.region != self.home_region and not s.coordinated:
                return False, f"出货单 {s.id} 跨区流向 {s.region} 尚未经上级协调"
            if self._ledgers[s.id].outstanding:
                return False, (f"出货单 {s.id} 仍有 {self._ledgers[s.id].outstanding} "
                               "件未追回，数量未守恒结清")
        channel_shipments = [s for s in self.shipments.values() if s.channel_id == channel_id]
        targets = set()
        for s in channel_shipments:
            pkg = self.packages[s.package_id]
            targets.add(pkg.id)
            targets.add(pkg.production_id)
        pending = [r for r in self.rechecks.values()
                   if not r.decided and r.target in targets]
        if pending:
            return False, f"复检申请 {pending[0].id} 尚未作出决定"
        return True, "全部出货去向守恒结清，可结案"

    def close_channel(self, channel_id: str, by: str, at) -> None:
        ok, reason = self.can_close_channel(channel_id, at)
        if not ok:
            raise RecallError(f"渠道 {channel_id} 不能结案：{reason}")
        self.closed_channels.add(channel_id)
        self._log(EvidenceType.DISPOSAL, at, by, channel_id, "渠道追召结案")

    def overview(self, production_id: str, today) -> dict:
        """办案人员打开一批货时看到的完整画面。"""
        p = self._require_production(production_id)
        affected = set(self.affected_package_ids(production_id))
        packages_view = []
        controlled_recalled = 0
        for pkg in self.packages.values():
            if pkg.production_id != production_id:
                continue
            shipments_view = []
            unsold = pkg.qty
            for s in self.shipments.values():
                if s.package_id != pkg.id:
                    continue
                led = self._ledgers[s.id]
                unsold -= s.qty
                controlled_recalled += led.returned + led.destroyed
                shipments_view.append({
                    "出货单": s.id, "渠道": self.channels[s.channel_id].name,
                    "销售主体": s.seller, "地区": s.region,
                    "跨区": s.region != self.home_region,
                    "上级已协调": s.coordinated,
                    "出货量": led.shipped, "退货": led.returned,
                    "销毁": led.destroyed, "复检放行": led.released,
                    "未追回": led.outstanding,
                    "守恒": led.accounted + led.outstanding == led.shipped,
                })
            packages_view.append({
                "包装批": pkg.id, "标签": self.labels[pkg.label_id].brand,
                "包装量": pkg.qty, "受影响": pkg.id in affected,
                "未出库": unsold, "出货": shipments_view,
            })
        sealed = self.sealed_qty(production_id)
        return {
            "生产批": production_id,
            "产量": p.produced_qty,
            "风险依据": self.risk_basis(production_id),
            "真实受影响数量": self.affected_quantity(production_id),
            "谱系与去向": packages_view,
            "已控制数量": {
                "封存": sealed,
                "追回控制(退货+销毁)": controlled_recalled,
                "合计": sealed + controlled_recalled,
            },
            "复检催办": [
                {"申请": r.id, "对象": r.target, "抽样人": r.inspector,
                 "期限": str(r.deadline), "逾期天数": (today - r.deadline).days}
                for r in self.rechecks.values()
                if not r.decided and r.target in {production_id, *{x.id for x in self.packages.values() if x.production_id == production_id}}
            ],
            "渠道状态": {
                c.name: ("暂停结案：" + (st.hold_reason or "")) if st.held
                else ("已结案" if c.id in self.closed_channels else "追召中")
                for c in self.channels.values() for st in [self.channel_states[c.id]]
                if any(s.channel_id == c.id for s in
                       (x for x in self.shipments.values()
                        if x.package_id in {z.id for z in self.packages.values()
                                            if z.production_id == production_id}))
            },
            "证据链": [
                {"时间": str(e.at), "类型": e.type.value, "责任人": e.by,
                 "对象": e.target, "说明": e.detail,
                 **({"数量": e.qty} if e.qty else {}),
                 **({"处置": e.disposition.value} if e.disposition else {})}
                for e in self.decisions(production_id)
            ],
        }
