"""业务逻辑层。所有写操作都在单个事务内完成，校验失败整体回滚。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import errors
from .models import (
    DEFAULT_LOCATION_CODE,
    Batch,
    BatchLocation,
    BatchReview,
    BatchSplit,
    IdempotencyKey,
    Production,
    ProductionInput,
    ProductionInputLocation,
    ReturnOrder,
    ReturnOrderLine,
    Shipment,
    ShipmentLine,
    StockCheck,
    StockCheckLine,
    StockTransfer,
)
from .schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    ReturnIn,
    ReviewIn,
    ShipmentIn,
    ShipmentLineIn,
    SplitIn,
    StockCheckIn,
    StockCheckLineIn,
    SuspectIn,
    TransferIn,
)

# ---------------------------------------------------------------- 幂等支持


def _canonical_hash(payload: dict[str, Any]) -> str:
    """对请求体做规范序列化后取 sha256；Decimal 用原始标量文本表示。"""

    def default(obj: object) -> object:
        if isinstance(obj, Decimal):
            return f"decimal:{normalize_decimal(obj)}"
        raise TypeError(f"不可序列化的类型 {type(obj)!r}")

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def normalize_decimal(value: Decimal) -> str:
    """1 与 1.00 视为同一内容；去掉多余尾零。"""
    return format(value.normalize(), "f")


def idempotent(
    session: Session,
    *,
    method: str,
    path: str,
    key: str | None,
    payload: dict[str, Any],
    action: Callable[[], tuple[int, Any]],
) -> tuple[int, Any]:
    """以幂等键执行动作。

    - 无 key：直接执行（查询类接口不需要）；
    - 同 key + 同内容（含重试）：返回首次固化的状态码与响应；
    - 同 key + 异内容：409 冲突；
    - 失败请求不落幂等记录，允许客户端修正后以同 key 重试。
    """
    if not key:
        status, body = action()
        return status, body

    request_hash = _canonical_hash(payload)
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.method == method,
        IdempotencyKey.path == path,
        IdempotencyKey.key == key,
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AppError_conflict(key)
        return existing.status_code, json.loads(existing.response_body)

    status, body = action()

    record = IdempotencyKey(
        method=method,
        path=path,
        key=key,
        request_hash=request_hash,
        status_code=status,
        response_body=json.dumps(body, ensure_ascii=False, default=str),
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        # 并发下另一请求已抢先登记该 key
        existing = session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise AppError_conflict(key)
            return existing.status_code, json.loads(existing.response_body)
        raise
    return status, body


def AppError_conflict(key: str) -> errors.AppError:
    return errors.AppError(
        "IDEMPOTENCY_CONFLICT",
        f"幂等键 {key!r} 已用于不同的请求内容，异内容冲突",
        409,
    )


# ---------------------------------------------------------------- 批次操作


def register_raw_batch(session: Session, data: RawBatchIn) -> Batch:
    """登记一个原料批次。编号不得复用。"""
    existing = session.get(Batch, data.batch_no)
    if existing is not None:
        raise errors.batch_exists(data.batch_no)

    batch = Batch(
        batch_no=data.batch_no,
        unit=data.unit,
        initial_qty=data.quantity,
        remaining_qty=data.quantity,
        note=data.note,
    )
    session.add(batch)
    # 新登记批次：初始数量全部进入默认库位
    session.add(
        BatchLocation(
            batch_no=data.batch_no,
            location_code=DEFAULT_LOCATION_CODE,
            qty=data.quantity,
        )
    )
    try:
        session.flush()
    except IntegrityError:
        raise errors.batch_exists(data.batch_no) from None
    return batch


def _lock_batch(session: Session, batch_no: str) -> Batch | None:
    """读取批次并加行锁（PostgreSQL FOR UPDATE；SQLite 被写事务锁覆盖）。"""
    stmt = select(Batch).where(Batch.batch_no == batch_no)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _cancelled_output_nos(session: Session, batch_nos: list[str]) -> set[str]:
    """返回给定编号中“已撤销生产单产出”的编号集合。

    已撤销产出的批次行仍保留（编号不得复用、原单据可查），但不得再投料。
    """
    if not batch_nos:
        return set()
    rows = session.execute(
        select(Production.output_no).where(
            Production.output_no.in_(batch_nos),
            Production.is_cancelled.is_(True),
        )
    ).all()
    return {r[0] for r in rows}


def _lock_batches_in_order(
    session: Session, batch_nos: list[str]
) -> dict[str, Batch]:
    """按编号排序后逐个锁定批次。

    全局统一的加锁顺序（编号升序）保证并发生产事务之间不会出现
    “互相持有对方等待的行锁”式死锁（PostgreSQL FOR UPDATE；
    SQLite 写事务已被 BEGIN IMMEDIATE 全局串行化，FOR UPDATE 为空操作）。
    """
    ordered = sorted(set(batch_nos))
    locked: dict[str, Batch] = {}
    for no in ordered:
        stmt = select(Batch).where(Batch.batch_no == no)
        if session.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        batch = session.execute(stmt).scalar_one_or_none()
        if batch is not None:
            locked[no] = batch
    return locked


# ---------------------------------------------------------------- 库位层库存

def _lock_locations(
    session: Session, batch_no: str, location_codes: list[str]
) -> dict[str, BatchLocation]:
    """锁定某批次给定库位的余额行并按库位编号返回（PostgreSQL FOR UPDATE）。

    调用方必须已持有该批次行锁（所有事务先按批次编号升序锁批次，再锁库位，
    以保证全局统一加锁顺序）；SQLite 由 BEGIN IMMEDIATE 全库写锁串行，
    FOR UPDATE 为空操作。
    """
    codes = sorted(set(location_codes))
    if not codes:
        return {}
    stmt = select(BatchLocation).where(
        BatchLocation.batch_no == batch_no,
        BatchLocation.location_code.in_(codes),
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    rows = session.execute(stmt).scalars().all()
    return {r.location_code: r for r in rows}


def _ensure_location_rows(
    session: Session, batch_no: str, location_codes: list[str]
) -> None:
    """确保某批次的指定库位行存在（余额 0），幂等。

    调拨可能首次把数量移入一个此前没有余额的库位。并发下两个事务可能同时
    尝试 INSERT 同一目标库位行：用 ON CONFLICT DO NOTHING 让冲突方插入
    空转，随后统一 SELECT ... FOR UPDATE 即可串行；PG/SQLite 各用其方言
    语句，避免“插入失败导致整事务回滚再重试”的写法。
    """
    codes = sorted(set(c for c in location_codes if c != DEFAULT_LOCATION_CODE))
    if not codes:
        return
    dialect = session.get_bind().dialect.name
    rows = [
        {"batch_no": batch_no, "location_code": code, "qty": Decimal("0")}
        for code in codes
    ]
    if dialect == "postgresql":
        stmt = pg_insert(BatchLocation).values(rows).on_conflict_do_nothing(
            index_elements=["batch_no", "location_code"]
        )
    else:
        stmt = sqlite_insert(BatchLocation).values(rows).on_conflict_do_nothing(
            index_elements=["batch_no", "location_code"]
        )
    session.execute(stmt)


def _allocate_consumption(
    session: Session, batch_no: str, quantity: Decimal, location_code: str | None
) -> tuple[dict[str, Decimal], dict[str, BatchLocation]]:
    """为一笔投料/分装扣减规划库位分配方案（不做任何修改）。

    - 指定库位：只允许扣该库位；库位不存在报 LOCATION_NOT_FOUND(404)，
      该库位余额不足报 INSUFFICIENT_BALANCE(409)（details 带库位编号）；
    - 未指定库位：按库位编号升序跨库位依次扣减；全局余量不足时按既有
      INSUFFICIENT_BALANCE(409) 拒绝。

    返回 (分配方案, 已锁定库位行)；方案仅含扣减量为正的库位，调用方据此改
    库位行。调用方必须已持有该批次行锁，故此处读到的库位行集合在事务内
    不会被并发调拨改变（新库位只能由同批次的调拨事务创建，而它同样先锁批次）。
    """
    if location_code is not None:
        locked = _lock_locations(session, batch_no, [location_code])
        loc = locked.get(location_code)
        if loc is None:
            raise errors.location_not_found(batch_no, location_code)
        if quantity > loc.qty:
            raise errors.insufficient(
                batch_no, quantity, loc.qty, location_code=location_code
            )
        return {location_code: quantity}, locked

    # 调用方已持批次行锁：一次性对该批次全部库位行加锁并据此分配，
    # 读与锁在同一语句完成（PG FOR UPDATE；SQLite 为空操作但有全库写锁）。
    stmt = select(BatchLocation).where(BatchLocation.batch_no == batch_no)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    existing = session.execute(stmt).scalars().all()
    by_code = {r.location_code: r for r in existing}
    total = sum((r.qty for r in existing), Decimal("0"))
    if quantity > total:
        raise errors.insufficient(batch_no, quantity, total)

    codes = sorted(by_code)
    remaining = quantity
    plan: dict[str, Decimal] = {}
    for code in codes:
        available = by_code[code].qty
        if available <= 0 or remaining <= 0:
            continue
        take = min(available, remaining)
        plan[code] = take
        remaining -= take
    # 库位行之和 == 全局余量（不变式），上面已比较过 quantity <= 总量，
    # 因此剩余必然被分配完。
    return plan, by_code


def _apply_location_plan(
    location_rows: dict[str, BatchLocation], plan: dict[str, Decimal]
) -> None:
    """按分配方案扣减已锁定的库位行。"""
    for code, take in plan.items():
        location_rows[code].qty -= take


def _create_batch_with_default_location(
    session: Session, batch_no: str, unit: str, qty: Decimal
) -> Batch:
    """创建批次并在默认库位写入等量余额（生产产出 / 分装子批次）。"""
    batch = Batch(batch_no=batch_no, unit=unit, initial_qty=qty, remaining_qty=qty)
    session.add(batch)
    session.add(
        BatchLocation(batch_no=batch_no, location_code=DEFAULT_LOCATION_CODE, qty=qty)
    )
    return batch


def get_batch_locations(session: Session, batch_no: str) -> dict[str, Any]:
    """查询批次逐库位余额（按库位编号排序）及全局余量；两者恒等。"""
    batch = session.get(Batch, batch_no)
    if batch is None:
        raise errors.batch_not_found(batch_no)
    rows = session.execute(
        select(BatchLocation)
        .where(BatchLocation.batch_no == batch_no)
        .order_by(BatchLocation.location_code)
    ).scalars().all()
    return {
        "batch_no": batch_no,
        "unit": batch.unit,
        "remaining_qty": float(batch.remaining_qty),
        "locations": [
            {"location_code": r.location_code, "quantity": float(r.qty)} for r in rows
        ],
    }


def _acquire_qc_lock(session: Session) -> None:
    """污染标记/复核等 QC 写事务的串行化锁。

    PostgreSQL：对 batches 取 SHARE ROW EXCLUSIVE 表锁。它与生产事务的
    ROW SHARE（SELECT ... FOR UPDATE）/ ROW EXCLUSIVE（写入）冲突，也与
    其它 QC 事务互斥——使“标记/复核”与“投料”严格串行，既杜绝隔离批次被
    投料，也避免“先锁 root、再锁后代”与生产的排序行锁之间形成死锁环。
    QC 操作低频，粗粒度表锁代价可接受。
    SQLite：BEGIN IMMEDIATE 已提供全库写串行化，无需额外处理。
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE batches IN SHARE ROW EXCLUSIVE MODE"))


def submit_production(session: Session, data: ProductionIn) -> Production:
    """提交一次生产：校验全部通过后才扣减余量并创建产出批次。

    校验顺序（任一失败整笔拒绝、不改变余量）：
    1. 投入明细编号不重复；
    2. 每个来源批次存在、单位一致、非已撤销产出、未隔离；
    3. 产出编号未被占用；
    4. 每笔用量为正：指定库位时该库位须存在且充足，未指定时全局余量充足
       （库位层按库位编号顺序跨库位扣减）；
    5. 产出总量不超过投入总量。

    库位层：每笔投料的实际扣减按库位写入 ProductionInputLocation，供生产
    撤销时逐库位返还；产出批次进入默认库位。加锁顺序仍为“先按批次编号升序
    锁批次，再按 (批次, 库位) 锁库位”，与调拨/分装共用同一全局顺序。
    """
    source_nos = [item.batch_no for item in data.inputs]
    if len(set(source_nos)) != len(source_nos):
        raise errors.AppError(
            "DUPLICATE_INPUT",
            "同一生产单中来源批次不得重复",
            422,
        )

    sources: dict[str, Batch] = {}
    total_input = Decimal("0")
    # 先按编号排序一次性锁定所有来源批次，避免并发事务交叉加锁死锁
    # （PostgreSQL 行锁；SQLite 为立即写锁）
    locked = _lock_batches_in_order(session, source_nos)
    for no in source_nos:
        batch = locked.get(no)
        if batch is None:
            raise errors.batch_not_found(no)
        sources[no] = batch

    cancelled_nos = _cancelled_output_nos(session, source_nos)

    for item in data.inputs:
        batch = sources[item.batch_no]
        if batch.unit != data.unit:
            raise errors.unit_mismatch(item.batch_no, batch.unit, data.unit)
        if item.batch_no in cancelled_nos:
            raise errors.batch_cancelled(item.batch_no)
        if batch.is_quarantined:
            raise errors.batch_quarantined(item.batch_no)

    if session.get(Batch, data.output_batch_no) is not None:
        raise errors.batch_exists(data.output_batch_no)

    # 逐笔规划库位扣减（内部加库位行锁）。先统一规划、全部通过后才修改，
    # 保证任一来源库位不足时整笔拒绝、不触碰任何库存。
    plans: dict[int, dict[str, Decimal]] = {}
    locked_locations: dict[int, dict[str, BatchLocation]] = {}
    for item in data.inputs:
        batch = sources[item.batch_no]
        if item.location_code is None and item.quantity > batch.remaining_qty:
            raise errors.insufficient(
                item.batch_no, item.quantity, batch.remaining_qty
            )
        plan, loc_rows = _allocate_consumption(
            session, item.batch_no, item.quantity, item.location_code
        )
        plans[id(item)] = plan
        locked_locations[id(item)] = loc_rows
        total_input += item.quantity

    if data.output_quantity > total_input:
        raise errors.AppError(
            "OUTPUT_EXCEEDS_INPUT",
            f"产出量 {normalize_decimal(data.output_quantity)} 超过投入总量 "
            f"{normalize_decimal(total_input)}",
            422,
            details={
                "output_quantity": normalize_decimal(data.output_quantity),
                "total_input": normalize_decimal(total_input),
            },
        )

    output = _create_batch_with_default_location(
        session, data.output_batch_no, data.unit, data.output_quantity
    )
    try:
        session.flush()  # 让产出批次主键可用，并尽早暴露编号冲突
    except IntegrityError:
        raise errors.batch_exists(data.output_batch_no) from None

    production = Production(
        output_no=output.batch_no, output_qty=data.output_quantity
    )
    session.add(production)
    session.flush()

    for item in data.inputs:
        batch = sources[item.batch_no]
        batch.remaining_qty -= item.quantity
        plan = plans[id(item)]
        _apply_location_plan(locked_locations[id(item)], plan)
        production_input = ProductionInput(
            production_id=production.id,
            source_no=batch.batch_no,
            qty=item.quantity,
            location_code=item.location_code,
        )
        session.add(production_input)
        session.flush()  # 取得 production_input.id
        for code in sorted(plan):
            session.add(
                ProductionInputLocation(
                    production_input_id=production_input.id,
                    location_code=code,
                    qty=plan[code],
                )
            )

    session.flush()
    # 重新加载聚合，便于响应组装
    return session.execute(
        select(Production)
        .options(selectinload(Production.inputs))
        .where(Production.id == production.id)
    ).scalar_one()


# ---------------------------------------------------------------- 批次分装


def split_batch(session: Session, parent_no: str, data: SplitIn) -> dict[str, Any]:
    """把同一批次分装为多个可独立投料的子批次。

    一笔分装包含多个互异的新编号及各自正数分装量。全部校验通过后才在同一
    事务内扣减父批次余量、创建子批次（继承单位，初始/当前余量等于分装量）
    并写分装关系；分装不产生生产单，任一不符整笔拒绝、不改库存。

    校验顺序：
    1. 同一笔分装中新编号互不相同；
    2. 父批次存在（SOURCE_NOT_FOUND 404）、未隔离（BATCH_QUARANTINED 409）、
       非已撤销产出（BATCH_CANCELLED 409）；
    3. 每个新编号未被占用（BATCH_NO_REUSED 409，编号全局不得复用）；
    4. 分装总量为正（入参已逐项保证 > 0）：指定库位时该库位须存在且充足，
       未指定时按库位编号顺序跨库位扣减、不得超过父批次全局余量
       （INSUFFICIENT_BALANCE 409）。

    库位层：父批次按与投料一致的规则扣减库位余额；分装产生的子批次一律
    进入默认库位（initial/当前余量均为分装量）。

    并发：与生产提交相同，按编号升序持锁（此处只锁父批次），再锁其库位行，
    子批次行由唯一约束兜底并发抢号；SQLite 由 BEGIN IMMEDIATE 全库串行。
    分装 / 投料 / 调拨并发不会超用或使两层余额失配；与污染标记/复核的关系
    同生产事务（经行锁与 QC 表锁互斥），隔离沿分装边与投料边一并传播，
    不会漏隔离。
    """
    child_nos = [item.batch_no for item in data.splits]
    if len(set(child_nos)) != len(child_nos):
        dup = next(no for no in child_nos if child_nos.count(no) > 1)
        raise errors.duplicate_split_target(dup)

    parent = _lock_batch(session, parent_no)
    if parent is None:
        raise errors.batch_not_found(parent_no)
    if _is_cancelled_output(session, parent_no):
        raise errors.batch_cancelled(parent_no)
    if parent.is_quarantined:
        raise errors.batch_quarantined(parent_no)

    # 新编号不得复用：编号空间全局唯一（原料/产出/子批次共用）
    existing_nos = {
        r[0]
        for r in session.execute(
            select(Batch.batch_no).where(Batch.batch_no.in_(child_nos))
        ).all()
    }
    if existing_nos:
        raise errors.batch_exists(sorted(existing_nos)[0])

    total = Decimal("0")
    for item in data.splits:
        total += item.quantity
    if data.location_code is None and total > parent.remaining_qty:
        raise errors.insufficient(parent_no, total, parent.remaining_qty)

    # 先规划库位扣减（内部加库位行锁），库位不足整笔拒绝、不动任何库存
    plan, locked_locations = _allocate_consumption(
        session, parent_no, total, data.location_code
    )

    created: list[Batch] = []
    relations: list[BatchSplit] = []
    for item in data.splits:
        child = _create_batch_with_default_location(
            session, item.batch_no, parent.unit, item.quantity
        )
        try:
            session.flush()  # 尽早暴露并发下的编号冲突
        except IntegrityError:
            raise errors.batch_exists(item.batch_no) from None
        created.append(child)
        relations.append(
            BatchSplit(parent_no=parent_no, child_no=item.batch_no, qty=item.quantity)
        )

    parent.remaining_qty -= total
    _apply_location_plan(locked_locations, plan)
    session.add_all(relations)
    session.flush()

    return {
        "parent_batch_no": parent_no,
        "parent_remaining_qty": float(parent.remaining_qty),
        "splits": [
            {
                "batch_no": child.batch_no,
                "unit": child.unit,
                "quantity": float(child.initial_qty),
                "remaining_qty": float(child.remaining_qty),
                "is_suspect": child.is_suspect,
                "is_quarantined": child.is_quarantined,
            }
            for child in created
        ],
        "relations": [
            {
                "parent_batch_no": parent_no,
                "child_batch_no": child.batch_no,
                "quantity": float(child.initial_qty),
            }
            for child in created
        ],
    }


# ---------------------------------------------------------------- 库位调拨


def transfer_stock(session: Session, data: TransferIn) -> dict[str, Any]:
    """同一批次在两个库位之间调拨正数数量。

    校验顺序（任一失败整笔拒绝、两库位余额均不变）：
    1. 来源库位与目标库位不同（库位编号非空白由入参校验保证；
       相同 -> TRANSFER_SAME_LOCATION 422）；
    2. 调拨单号未被占用（TRANSFER_NO_REUSED 409，单号全局不得复用）；
    3. 批次存在（SOURCE_NOT_FOUND 404）；
    4. 来源库位存在（LOCATION_NOT_FOUND 404）且余额充足
       （INSUFFICIENT_BALANCE 409，details 带库位编号）。

    隔离批次（含疑似）与已撤销产出批次均**允许**调拨——隔离只禁止投料与
    分装。调拨不改变批次全局余量，只在库位层把数量从来源移到目标；目标库位
    首次使用时自动以零余额建行。

    并发：先锁批次行（与投料/分装同一按编号升序的顺序），再按库位编号升序
    锁来源/目标两行（目标行先 ON CONFLICT DO NOTHING 确保存在）。因此
    调拨 / 投料 / 分装 / 撤销之间不会死锁，也不会超用或使两层余额失配；
    SQLite 由 BEGIN IMMEDIATE 全库串行。
    """
    if data.source_location == data.target_location:
        raise errors.transfer_same_location(data.source_location)

    existing_transfer = session.execute(
        select(StockTransfer.id).where(StockTransfer.transfer_no == data.transfer_no)
    ).scalar_one_or_none()
    if existing_transfer is not None:
        raise errors.transfer_no_exists(data.transfer_no)

    batch = _lock_batch(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)

    # 先锁来源库位并完成“存在且充足”的全部校验，任何不符都在目标库位建行
    # 之前拒绝（整单不留任何痕迹；HTTP 事务本也会回滚，这里双保险）。
    source_rows = _lock_locations(session, data.batch_no, [data.source_location])
    source = source_rows.get(data.source_location)
    if source is None:
        raise errors.location_not_found(data.batch_no, data.source_location)
    if data.quantity > source.qty:
        raise errors.insufficient(
            data.batch_no,
            data.quantity,
            source.qty,
            location_code=data.source_location,
        )

    # 目标库位可能首次使用：幂等确保零余额行存在后再加锁。同批次的写事务
    # 都先持有批次行锁，故此处与其它事务不可能并发建行（ON CONFLICT 双保险）。
    _ensure_location_rows(session, data.batch_no, [data.target_location])
    target_rows = _lock_locations(session, data.batch_no, [data.target_location])
    target = target_rows[data.target_location]

    source.qty -= data.quantity
    target.qty += data.quantity

    transfer = StockTransfer(
        transfer_no=data.transfer_no,
        batch_no=data.batch_no,
        source_location=data.source_location,
        target_location=data.target_location,
        qty=data.quantity,
    )
    session.add(transfer)
    try:
        session.flush()
    except IntegrityError:
        raise errors.transfer_no_exists(data.transfer_no) from None

    final_locations = session.execute(
        select(BatchLocation)
        .where(BatchLocation.batch_no == data.batch_no)
        .order_by(BatchLocation.location_code)
    ).scalars().all()
    return {
        "transfer_no": data.transfer_no,
        "batch_no": data.batch_no,
        "source_location": data.source_location,
        "target_location": data.target_location,
        "quantity": float(data.quantity),
        "source_balance": float(source.qty),
        "target_balance": float(target.qty),
        "remaining_qty": float(batch.remaining_qty),
        "locations": [
            {"location_code": r.location_code, "quantity": float(r.qty)}
            for r in final_locations
        ],
    }


# ---------------------------------------------------------------- 仓库盘点


def _stock_check_out(check: StockCheck) -> dict[str, Any]:
    """组装盘点单响应：状态、提交时间及逐行原值/差额/单位（按行 id 即提交顺序）。"""
    checked_at = check.created_at
    if checked_at is not None and getattr(checked_at, "tzinfo", None) is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return {
        "stock_check_no": check.stock_check_no,
        "status": check.status,
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
        "lines": [
            {
                "batch_no": line.batch_no,
                "location_code": line.location_code,
                "book_qty": float(line.book_qty),
                "actual_qty": float(line.actual_qty),
                "loss_qty": float(line.loss_qty),
                "unit": line.unit,
            }
            for line in sorted(check.lines, key=lambda x: x.id)
        ],
    }


def submit_stock_check(session: Session, data: StockCheckIn) -> dict[str, Any]:
    """凭唯一盘点单号提交多行盘点：核对账面后按差额扣减库位与全局余量。

    规则（任一不符整单拒绝、不留盘点记录、不改任何库存）：
    1. 各行（批次, 库位）互异（DUPLICATE_STOCK_CHECK_LINE 422，由入参与
       本层双重保证）；账面量/实点量非负、实点量不得高于账面量（入参 422）；
       整单至少一条盘亏（STOCK_CHECK_NO_LOSS 422）；
    2. 盘点单号未被占用（STOCK_CHECK_NO_REUSED 409，单号全局不得复用）；
    3. 每个批次存在（SOURCE_NOT_FOUND 404）；对应库位行存在
       （LOCATION_NOT_FOUND 404）；逐行核对当前库位实存恰等于申报账面量，
       不符即 STOCK_CHECK_BOOK_CHANGED(409)，整单拒绝。

    隔离批次（含疑似）与已撤销产出仍可核销——盘点是库存校正，不是投料/分装。
    全部通过后在同一事务内：按差额扣减各库位行与批次全局余量
    （既有 remaining_qty 字段仍表示实存，各库位之和仍等于全局余量），
    并写入只追加的盘点单与逐行原值/差额/单位/时间快照。

    并发：沿用全局统一加锁顺序——先按批次编号升序锁批次行，再按
    （批次, 库位编号）锁库位行（PG FOR UPDATE；SQLite BEGIN IMMEDIATE
    全库串行）。与投料/分装/调拨/撤销交错时，后到者要么在锁后读到新余额
    （账面已变动 -> 整单拒绝），要么排在前序事务之后按新账面核对，绝不超扣、
    绝不覆盖新余额，两层恒等式恒成立。
    """
    # 同单内（批次, 库位）互异（入参 model_validator 之外的服务层兜底）
    pairs = [(line.batch_no, line.location_code) for line in data.lines]
    if len(set(pairs)) != len(pairs):
        seen: set[tuple[str, str]] = set()
        for batch_no, location_code in pairs:
            if (batch_no, location_code) in seen:
                raise errors.duplicate_stock_check_line(batch_no, location_code)
            seen.add((batch_no, location_code))

    if not any(line.actual_qty < line.book_qty for line in data.lines):
        raise errors.stock_check_no_loss()

    # 单号不得复用（唯一约束在提交时再兜底一次）
    existing_no = session.execute(
        select(StockCheck.id).where(StockCheck.stock_check_no == data.stock_check_no)
    ).scalar_one_or_none()
    if existing_no is not None:
        raise errors.stock_check_no_exists(data.stock_check_no)

    # 先按批次编号升序锁定全部涉及批次，再在每个批次内按库位编号升序锁库位行。
    batch_nos = [batch_no for batch_no, _ in pairs]
    locked_batches = _lock_batches_in_order(session, batch_nos)

    # 逐行核对：批次存在、库位存在、当前实存恰等于申报账面量。
    # 全部核对通过后才允许任何写入，确保“账面已变动”时整单不留痕迹。
    location_rows: dict[tuple[str, str], BatchLocation] = {}
    by_batch: dict[str, list[StockCheckLineIn]] = {}
    for line in data.lines:
        by_batch.setdefault(line.batch_no, []).append(line)
    for batch_no in sorted(by_batch):
        batch = locked_batches.get(batch_no)
        if batch is None:
            raise errors.batch_not_found(batch_no)
        codes = [line.location_code for line in by_batch[batch_no]]
        rows = _lock_locations(session, batch_no, codes)
        for line in by_batch[batch_no]:
            loc_row = rows.get(line.location_code)
            if loc_row is None:
                raise errors.location_not_found(batch_no, line.location_code)
            if loc_row.qty != line.book_qty:
                raise errors.stock_check_book_changed(
                    batch_no, line.location_code, line.book_qty, loc_row.qty
                )
            location_rows[(batch_no, line.location_code)] = loc_row

    # 核对全部通过：先建盘点单头（尽早暴露并发抢号），再扣减并写行快照
    check = StockCheck(stock_check_no=data.stock_check_no, status="POSTED")
    session.add(check)
    try:
        session.flush()
    except IntegrityError:
        raise errors.stock_check_no_exists(data.stock_check_no) from None

    for line in data.lines:
        batch = locked_batches[line.batch_no]
        loss = line.book_qty - line.actual_qty
        if loss > 0:
            # 库位行已核对 == book_qty >= actual_qty，扣减后必不为负；
            # 全局余量 = 各库位之和，故同步扣减不会打破批次内不变式。
            location_rows[(line.batch_no, line.location_code)].qty -= loss
            batch.remaining_qty -= loss
        session.add(
            StockCheckLine(
                stock_check_id=check.id,
                batch_no=line.batch_no,
                location_code=line.location_code,
                book_qty=line.book_qty,
                actual_qty=line.actual_qty,
                loss_qty=loss,
                unit=batch.unit,
            )
        )

    session.flush()
    return _stock_check_out(check)


def get_stock_check(session: Session, stock_check_no: str) -> dict[str, Any]:
    """按盘点单号查询：返回状态及逐行原值、差额与单位。

    单号不存在（失败提交不留记录）-> STOCK_CHECK_NOT_FOUND(404)。
    """
    check = session.execute(
        select(StockCheck)
        .options(selectinload(StockCheck.lines))
        .where(StockCheck.stock_check_no == stock_check_no)
    ).scalar_one_or_none()
    if check is None:
        raise errors.stock_check_not_found(stock_check_no)
    return _stock_check_out(check)


# ---------------------------------------------------------------- 客户发货


def _iso_utc(dt: object) -> str:
    """naive（SQLite）时间统一当作 UTC 输出 ISO 8601。"""
    if dt is not None and getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)  # type: ignore[union-attr]
    return dt.isoformat() if dt is not None else None  # type: ignore[union-attr]


def _line_return_status(shipped: Decimal, returned: Decimal) -> str:
    if returned >= shipped:
        return "FULLY_RETURNED"
    if returned > 0:
        return "PARTIALLY_RETURNED"
    return "SHIPPED"


def _returned_qty_by_line(
    session: Session, shipment_line_ids: list[int]
) -> dict[int, Decimal]:
    """各发货行截至当前的累计退回量（退货行只追加，SUM 聚合）。"""
    if not shipment_line_ids:
        return {}
    rows = session.execute(
        select(ReturnOrderLine.shipment_line_id, func.coalesce(func.sum(ReturnOrderLine.qty), 0))
        .where(ReturnOrderLine.shipment_line_id.in_(shipment_line_ids))
        .group_by(ReturnOrderLine.shipment_line_id)
    ).all()
    return {line_id: Decimal(qty) for line_id, qty in rows}


def _shipment_view(session: Session, shipment: Shipment) -> dict[str, Any]:
    """组装发货单查询响应：整单状态、逐行发出/累计退回/可退量与逐次退货记录。"""
    lines = sorted(shipment.lines, key=lambda x: x.line_no)
    returned_map = _returned_qty_by_line(session, [line.id for line in lines])

    line_views: list[dict[str, Any]] = []
    any_return = False
    all_full = True
    for line in lines:
        shipped = Decimal(line.qty)
        returned = returned_map.get(line.id, Decimal("0"))
        line_status = _line_return_status(shipped, returned)
        if returned > 0:
            any_return = True
        if line_status != "FULLY_RETURNED":
            all_full = False
        line_views.append(
            {
                "line_no": line.line_no,
                "batch_no": line.batch_no,
                "location_code": line.location_code,
                "unit": line.unit,
                "shipped_qty": float(shipped),
                "returned_qty": float(returned),
                "returnable_qty": float(shipped - returned),
                "status": line_status,
            }
        )

    # 全部行全退 -> FULLY_RETURNED；有任一行退回（但未全退）-> PARTIALLY_RETURNED
    if all_full:
        overall = "FULLY_RETURNED"
    elif any_return:
        overall = "PARTIALLY_RETURNED"
    else:
        overall = "SHIPPED"

    returns = session.execute(
        select(ReturnOrder)
        .options(selectinload(ReturnOrder.lines).selectinload(ReturnOrderLine.shipment_line))
        .where(ReturnOrder.shipment_no == shipment.shipment_no)
        .order_by(ReturnOrder.created_at, ReturnOrder.id)
    ).scalars().all()
    return_views: list[dict[str, Any]] = []
    for ret in returns:
        ret_lines = sorted(ret.lines, key=lambda x: x.shipment_line.line_no)
        return_views.append(
            {
                "return_no": ret.return_no,
                "shipment_no": ret.shipment_no,
                "returned_at": _iso_utc(ret.created_at),
                "lines": [
                    {
                        "line_no": rl.shipment_line.line_no,
                        "batch_no": rl.shipment_line.batch_no,
                        "location_code": rl.shipment_line.location_code,
                        "unit": rl.shipment_line.unit,
                        "quantity": float(rl.qty),
                    }
                    for rl in ret_lines
                ],
            }
        )

    return {
        "shipment_no": shipment.shipment_no,
        "status": overall,
        "shipped_at": _iso_utc(shipment.created_at),
        "lines": line_views,
        "returns": return_views,
    }


def _quarantined_shipment_error(batch_no: str) -> errors.AppError:
    """发货场景的隔离拒绝：错误码沿用 BATCH_QUARANTINED，消息贴合发货。"""
    return errors.AppError(
        "BATCH_QUARANTINED",
        f"批次 {batch_no!r} 已隔离，隔离或已撤销产出不可发货",
        409,
        details={"batch_no": batch_no},
    )


def submit_shipment(session: Session, data: ShipmentIn) -> dict[str, Any]:
    """客户发货：整单确认时原子扣减各库位与批次全局余量。

    校验顺序（任一不符整单拒绝、无任何部分变更）：
    1. 同单内（批次, 库位）互异（DUPLICATE_SHIPMENT_LINE 422）；
    2. 发货单号未被占用（SHIPMENT_NO_REUSED 409，单号全局不得复用）；
    3. 每个批次存在（SOURCE_NOT_FOUND 404）；
    4. 批次非已撤销产出（BATCH_CANCELLED 409）且未隔离
       （BATCH_QUARANTINED 409，含疑似）——隔离或已撤销产出不可发货；
    5. 逐行：发货库位存在（LOCATION_NOT_FOUND 404）且该库位余额不少于
       发货量（INSUFFICIENT_BALANCE 409，details 带库位编号）；不做跨库位
       分配，库位不足即使全局余量充足也整单拒绝。

    全部通过后同事务：逐行扣减库位行与批次全局余量（各库位之和恒等于
    全局余量），写入只追加的发货单头与逐行快照（单位取批次单位）。

    并发：沿用全局统一两级加锁顺序——先按批次编号升序锁批次行，再在每个
    批次内按库位编号升序锁库位行（PG FOR UPDATE；SQLite 为
    BEGIN IMMEDIATE 全库串行）。发货与投料、分装、调拨、盘点、退货、QC
    任意并发都不会超扣，两层恒等式恒成立。
    """
    pairs = [(line.batch_no, line.location_code) for line in data.lines]
    if len(set(pairs)) != len(pairs):
        seen: set[tuple[str, str]] = set()
        for batch_no, location_code in pairs:
            if (batch_no, location_code) in seen:
                raise errors.duplicate_shipment_line(batch_no, location_code)
            seen.add((batch_no, location_code))

    existing_shipment = session.execute(
        select(Shipment.id).where(Shipment.shipment_no == data.shipment_no)
    ).scalar_one_or_none()
    if existing_shipment is not None:
        raise errors.shipment_no_exists(data.shipment_no)

    batch_nos = [batch_no for batch_no, _ in pairs]
    locked_batches = _lock_batches_in_order(session, batch_nos)

    # 逐批做存在性 / 可发货状态校验
    cancelled_nos = _cancelled_output_nos(session, batch_nos)
    by_batch: dict[str, list[ShipmentLineIn]] = {}
    for line in data.lines:
        batch = locked_batches.get(line.batch_no)
        if batch is None:
            raise errors.batch_not_found(line.batch_no)
        if line.batch_no in cancelled_nos:
            raise errors.batch_cancelled(line.batch_no)
        if batch.is_quarantined:
            raise _quarantined_shipment_error(line.batch_no)
        by_batch.setdefault(line.batch_no, []).append(line)

    # 先统一锁定/校验全部库位行并检查余额，全部通过后才允许任何写入。
    location_rows: dict[tuple[str, str], BatchLocation] = {}
    for batch_no in sorted(by_batch):
        codes = [line.location_code for line in by_batch[batch_no]]
        rows = _lock_locations(session, batch_no, codes)
        for line in by_batch[batch_no]:
            loc_row = rows.get(line.location_code)
            if loc_row is None:
                raise errors.location_not_found(batch_no, line.location_code)
            if line.quantity > loc_row.qty:
                raise errors.insufficient(
                    batch_no, line.quantity, loc_row.qty,
                    location_code=line.location_code,
                )
            location_rows[(batch_no, line.location_code)] = loc_row

    # 建单头（尽早暴露并发抢号），唯一约束再兜底一次
    shipment = Shipment(shipment_no=data.shipment_no, status="SHIPPED")
    session.add(shipment)
    try:
        session.flush()
    except IntegrityError:
        raise errors.shipment_no_exists(data.shipment_no) from None

    for line_no, line in enumerate(data.lines, start=1):
        batch = locked_batches[line.batch_no]
        location_rows[(line.batch_no, line.location_code)].qty -= line.quantity
        batch.remaining_qty -= line.quantity
        session.add(
            ShipmentLine(
                shipment_id=shipment.id,
                line_no=line_no,
                batch_no=line.batch_no,
                location_code=line.location_code,
                qty=line.quantity,
                unit=batch.unit,
            )
        )

    session.flush()
    return _shipment_view(session, shipment)


def get_shipment(session: Session, shipment_no: str) -> dict[str, Any]:
    """按发货单号查询：整单状态、逐行发出/累计退回/可退量与逐次退货记录。

    单号不存在（失败提交不留记录）-> SHIPMENT_NOT_FOUND(404)。
    """
    shipment = session.execute(
        select(Shipment)
        .options(selectinload(Shipment.lines))
        .where(Shipment.shipment_no == shipment_no)
    ).scalar_one_or_none()
    if shipment is None:
        raise errors.shipment_not_found(shipment_no)
    return _shipment_view(session, shipment)


# ---------------------------------------------------------------- 客户退货


def submit_return(session: Session, data: ReturnIn) -> dict[str, Any]:
    """客户退货：凭原发货单号 + 唯一退货单号，对若干原行原子退回正数数量。

    规则（任一不符整单拒绝、无任何部分变更）：
    1. 同单内原行号互异（DUPLICATE_RETURN_LINE 422）；
    2. 退货单号未被占用（RETURN_NO_REUSED 409，单号全局不得复用）；
    3. 原发货单存在（SHIPMENT_NOT_FOUND 404）；先锁发货单头，使同一发货单
       的并发退货全局串行，累计退回绝不可能超过原行发出量；
    4. 每个原行号在该发货单内存在（SHIPMENT_LINE_NOT_FOUND 404）；
    5. 逐行累计退回（含本次）不超过原行发出量
       （RETURN_EXCEEDS_SHIPPED 409，details 带已退/可退量）。

    返还严格回原批次、原库位：库位行不存在（数据异常）时幂等补零行；
    返还后批次余量可能高于当前量但不超过 initial_qty（退货量来自此前的
    发货扣减）。发货后才隔离的批次仍可退货——退货不检查隔离/撤销状态，
    返还到原批次后该批次继续隔离。

    并发：先锁原发货单头串行化同单退货，再按批次编号升序、库位编号升序
    锁库存，与发货/投料/分装/调拨/盘点共用同一全局加锁顺序；任意并发
    组合都不会超退、超扣或使两层余额失配（PG FOR UPDATE；SQLite 为
    BEGIN IMMEDIATE 全库串行）。
    """
    line_nos = [line.line_no for line in data.lines]
    if len(set(line_nos)) != len(line_nos):
        dup = next(no for no in line_nos if line_nos.count(no) > 1)
        raise errors.duplicate_return_line(dup)

    existing_return = session.execute(
        select(ReturnOrder.id).where(ReturnOrder.return_no == data.return_no)
    ).scalar_one_or_none()
    if existing_return is not None:
        raise errors.return_no_exists(data.return_no)

    # 锁发货单头：同一张发货单上的退货严格串行（PG 行锁；SQLite 全库写锁）。
    stmt = select(Shipment).where(Shipment.shipment_no == data.shipment_no)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    shipment = session.execute(
        stmt.options(selectinload(Shipment.lines))
    ).scalar_one_or_none()
    if shipment is None:
        raise errors.shipment_not_found(data.shipment_no)

    lines_by_no = {line.line_no: line for line in shipment.lines}
    targets: list[tuple[Any, Any]] = []
    requested_by_line: dict[int, Decimal] = {}
    for line in data.lines:
        shipment_line = lines_by_no.get(line.line_no)
        if shipment_line is None:
            raise errors.shipment_line_not_found(data.shipment_no, line.line_no)
        targets.append((line, shipment_line))
        requested_by_line[line.line_no] = line.quantity

    # 锁原发货行（同一发货单的退货已被单头锁串行，FOR UPDATE 为一致性双保险）
    line_ids = [sl.id for _, sl in targets]
    if session.get_bind().dialect.name == "postgresql" and line_ids:
        session.execute(
            select(ShipmentLine).where(ShipmentLine.id.in_(line_ids)).with_for_update()
        ).scalars().all()

    returned_map = _returned_qty_by_line(session, line_ids)
    # 超退校验：本单内同原行不重复，故无需累计本次请求
    for line, shipment_line in targets:
        shipped = Decimal(shipment_line.qty)
        already = returned_map.get(shipment_line.id, Decimal("0"))
        if already + line.quantity > shipped:
            raise errors.return_exceeds_shipped(
                data.shipment_no, line.line_no, line.quantity, shipped, already
            )

    # 按全局统一顺序锁原批次、原库位（批次编号升序 -> 库位编号升序）。
    # 发货后才隔离的批次仍可退货：退货不判定 is_quarantined / is_cancelled。
    batch_nos = sorted({sl.batch_no for _, sl in targets})
    locked_batches = _lock_batches_in_order(session, batch_nos)
    needed_locations: dict[str, set[str]] = {}
    for _, sl in targets:
        needed_locations.setdefault(sl.batch_no, set()).add(sl.location_code)
    locked_locations: dict[str, dict[str, BatchLocation]] = {}
    for batch_no in sorted(needed_locations):
        codes = sorted(needed_locations[batch_no])
        # 防御：原库位行必然存在（发货时即扣减）；异常缺失时补零行再锁。
        _ensure_location_rows(session, batch_no, codes)
        locked_locations[batch_no] = _lock_locations(session, batch_no, codes)

    return_order = ReturnOrder(
        return_no=data.return_no, shipment_no=data.shipment_no
    )
    session.add(return_order)
    try:
        session.flush()  # 尽早暴露并发抢号
    except IntegrityError:
        raise errors.return_no_exists(data.return_no) from None

    for line, shipment_line in targets:
        locked_batches[shipment_line.batch_no].remaining_qty += line.quantity
        locked_locations[shipment_line.batch_no][shipment_line.location_code].qty += (
            line.quantity
        )
        session.add(
            ReturnOrderLine(
                return_id=return_order.id,
                shipment_line_id=shipment_line.id,
                qty=line.quantity,
            )
        )

    session.flush()

    # 重算并固化发货单头状态
    view = _shipment_view(session, shipment)
    shipment.status = view["status"]
    session.flush()

    new_returned = {
        sl.id: returned_map.get(sl.id, Decimal("0")) + requested_by_line[sl.line_no]
        for _, sl in targets
    }
    result_lines = []
    for _, sl in sorted(targets, key=lambda t: t[1].line_no):
        shipped = Decimal(sl.qty)
        returned_total = new_returned[sl.id]
        result_lines.append(
            {
                "line_no": sl.line_no,
                "batch_no": sl.batch_no,
                "location_code": sl.location_code,
                "unit": sl.unit,
                "quantity": float(requested_by_line[sl.line_no]),
                "returned_qty": float(returned_total),
                "returnable_qty": float(shipped - returned_total),
                "status": _line_return_status(shipped, returned_total),
            }
        )

    return {
        "return_no": data.return_no,
        "shipment_no": data.shipment_no,
        "shipment_status": view["status"],
        "returned_at": _iso_utc(return_order.created_at),
        "lines": result_lines,
    }


# ---------------------------------------------------------------- 生产单撤销


def _is_cancelled_output(session: Session, batch_no: str) -> bool:
    """该批次是否为一张已撤销生产单的产出（原料批次恒为 False）。"""
    return batch_no in _cancelled_output_nos(session, [batch_no])


def _load_production_for_output(
    session: Session, output_no: str
) -> Production | None:
    return session.execute(
        select(Production)
        .options(selectinload(Production.inputs))
        .where(Production.output_no == output_no)
    ).scalar_one_or_none()


def get_production_record(session: Session, output_no: str) -> Production:
    """按产出编号查询生产原单据（含撤销原因/时间）。

    编号不存在 -> SOURCE_NOT_FOUND(404)；编号存在但不是生产产出
    （原料批次）-> PRODUCTION_NOT_FOUND(404)。
    """
    if session.get(Batch, output_no) is None:
        raise errors.batch_not_found(output_no)
    production = _load_production_for_output(session, output_no)
    if production is None:
        raise errors.production_not_found(output_no)
    return production


def cancel_production(session: Session, output_no: str, data: CancelIn) -> dict[str, Any]:
    """撤销一张录入错误的生产单：返还各来源原投料量、作废产出与投料边。

    仅当产出同时满足以下全部条件时允许撤销，任一不符整笔拒绝、不改变库存：
    1. 产出编号存在且确为生产产出（原料批次不可撤销）；
    2. 该生产单尚未撤销（重复撤销明确拒绝）；
    3. 产出从未被后续生产投料（严格按保留的投料记录判定，与下游是否已撤销无关）；
    3b. 产出从未被分装（一旦分装即视为曾被使用，即使子批次尚未投料）；
    4. 产出当前非疑似（自身 is_suspect 为假；仅因疑似祖先被连带隔离不阻止撤销）；
    5. 产出无质检复核记录。
    6. 产出从未发货（一旦发货即使全部退回也不得撤销原生产单）。

    通过后在同一事务内：
    - 各来源批次余量按“原投料量”返还（来源即使已隔离也照常返还，返还不会
      超过其历史累计消耗，余量恒不超过初始量）；
    - 生产单标记 is_cancelled、记录撤销原因与时间；产出批次保留、编号不得复用，
      但其投料边不再进入有效谱系/疑似传播，且不得再投料；
    - 边失效后按“自身疑似或任一（有效）祖先疑似”重算产出隔离位：撤销后产出
      已无有效祖先，自身又非疑似，因此解除其连带隔离（不触碰任何其它批次）。

    并发：与污染标记/复核一致先取 QC 串行化锁（PG 为 batches 表
    SHARE ROW EXCLUSIVE，SQLite 为 BEGIN IMMEDIATE 全库写锁），再按编号
    升序对“产出 + 各来源”统一加行锁，使撤销 / 投料 / 污染标记 / 复核的
    任意并发都等价于某一串行顺序，不会超用余量或误放行。
    """
    _acquire_qc_lock(session)

    # 产出行（FOR UPDATE）；不存在直接 404
    output_batch = _lock_batch(session, output_no)
    if output_batch is None:
        raise errors.batch_not_found(output_no)

    production = _load_production_for_output(session, output_no)
    if production is None:
        # 编号存在但是原料批次：没有可撤销的生产单
        raise errors.production_not_found(output_no)

    # 重复撤销优先、明确拒绝（不改库存）
    if production.is_cancelled:
        raise errors.production_already_cancelled(output_no)

    # 从未被后续生产投料：按保留的原始投料记录严格判定
    # （下游单据即便已撤销，其投料行仍保留，因此这里不排除 is_cancelled）
    consumer_rows = session.execute(
        select(Production.output_no)
        .join(ProductionInput, ProductionInput.production_id == Production.id)
        .where(ProductionInput.source_no == output_no)
    ).all()
    consumer_nos = sorted({row[0] for row in consumer_rows})
    if consumer_nos:
        raise errors.output_already_consumed(output_no, consumer_nos)

    # 产出一旦被分装（即使子批次尚未投料）即视为曾被使用，拒绝撤销：
    # 分装边已进入谱系/隔离传播，撤销生产单会令来源返还与父子关系自相矛盾。
    split_rows = session.execute(
        select(BatchSplit.child_no).where(BatchSplit.parent_no == output_no)
    ).all()
    child_nos = sorted({row[0] for row in split_rows})
    if child_nos:
        raise errors.output_already_split(output_no, child_nos)

    # 产出自身发生过盘点盘亏即不得撤销原生产单：盘亏已经核销、历史投料与
    # 谱系不改，撤销返还会与已校正的实存矛盾。只看产出自身批次的盘亏行，
    # 来源批次盘亏不影响本单撤销。
    loss_rows = session.execute(
        select(StockCheck.stock_check_no)
        .join(StockCheckLine, StockCheckLine.stock_check_id == StockCheck.id)
        .where(
            StockCheckLine.batch_no == output_no,
            StockCheckLine.loss_qty > 0,
        )
    ).all()
    loss_check_nos = sorted({row[0] for row in loss_rows})
    if loss_check_nos:
        raise errors.output_has_stock_loss(output_no, loss_check_nos)

    # 产出一旦发货即不可撤销原生产单——即使全部退回也不可以。
    # 发货是货物已离厂对外交付的既成事实；退货返还库存并不让生产单回到
    # “录入错误可作废”的状态。按保留的发货行严格判定。
    shipment_rows = session.execute(
        select(Shipment.shipment_no)
        .join(ShipmentLine, ShipmentLine.shipment_id == Shipment.id)
        .where(ShipmentLine.batch_no == output_no)
    ).all()
    shipment_nos = sorted({row[0] for row in shipment_rows})
    if shipment_nos:
        raise errors.output_already_shipped(output_no, shipment_nos)

    # 产出当前非疑似（仅因疑似祖先被隔离不阻止撤销）
    if output_batch.is_suspect:
        raise errors.output_is_suspect(output_no)

    # 产出无复核记录
    review = session.execute(
        select(BatchReview).where(BatchReview.batch_no == output_no)
    ).scalar_one_or_none()
    if review is not None:
        raise errors.production_has_review(output_no)

    # 统一按编号升序锁定来源批次（产出已锁），与并发投料/撤销保持一致加锁顺序
    source_nos = sorted({pi.source_no for pi in production.inputs})
    locked_sources = _lock_batches_in_order(session, source_nos)

    # 载入各投料行的逐库位消耗明细（撤销按原消耗库位返还）。
    # 生产单内来源批次互不重复，但仍按 (来源编号, 库位) 顺序统一加锁，
    # 与生产/分装/调拨保持同一全局加锁顺序。
    input_ids = [pi.id for pi in sorted(production.inputs, key=lambda x: x.id)]
    detail_rows = (
        session.execute(
            select(ProductionInputLocation).where(
                ProductionInputLocation.production_input_id.in_(input_ids)
            )
        ).scalars().all()
        if input_ids
        else []
    )
    details_by_input: dict[int, list[ProductionInputLocation]] = {}
    for row in detail_rows:
        details_by_input.setdefault(row.production_input_id, []).append(row)

    needed_locations: dict[str, set[str]] = {}
    for pi in sorted(production.inputs, key=lambda x: x.id):
        for d in details_by_input.get(pi.id, []):
            needed_locations.setdefault(pi.source_no, set()).add(d.location_code)
    locked_locations: dict[str, dict[str, BatchLocation]] = {}
    for source_no in sorted(needed_locations):
        codes = sorted(needed_locations[source_no])
        # 防御：库位行必存在（生产时即扣减、启动回填覆盖历史单据）；
        # 若因数据异常缺失，先补零行再锁定，返还逻辑保持不变。
        _ensure_location_rows(session, source_no, codes)
        locked_locations[source_no] = _lock_locations(session, source_no, codes)

    cancelled_at = datetime.now(timezone.utc)
    returned: list[dict[str, Any]] = []
    for pi in sorted(production.inputs, key=lambda x: x.id):
        source = locked_sources.get(pi.source_no)
        if source is None:
            # 理论不可达：来源批次必然存在（生产创建时外键约束）
            raise errors.batch_not_found(pi.source_no)
        # 即使来源批次已隔离，也在本事务内返还其原投料量
        source.remaining_qty += pi.qty
        details = details_by_input.get(pi.id, [])
        for d in sorted(details, key=lambda r: r.location_code):
            loc_row = locked_locations[pi.source_no][d.location_code]
            loc_row.qty += d.qty
        returned.append(
            {"batch_no": pi.source_no, "returned_quantity": pi.qty}
        )

    # 作废生产单：保留行与原单据，仅置撤销标记/原因/时间
    production.is_cancelled = True
    production.cancel_reason = data.cancel_reason
    production.cancelled_at = cancelled_at

    # 投料边失效后重算产出隔离位：自身非疑似且已无有效祖先 -> 解除连带隔离。
    # 显式 flush 让后续原始 SQL 谓词读到本事务刚撤销的边。
    session.flush()
    should_quarantine = bool(
        session.execute(
            text(_ANCESTOR_SUSPECT_SQL), {"root": output_no}
        ).scalar_one()
    )
    output_batch.is_quarantined = should_quarantine
    session.flush()

    return {
        "output_batch_no": output_no,
        "status": "CANCELLED",
        "cancel_reason": data.cancel_reason,
        "cancelled_at": cancelled_at.isoformat(),
        "returned_inputs": [
            {
                "batch_no": item["batch_no"],
                "returned_quantity": float(item["returned_quantity"]),
            }
            for item in returned
        ],
    }


# ---------------------------------------------------------------- 谱系与污染

# 边方向：parent -> child（父批次指向子批次），谱系由两类边共同构成：
# - PRODUCTION：source -> output（生产投料边）
# - SPLIT：父批次 -> 子批次（分装边，子批次可独立投料）
# 已撤销生产单的投料边不再进入有效谱系查询，也不参与疑似隔离传播：
# 撤销后产出作废、来源投料已在同一事务返还，该笔单据视为从未发生。
# 分装边不撤销（撤销只针对生产单据），始终参与谱系遍历与隔离传播。
_EDGE_CTE = """
SELECT pi.source_no AS parent_no, p.output_no AS child_no, 'PRODUCTION' AS kind
FROM production_inputs pi
JOIN productions p ON p.id = pi.production_id
WHERE p.is_cancelled = FALSE
UNION ALL
SELECT bs.parent_no, bs.child_no, 'SPLIT'
FROM batch_splits bs
"""


def _descendant_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部后代（直接/间接使用该批次的产出/子批次），带层数。

    谱系是允许汇聚的 DAG（混料、分装都可能使同一后代经多条路径到达），
    递归按 (batch_no, depth) 去重，最终每个批次只保留最小深度，并按
    (depth, batch_no) 稳定排序。
    """
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth) AS (
        SELECT :root, 0
        UNION
        SELECT e.child_no, w.depth + 1
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.parent_no = w.batch_no
    )
    SELECT batch_no, MIN(depth) AS depth
    FROM walk
    WHERE depth > 0
    GROUP BY batch_no
    ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _ancestor_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部祖先（直接/间接被其消耗的原料/父批次），带层数。

    同一祖先可能经投料边与分装边多条路径到达，按批次取最小深度去重。
    """
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth) AS (
        SELECT :root, 0
        UNION
        SELECT e.parent_no, w.depth + 1
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.child_no = w.batch_no
    )
    SELECT batch_no, MIN(depth) AS depth
    FROM walk
    WHERE depth > 0
    GROUP BY batch_no
    ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _load_batches_map(session: Session, nos: list[str]) -> dict[str, Batch]:
    if not nos:
        return {}
    rows = session.execute(select(Batch).where(Batch.batch_no.in_(nos))).scalars().all()
    return {b.batch_no: b for b in rows}


def _lock_descendant_closure(
    session: Session, root_no: str
) -> tuple[Batch | None, list[str], dict[str, Batch]]:
    """在固定点谱系上加载 root 及其全部后代。

    调用方必须先经 ``_acquire_qc_lock`` 与并发生产事务串行化，因此此处读到的
    谱系边与批次状态在本事务内稳定（PG 上表锁排除了并发投料，SQLite 上
    BEGIN IMMEDIATE 全库串行），不存在边被并发追加的问题。

    返回 (root 批次[可能不存在], 后代编号[depth 序], 批次映射)。
    """
    root = _lock_batch(session, root_no)
    if root is None:
        return None, [], {}

    rows = _descendant_rows(session, root_no)
    descendant_nos = [r["batch_no"] for r in rows]
    targets = _load_batches_map(session, descendant_nos)
    return root, descendant_nos, {root_no: root, **targets}


# 判断给定批次是否应隔离（自身疑似或任一祖先疑似）：
# 祖先部分以目标批次为根沿 source -> output 反向递归（严格祖先），
# 再并上目标自身；谱系边以同事务内可见数据为准，谱系为无环 DAG。
_ANCESTOR_SUSPECT_SQL = f"""
WITH RECURSIVE anc(batch_no) AS (
    SELECT e.parent_no
    FROM ({_EDGE_CTE}) e
    WHERE e.child_no = :root
    UNION
    SELECT e.parent_no FROM anc a
    JOIN ({_EDGE_CTE}) e ON e.child_no = a.batch_no
)
SELECT EXISTS(
    SELECT 1 FROM batches s
    WHERE s.is_suspect AND (
        s.batch_no = :root OR s.batch_no IN (SELECT batch_no FROM anc)
    )
)
"""


def _suspect_closure_map(
    session: Session, batch_nos: list[str]
) -> dict[str, bool]:
    """对每个目标批次求：自身疑似，或任一祖先疑似（是否应隔离）。"""
    return {
        no: bool(session.execute(text(_ANCESTOR_SUSPECT_SQL), {"root": no}).scalar_one())
        for no in batch_nos
    }


def lineage(session: Session, batch_no: str) -> dict[str, Any]:
    root = session.get(Batch, batch_no)
    if root is None:
        raise errors.batch_not_found(batch_no)

    up_rows = _ancestor_rows(session, batch_no)
    down_rows = _descendant_rows(session, batch_no)

    up_map = _load_batches_map(session, [r["batch_no"] for r in up_rows])
    down_map = _load_batches_map(session, [r["batch_no"] for r in down_rows])

    def node(r: dict[str, Any], b: Batch) -> dict[str, Any]:
        return {
            "batch_no": b.batch_no,
            "unit": b.unit,
            "remaining_qty": b.remaining_qty,
            "is_suspect": b.is_suspect,
            "is_quarantined": b.is_quarantined,
            "is_cancelled": _is_cancelled_output(session, b.batch_no),
            "depth": r["depth"],
        }

    upstream = [node(r, up_map[r["batch_no"]]) for r in up_rows if r["batch_no"] in up_map]
    downstream = [
        node(r, down_map[r["batch_no"]]) for r in down_rows if r["batch_no"] in down_map
    ]

    involved = set(up_map) | set(down_map) | {batch_no}
    edges = session.execute(
        select(ProductionInput.source_no, Production.output_no)
        .join(Production, Production.id == ProductionInput.production_id)
        .where(
            ProductionInput.source_no.in_(involved),
            Production.output_no.in_(involved),
            # 撤销的投料边不再进入有效谱系查询
            Production.is_cancelled.is_(False),
        )
    ).all()
    edge_list = [
        {"from": s, "to": o, "kind": "PRODUCTION"} for s, o in edges
    ]
    # 分装边与投料边一起进入谱系
    split_edges = session.execute(
        select(BatchSplit.parent_no, BatchSplit.child_no).where(
            BatchSplit.parent_no.in_(involved),
            BatchSplit.child_no.in_(involved),
        )
    ).all()
    edge_list.extend(
        {"from": p, "to": c, "kind": "SPLIT"} for p, c in split_edges
    )
    return {
        "batch_no": batch_no,
        "is_cancelled": _is_cancelled_output(session, batch_no),
        "upstream": upstream,
        "downstream": downstream,
        "edges": edge_list,
    }


def mark_suspect(session: Session, data: SuspectIn) -> dict[str, Any]:
    """标记批次疑似污染，并隔离其全部后代产出批次。

    混料规则：一个产出若混有受污染来源（直接或间接），该产出整体隔离；
    隔离批次不得再投料（提交生产时拦截）。

    并发：先取 QC 串行化锁（PG 表锁 / SQLite 全库写锁），再读取后代闭包
    并写状态，与生产、复核事务整体等价于某一串行顺序。
    """
    _acquire_qc_lock(session)
    batch, descendant_nos, locked = _lock_descendant_closure(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)

    previously_quarantined = {no for no, b in locked.items() if b.is_quarantined}

    batch.is_suspect = True
    batch.is_quarantined = True
    if data.note:
        batch.note = data.note

    for no in descendant_nos:
        target = locked[no]
        target.is_quarantined = True

    session.flush()
    newly = sorted(set(descendant_nos) - previously_quarantined)
    return {
        "source": data.batch_no,
        "is_suspect": True,
        "affected_batch_nos": descendant_nos,
        "newly_quarantined": newly,
    }


def review_suspect(session: Session, data: ReviewIn) -> dict[str, Any]:
    """质检复核确认误报：撤销批次疑似标记，并重算自身及全部后代的隔离状态。

    规则：
    - 只接受当前仍疑似的批次；编号不存在 -> SOURCE_NOT_FOUND(404)，
      已撤销或从未疑似 -> BATCH_NOT_SUSPECT(409)，均不改变任何状态；
    - 重算范围为该批次及其全部后代：自身或任一祖先仍疑似则继续隔离，否则解除；
    - 混料后代若还有另一条疑似来源（祖先），不得因本来源撤销而解除隔离。

    并发：先取 QC 串行化锁，再按与 mark_suspect 相同的固定点闭包读取，
    与生产事务的来源行锁互斥，保证复核 / 污染标记 / 投料并发时等价于
    某一串行顺序；隔离批次在任一串行结局下都不会被投料。
    """
    _acquire_qc_lock(session)
    batch, descendant_nos, locked = _lock_descendant_closure(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)
    if not batch.is_suspect:
        raise errors.batch_not_suspect(data.batch_no)

    targets = [data.batch_no, *descendant_nos]

    # 撤销自身疑似标记并固化复核说明（同一批次可能经历 标记→撤销→再标记→再撤销，
    # 复用已有复核行更新说明，避免唯一约束冲突）
    batch.is_suspect = False
    review = session.execute(
        select(BatchReview).where(BatchReview.batch_no == data.batch_no)
    ).scalar_one_or_none()
    if review is None:
        session.add(
            BatchReview(batch_no=data.batch_no, review_note=data.review_note)
        )
    else:
        review.review_note = data.review_note
        review.reviewed_at = func.now()

    # 重算：自身疑似或任一祖先疑似 -> 隔离；否则解除。
    # 注意：session.execute(text()) 不会触发 ORM autoflush，必须显式 flush，
    # 让下面的原始 SQL 谓词读到本事务刚撤销的 is_suspect 状态。
    session.flush()
    should_quarantine = _suspect_closure_map(session, targets)

    released: list[str] = []
    still: list[str] = []
    for no in targets:
        target = locked[no]
        quarantined = should_quarantine.get(no, False)
        if target.is_quarantined and not quarantined:
            released.append(no)
        target.is_quarantined = quarantined
        if quarantined:
            still.append(no)

    session.flush()
    released.sort()
    # still 中的第一个即复核批次自身（撤销后通常已解除；仍疑似祖先时会保留）
    still.sort()
    return {
        "reviewed_batch_no": data.batch_no,
        "is_suspect": False,
        "review_note": data.review_note,
        "affected_batch_nos": descendant_nos,
        "released_batch_nos": released,
        "still_quarantined": still,
    }
