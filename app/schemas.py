"""请求/响应模型。

数量统一走 ``Decimal`` 精确计算，拒绝零、负数与 NaN/Inf；响应再序列化为数字。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, field_validator


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("数量不能为布尔值")
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # invalid literal
        raise ValueError("数量必须是十进制数字") from exc
    if not dec.is_finite():
        raise ValueError("数量必须是有限数字")
    if dec <= 0:
        raise ValueError("数量必须为正数")
    return dec


def _to_non_negative_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("数量不能为布尔值")
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # invalid literal
        raise ValueError("数量必须是十进制数字") from exc
    if not dec.is_finite():
        raise ValueError("数量必须是有限数字")
    if dec < 0:
        raise ValueError("数量不得为负数")
    return dec


# 非负数量：盘点的账面量与实点量允许为 0（空库位/全部盘亏）
NonNegativeQty = Annotated[Decimal, BeforeValidator(_to_non_negative_decimal)]

# 正数数量：入参即完成 > 0 校验
PositiveQty = Annotated[Decimal, BeforeValidator(_to_decimal)]

BatchNo = Annotated[str, Field(min_length=1, max_length=64, examples=["RM-20260930-01"])]
Unit = Annotated[str, Field(min_length=1, max_length=16, examples=["kg"])]


def _to_location_code(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("库位编号必须为字符串")
    code = value.strip()
    if not code:
        raise ValueError("库位编号不得为空白")
    if len(code) > 32:
        raise ValueError("库位编号长度不得超过 32")
    return code


# 库位编号：非空、非纯空白、最长 32；两端空白自动去除
LocationCode = Annotated[str, BeforeValidator(_to_location_code)]


class RawBatchIn(BaseModel):
    batch_no: BatchNo
    unit: Unit
    quantity: PositiveQty
    note: str | None = Field(default=None, max_length=255)


class ConsumptionIn(BaseModel):
    batch_no: BatchNo
    quantity: PositiveQty
    # 投料库位：未指定（省略/为 null）时按库位编号顺序跨库位扣减；
    # 指定时仅扣减该库位（该库位不存在或余额不足整笔拒绝）。
    location_code: LocationCode | None = None


class ProductionIn(BaseModel):
    output_batch_no: BatchNo
    unit: Unit
    output_quantity: PositiveQty
    inputs: list[ConsumptionIn] = Field(min_length=1)


class SuspectIn(BaseModel):
    batch_no: BatchNo
    note: str | None = Field(default=None, max_length=255)


class SplitTargetIn(BaseModel):
    """一笔分装：新子批次编号（全局不得复用）与正数分装量。"""

    batch_no: BatchNo
    quantity: PositiveQty


class SplitIn(BaseModel):
    """按父批次编号提交一笔分装：多个互异新编号 + 各自正数分装量。"""

    splits: list[SplitTargetIn] = Field(min_length=1)
    # 分装出库库位：未指定时按库位编号顺序跨库位扣减父批次；指定时仅扣该库位。
    # 分装产生的子批次一律进入默认库位。
    location_code: LocationCode | None = None


class TransferIn(BaseModel):
    """库位调拨：同一批次在两个不同库位间移动正数数量。"""

    transfer_no: Annotated[str, Field(min_length=1, max_length=64, examples=["TR-20261001-01"])]
    batch_no: BatchNo
    source_location: LocationCode
    target_location: LocationCode
    quantity: PositiveQty

    @field_validator("target_location")
    @classmethod
    def _locations_must_differ(cls, value: str, info: ValidationInfo) -> str:
        source = info.data.get("source_location")
        # source_location 先于 target_location 校验，LocationCode 已去空白；
        # 去空白后相同（如 " A " 与 "A"）即视为同一库位，整单拒绝。
        if source is not None and source == value:
            raise ValueError("来源库位与目标库位不得相同")
        return value


class StockCheckLineIn(BaseModel):
    """盘点一行：批次 + 库位 + 账面量 + 实点量。

    两种数量均允许为 0，但实点量不得高于账面量；同一盘点单内
    （批次, 库位）必须互异，整单至少一条盘亏（服务层校验）。
    """

    batch_no: BatchNo
    location_code: LocationCode
    book_qty: NonNegativeQty
    actual_qty: NonNegativeQty

    @field_validator("actual_qty")
    @classmethod
    def _actual_not_above_book(cls, value: Decimal, info: ValidationInfo) -> Decimal:
        book = info.data.get("book_qty")
        if book is not None and value > book:
            raise ValueError("实点量不得高于账面量（盘点不允许盘盈）")
        return value


class StockCheckIn(BaseModel):
    """凭唯一盘点单号提交多条互异盘点行；整单至少一条盘亏。"""

    stock_check_no: Annotated[
        str, Field(min_length=1, max_length=64, examples=["SC-20261001-01"])
    ]
    lines: list[StockCheckLineIn] = Field(min_length=1)


class ReviewIn(BaseModel):
    batch_no: BatchNo
    review_note: str = Field(min_length=1, max_length=1000, examples=["复检合格：留样复测未检出农残"])

    @field_validator("review_note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("复核说明不得为空白")
        return value


class CancelIn(BaseModel):
    # 撤销原因非空、非空白；凭产出批次编号（路径）+ 撤销原因撤销生产单
    cancel_reason: str = Field(min_length=1, max_length=1000, examples=["产出编号录入错误，实际应为 MIX-1B"])

    @field_validator("cancel_reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("撤销原因不得为空白")
        return value


# ---------- 响应 ----------


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BatchOut(_ORMModel):
    batch_no: str
    unit: str
    initial_qty: float
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool
    is_cancelled: bool = False
    note: str | None = None


class ConsumptionOut(_ORMModel):
    batch_no: str
    quantity: float


class ProductionOut(_ORMModel):
    output_batch_no: str
    unit: str
    output_quantity: float
    inputs: list[ConsumptionOut]


class ReturnedInputOut(BaseModel):
    """撤销时返还给某来源批次的一笔原投料量。"""

    batch_no: str
    returned_quantity: float


class CancellationOut(BaseModel):
    """生产单撤销结果：产出编号、返还明细、撤销状态。"""

    output_batch_no: str
    status: str = "CANCELLED"
    cancel_reason: str
    cancelled_at: str
    returned_inputs: list[ReturnedInputOut]


class ProductionRecordOut(BaseModel):
    """生产原单据（含撤销信息），供按产出编号查询。"""

    output_batch_no: str
    unit: str
    output_quantity: float
    inputs: list[ConsumptionOut]
    is_cancelled: bool
    cancel_reason: str | None = None
    cancelled_at: str | None = None


class NodeOut(BaseModel):
    batch_no: str
    unit: str
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool
    is_cancelled: bool = False
    depth: int


class LineageOut(BaseModel):
    batch_no: str
    is_cancelled: bool = False
    upstream: list[NodeOut]
    downstream: list[NodeOut]
    edges: list[dict[str, str]]


class DescendantsOut(BaseModel):
    source: str
    is_suspect: bool
    affected_batch_nos: list[str]
    newly_quarantined: list[str]


class ReviewOut(BaseModel):
    reviewed_batch_no: str
    is_suspect: bool
    review_note: str
    affected_batch_nos: list[str]
    released_batch_nos: list[str]
    still_quarantined: list[str]


class SplitBatchOut(BaseModel):
    """分装产生的子批次：编号、继承的单位与分装余量。"""

    batch_no: str
    unit: str
    quantity: float
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool


class SplitRelationOut(BaseModel):
    """分装关系：父批次 -> 子批次（分装量）。"""

    parent_batch_no: str
    child_batch_no: str
    quantity: float


class SplitOut(BaseModel):
    """分装结果：父批次新余量、子批次清单与分装关系。"""

    parent_batch_no: str
    parent_remaining_qty: float
    splits: list[SplitBatchOut]
    relations: list[SplitRelationOut]


class LocationBalanceOut(BaseModel):
    """批次在单个库位上的余额。"""

    location_code: str
    quantity: float


class BatchLocationsOut(BaseModel):
    """批次的库位层库存：逐库位余额（按库位编号排序）与全局余量。

    恒等式：locations 各项 quantity 之和 == remaining_qty（全局余量）。
    """

    batch_no: str
    unit: str
    remaining_qty: float
    locations: list[LocationBalanceOut]


class TransferOut(BaseModel):
    """调拨结果：单号、批次、来源/目标库位与调拨后两库位余额。"""

    transfer_no: str
    batch_no: str
    source_location: str
    target_location: str
    quantity: float
    source_balance: float
    target_balance: float
    remaining_qty: float
    locations: list[LocationBalanceOut]


class StockCheckLineOut(BaseModel):
    """盘点行原值快照：账面量、实点量、差额（= 账面量 - 实点量）与单位。"""

    batch_no: str
    location_code: str
    book_qty: float
    actual_qty: float
    loss_qty: float
    unit: str


class StockCheckOut(BaseModel):
    """盘点单：单号、状态、提交时间与逐行原值/差额（按提交顺序返回）。"""

    stock_check_no: str
    status: str
    checked_at: str
    lines: list[StockCheckLineOut]
