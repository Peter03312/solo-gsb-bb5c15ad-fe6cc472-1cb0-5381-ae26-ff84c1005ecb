"""批次追溯 HTTP 路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy.orm import Session

from . import service
from .deps import SessionDep
from .errors import AppError
from .models import Batch, Production
from .schemas import (
    BatchLocationsOut,
    BatchOut,
    CancellationOut,
    CancelIn,
    DescendantsOut,
    LineageOut,
    ProductionIn,
    ProductionOut,
    ProductionRecordOut,
    RawBatchIn,
    ReviewIn,
    ReviewOut,
    SplitIn,
    SplitOut,
    StockCheckIn,
    StockCheckOut,
    SuspectIn,
    TransferIn,
    TransferOut,
)

router = APIRouter()


def _batch_out(batch: Batch, is_cancelled: bool = False) -> dict[str, Any]:
    return {
        "batch_no": batch.batch_no,
        "unit": batch.unit,
        "initial_qty": float(batch.initial_qty),
        "remaining_qty": float(batch.remaining_qty),
        "is_suspect": batch.is_suspect,
        "is_quarantined": batch.is_quarantined,
        "is_cancelled": is_cancelled,
        "note": batch.note,
    }


def _production_out(prod: Production, unit: str) -> dict[str, Any]:
    return {
        "output_batch_no": prod.output_no,
        "unit": unit,
        "output_quantity": float(prod.output_qty),
        "inputs": [
            {"batch_no": pi.source_no, "quantity": float(pi.qty)}
            for pi in sorted(prod.inputs, key=lambda x: x.id)
        ],
    }


def _production_record_out(prod: Production, unit: str) -> dict[str, Any]:
    """原单据查询响应：在生产单基础上附带撤销信息。"""
    cancelled_at = prod.cancelled_at
    if cancelled_at is not None:
        # SQLite 取回 naive datetime，统一为带时区的 ISO 8601 字符串
        if cancelled_at.tzinfo is None:
            from datetime import timezone

            cancelled_at = cancelled_at.replace(tzinfo=timezone.utc)
        cancelled_at = cancelled_at.isoformat()
    return {
        "output_batch_no": prod.output_no,
        "unit": unit,
        "output_quantity": float(prod.output_qty),
        "inputs": [
            {"batch_no": pi.source_no, "quantity": float(pi.qty)}
            for pi in sorted(prod.inputs, key=lambda x: x.id)
        ],
        "is_cancelled": prod.is_cancelled,
        "cancel_reason": prod.cancel_reason,
        "cancelled_at": cancelled_at,
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/raw-batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_raw_batch(
    body: RawBatchIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        batch = service.register_raw_batch(session, body)
        return status.HTTP_201_CREATED, _batch_out(batch)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/productions", response_model=ProductionOut, status_code=status.HTTP_201_CREATED)
def create_production(
    body: ProductionIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        prod = service.submit_production(session, body)
        return status.HTTP_201_CREATED, _production_out(prod, body.unit)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/batches/{batch_no}", response_model=BatchOut)
def get_batch(batch_no: str, session: SessionDep) -> dict[str, Any]:
    batch = session.get(Batch, batch_no)
    if batch is None:
        from .errors import batch_not_found

        raise batch_not_found(batch_no)
    is_cancelled = service._is_cancelled_output(session, batch_no)
    return _batch_out(batch, is_cancelled)


@router.get("/batches/{batch_no}/lineage", response_model=LineageOut)
def get_lineage(batch_no: str, session: SessionDep) -> dict[str, Any]:
    return service.lineage(session, batch_no)


@router.post(
    "/batches/{batch_no}/splits",
    response_model=SplitOut,
    status_code=status.HTTP_201_CREATED,
)
def split_batch(
    batch_no: str,
    body: SplitIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """按父批次编号分装：提交多个互异新编号与正数分装量。

    返回父批次新余量、子批次清单与分装关系；任一校验不符整笔拒绝、不改库存。
    分装不产生生产单，子批次继承单位并可作为正常生产来源。必须携带幂等键。
    """
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_201_CREATED, service.split_batch(session, batch_no, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/transfers", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
def create_transfer(
    body: TransferIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """库位调拨：凭唯一调拨单号把某批次的正数数量从来源库位移到目标库位。

    返回调拨后两库位余额与该批次全部库位余额；库位为空/相同、批次不存在或
    来源库位不足时整单拒绝、余额不变。隔离批次允许调拨。必须携带幂等键。
    """
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_201_CREATED, service.transfer_stock(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/batches/{batch_no}/locations", response_model=BatchLocationsOut)
def get_batch_locations(batch_no: str, session: SessionDep) -> dict[str, Any]:
    """查询批次逐库位余额（按库位编号排序）；各库位之和等于全局余量。"""
    return service.get_batch_locations(session, batch_no)


@router.post("/stock-checks", response_model=StockCheckOut, status_code=status.HTTP_201_CREATED)
def create_stock_check(
    body: StockCheckIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """仓库盘点：凭唯一盘点单号提交多条互异的（批次, 库位, 账面量, 实点量）。

    两种数量非负、实点量不得高于账面量、整单至少一条盘亏；提交时逐行核对
    当前库位实存恰等于账面量，任一不符整单拒绝、不留盘点记录。成功后同事务
    按差额扣减库位与全局余量并固化不可修改的原值快照。必须携带幂等键。
    """
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_201_CREATED, service.submit_stock_check(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/stock-checks/{stock_check_no}", response_model=StockCheckOut)
def get_stock_check(stock_check_no: str, session: SessionDep) -> dict[str, Any]:
    """按单号查询盘点单：状态及逐行原值、差额与单位。"""
    return service.get_stock_check(session, stock_check_no)


@router.post("/contamination", response_model=DescendantsOut, status_code=status.HTTP_200_OK)
def flag_suspect(
    body: SuspectIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    # 污染标记本身是幂等动作：无键也允许，有键则走标准重放/冲突语义
    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.mark_suspect(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/reviews", response_model=ReviewOut, status_code=status.HTTP_200_OK)
def review_suspect(
    body: ReviewIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """质检复核：确认疑似批次为误报，撤销标记并重算隔离。必须携带幂等键。"""
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.review_suspect(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post(
    "/productions/{output_no}/cancellation",
    response_model=CancellationOut,
    status_code=status.HTTP_200_OK,
)
def cancel_production(
    output_no: str,
    body: CancelIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """撤销录入错误的生产单：凭产出批次编号 + 非空撤销原因。

    在同一事务内返还各来源原投料量、作废产出（不得再投料）与投料边
    （不再进入有效谱系/疑似传播）。必须携带幂等键。
    """
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.cancel_production(session, output_no, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/productions/{output_no}", response_model=ProductionRecordOut)
def get_production(output_no: str, session: SessionDep) -> dict[str, Any]:
    """按产出编号查询生产原单据（未撤销单据同样可查，附撤销状态与原因）。"""
    prod = service.get_production_record(session, output_no)
    output = session.get(Batch, output_no)
    return _production_record_out(prod, output.unit if output is not None else "")
