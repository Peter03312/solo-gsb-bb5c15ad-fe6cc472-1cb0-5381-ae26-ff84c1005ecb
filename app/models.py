"""ORM 模型：批次、生产单、投料明细、幂等记录。"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Numeric 精度：整数 18 位、小数 4 位，满足食品厂计量场景
QTY = Numeric(18, 4)

# 默认库位编号：既有批次余量与新登记批次的初始数量都归入该库位；
# 生产产出与分装子批次也始终进入默认库位。
DEFAULT_LOCATION_CODE = "DEFAULT"


class Batch(Base):
    """批次：既可能是原料批次，也可能是生产产出批次。"""

    __tablename__ = "batches"

    batch_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    initial_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    remaining_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    # 被人工/检测标记为疑似污染的批次
    is_suspect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # 隔离中：自身疑似，或谱系上有疑似祖先；隔离批次禁止再投料
    is_quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    inputs: Mapped[list["ProductionInput"]] = relationship(
        back_populates="source_batch", foreign_keys="ProductionInput.source_no"
    )

    __table_args__ = (
        CheckConstraint("initial_qty > 0", name="ck_batch_initial_qty_positive"),
        CheckConstraint("remaining_qty >= 0", name="ck_batch_remaining_non_negative"),
        CheckConstraint("remaining_qty <= initial_qty", name="ck_batch_remaining_within"),
    )


class Production(Base):
    """一次生产：多笔投入 -> 一个产出批次。"""

    __tablename__ = "productions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    output_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    output_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 撤销（作废）：录入错误的生产单可整单撤销。撤销后行保留（原单据可查），
    # 但其投料边不再进入有效谱系/疑似传播，产出批次不得再投料。
    is_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    inputs: Mapped[list["ProductionInput"]] = relationship(
        back_populates="production", cascade="all, delete-orphan"
    )
    output: Mapped[Batch] = relationship(foreign_keys=[output_no])

    __table_args__ = (
        CheckConstraint("output_qty > 0", name="ck_production_output_positive"),
    )


class ProductionInput(Base):
    """生产单中的一笔原料消耗。"""

    __tablename__ = "production_inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    production_id: Mapped[int] = mapped_column(
        ForeignKey("productions.id", ondelete="CASCADE"), nullable=False
    )
    source_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    # 投料时指定的库位：未指定库位（按编号顺序跨库位扣减）时为 None。
    # 仅用于展示/排查；逐库位的实际消耗见 ProductionInputLocation。
    location_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    production: Mapped[Production] = relationship(back_populates="inputs")
    source_batch: Mapped[Batch] = relationship(
        back_populates="inputs", foreign_keys=[source_no]
    )

    __table_args__ = (
        UniqueConstraint("production_id", "source_no", name="uq_input_prod_source"),
        CheckConstraint("qty > 0", name="ck_production_input_qty_positive"),
        Index("ix_prod_input_source", "source_no"),
    )


class BatchSplit(Base):
    """批次分装关系：父批次 -> 一个可独立投料的子批次。

    分装只拆分余量、不产生生产单：父批次余量按 qty 扣减，子批次继承父批次
    单位、初始/当前余量均为 qty。子批次与普通批次一样可作为生产来源，也可
    再次分装。分装关系与投料关系一样进入谱系遍历与疑似隔离传播。
    """

    __tablename__ = "batch_splits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    # 一个子批次只能由一次分装创建（批次编号全局唯一、不得复用）
    child_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    parent: Mapped[Batch] = relationship(foreign_keys=[parent_no])
    child: Mapped[Batch] = relationship(foreign_keys=[child_no])

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_batch_split_qty_positive"),
        Index("ix_batch_split_parent", "parent_no"),
    )


class BatchLocation(Base):
    """批次在单个库位上的余量（库位层库存）。

    两层库存恒等式：同一批次全部 BatchLocation.qty 之和必须等于
    Batch.remaining_qty（全局余量，字段含义与引入库位前完全一致）。
    登记/产出/分装建批次时即写入默认库位行；既有批次由启动回填补建。
    """

    __tablename__ = "batch_locations"

    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, primary_key=True
    )
    location_code: Mapped[str] = mapped_column(String(32), nullable=False, primary_key=True)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch: Mapped[Batch] = relationship()

    __table_args__ = (
        CheckConstraint("qty >= 0", name="ck_batch_location_qty_non_negative"),
        Index("ix_batch_location_location", "location_code"),
    )


class ProductionInputLocation(Base):
    """一笔投料在单个库位上的实际消耗量。

    指定库位投料时恰有一行；未指定库位（按库位编号顺序跨库位扣减）时
    可有若干行，各行之和等于对应 ProductionInput.qty。生产撤销按这些
    行的“原消耗库位”逐笔返还，未指定库位的历史投料（回填为整笔计入
    默认库位）也照此路径返还。
    """

    __tablename__ = "production_input_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    production_input_id: Mapped[int] = mapped_column(
        ForeignKey("production_inputs.id", ondelete="CASCADE"), nullable=False
    )
    location_code: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    production_input: Mapped[ProductionInput] = relationship()

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_input_location_qty_positive"),
        Index("ix_input_location_input", "production_input_id"),
    )


class StockTransfer(Base):
    """库位调拨单：同一批次在两个库位间的一次正数数量移动。

    调拨只改变库位层分布，不改变批次全局余量。调拨单号全局唯一、
    不得复用；成功单据整行保留供幂等重放与审计。隔离批次允许调拨
    （隔离只禁止投料/分装），已撤销产出批次亦可调拨。
    """

    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    source_location: Mapped[str] = mapped_column(String(32), nullable=False)
    target_location: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch: Mapped[Batch] = relationship()

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_stock_transfer_qty_positive"),
        Index("ix_stock_transfer_batch", "batch_no"),
    )


class StockCheck(Base):
    """盘点单：一次仓库盘点凭唯一单号提交多条（批次, 库位）互异的行。

    盘点允许盘亏（实点量 < 账面量），不允许盘盈（实点量不得高于账面量），
    整单至少一条盘亏。成功后同事务按差额扣减对应库位行与批次全局余量；
    盘点单与明细行只追加、不修改，原账面量/实点量/差额/单位永久保留。
    单号全局唯一、不得复用。隔离批次与已撤销产出仍可核销盘亏。
    """

    __tablename__ = "stock_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_check_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 盘点单状态：成功提交后即为 POSTED；提交失败整单回滚、不留任何记录
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="POSTED")
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    lines: Mapped[list["StockCheckLine"]] = relationship(
        back_populates="stock_check", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_stock_checks_created", "created_at"),
    )


class StockCheckLine(Base):
    """盘点单中的一行：某批次在某库位上的账面量、实点量与差额。

    提交时逐行核对“当前库位实存恰等于账面量”，通过后按
    book_qty - actual_qty 扣减库位行与批次全局余量。行内原值为
    提交时刻的不可变快照，单位取批次单位一并固化（批次单位不会变更）。
    盘亏行（loss_qty > 0）还用于撤销校验：发生过盘亏的生产产出不得
    再撤销其原生产单。
    """

    __tablename__ = "stock_check_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_check_id: Mapped[int] = mapped_column(
        ForeignKey("stock_checks.id", ondelete="CASCADE"), nullable=False
    )
    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    location_code: Mapped[str] = mapped_column(String(32), nullable=False)
    book_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    actual_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    loss_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)

    stock_check: Mapped[StockCheck] = relationship(back_populates="lines")
    batch: Mapped[Batch] = relationship()

    __table_args__ = (
        CheckConstraint("book_qty >= 0", name="ck_stock_check_book_non_negative"),
        CheckConstraint("actual_qty >= 0", name="ck_stock_check_actual_non_negative"),
        CheckConstraint(
            "actual_qty <= book_qty", name="ck_stock_check_actual_within_book"
        ),
        CheckConstraint("loss_qty = book_qty - actual_qty", name="ck_stock_check_loss"),
        Index("ix_stock_check_line_batch_loc", "batch_no", "location_code"),
    )


class BatchReview(Base):
    """质检复核记录：确认某疑似批次为误报后撤销其疑似标记。

    每个批次至多一条有效复核；被复核批次必须当时处于疑似状态。
    复核只撤销“自身疑似”，隔离状态随后按谱系重算（混料来源仍疑似的批次继续隔离）。
    """

    __tablename__ = "batch_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    review_note: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch: Mapped[Batch] = relationship(foreign_keys=[batch_no])


class IdempotencyKey(Base):
    """请求幂等记录。

    同一 (method, path, key) 只允许一种请求体（sha256）；
    成功后固化状态码与响应体，重试原样返回，异内容报冲突。
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("method", "path", "key", name="uq_idem_endpoint_key"),
    )
