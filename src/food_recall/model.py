"""食品风险批次追召领域模型。

谱系：原料批 -> 生产批（可抽样/封存/复检）-> 多个包装批（贴标签）
      -> 出货单 -> 渠道与销售主体（中间商/直播平台，可跨地区）。
所有执法动作（抽样、封存、复检、处置、协调、平台回执）都是只追加的
证据记录，每条都带责任人和时间，构成证据链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


# ---------------------------------------------------------------- 基础档案

@dataclass(frozen=True)
class MaterialLot:
    """原料批，例如购入的大豆油（标称核桃油原料）。"""
    id: str
    name: str
    supplier: str
    received_qty: int          # 入库数量（单位：瓶/件，全系统统一计量单位）
    source_anomalous: bool = False  # 原料来源是否异常（以次充好、票货不符等）


@dataclass(frozen=True)
class Label:
    """产品标签；标签来源异常（套标、假冒授权）时同样构成风险扩散源。"""
    id: str
    brand: str
    licensor: str              # 标称授权方
    source_anomalous: bool = False


@dataclass
class ProductionBatch:
    """生产/灌装批：一个生产批可拆成多个包装批。"""
    id: str
    material_id: str
    produced_qty: int
    sampling_inspector: str | None = None  # 抽样人员（复检批准须回避此人）
    risk_indicator: str | None = None      # 超标指标，如“棕榈酸”
    risk_limit: str | None = None          # 核桃油标准限值
    risk_observed: str | None = None       # 实测值


@dataclass
class PackageBatch:
    """包装批：同一生产批灌装后贴不同标签形成不同包装批。"""
    id: str
    production_id: str
    label_id: str
    qty: int


@dataclass(frozen=True)
class Channel:
    """销售渠道/销售主体：中间商或直播平台店铺。"""
    id: str
    name: str
    kind: str                  # “中间商” / “直播平台”
    region: str


@dataclass(frozen=True)
class Shipment:
    """出货单：包装批经某渠道、由某销售主体卖往某地区。"""
    id: str
    package_id: str
    channel_id: str
    seller: str                # 销售主体（店铺/中间商主体名称）
    qty: int
    region: str
    shipped_at: date
    coordinated: bool = False  # 跨地区流向是否已经上级协调


# ---------------------------------------------------------------- 证据链

class EvidenceType(str, Enum):
    SAMPLING = "抽样"
    SEAL = "封存"
    RECHECK_SUBMIT = "复检申请"
    RECHECK_DECISION = "复检决定"
    DISPOSAL = "处置"
    COORDINATION = "跨区协调"
    RECEIPT = "平台回执"


class Disposition(str, Enum):
    RETURNED = "退货"
    DESTROYED = "销毁"
    RELEASED = "复检放行"


@dataclass(frozen=True)
class Evidence:
    """证据链上的一条记录，只追加、不修改。"""
    type: EvidenceType
    at: date
    by: str                    # 责任人/执法人员
    target: str                # 关联对象（生产批/包装批/出货单/渠道）
    detail: str
    qty: int = 0
    disposition: Disposition | None = None


@dataclass
class RecheckRequest:
    """复检申请：有法定期限，期限按自然日连续计算（停机不顺延）。"""
    id: str
    target: str                # 被复检的生产批/包装批
    inspector: str             # 申请人/原抽样人员，不得自行批准
    submitted_at: date
    deadline: date
    decided: bool = False
    approver: str | None = None
    approved: bool = False
    decided_at: date | None = None


@dataclass
class ChannelState:
    """渠道结案状态：回执矛盾时暂停结案。"""
    channel_id: str
    held: bool = False
    hold_reason: str | None = None
    receipts: dict[str, object] = field(default_factory=dict)  # 回执号 -> 载荷
