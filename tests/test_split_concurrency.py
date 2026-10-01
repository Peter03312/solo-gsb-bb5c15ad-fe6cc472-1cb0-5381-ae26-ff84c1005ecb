"""分装并发测试：分装 / 投料 / 污染标记 并发时不得超用或漏隔离。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch
from app.schemas import (
    ProductionIn,
    RawBatchIn,
    SplitIn,
    SuspectIn,
)


def _qty(value: str) -> Decimal:
    return Decimal(value)


def test_concurrent_splits_never_overuse(engine):
    # 父批次余量 100，4 个线程各分装 30：3 成功、1 拒绝，父余量恰为 10
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.split_batch(
                    s,
                    "RM-1",
                    SplitIn(
                        splits=[
                            {"batch_no": f"RM-1-{idx}", "quantity": _qty("30")}  # type: ignore[list-item]
                        ]
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    assert results.count("ok") == 3, results
    assert results.count("INSUFFICIENT_BALANCE") == 1, results
    assert not [r for r in results if r.startswith("ERROR")], results
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("10.0000")
        # 子批次余量之和恰好等于总装出量
        children_total = sum(
            s.get(Batch, f"RM-1-{idx}").remaining_qty
            for idx in range(4)
            if s.get(Batch, f"RM-1-{idx}") is not None
        )
        assert children_total == Decimal("90.0000")


def test_concurrent_split_and_production_never_overuse(engine):
    # 分装 60 与投料 60 并发（余量共 100）：恰好一个成功、另一个被拒
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def split_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.split_batch(
                    s,
                    "RM-1",
                    SplitIn(
                        splits=[{"batch_no": "RM-1-A", "quantity": _qty("60")}]  # type: ignore[list-item]
                    ),
                )
            results["split"] = "ok"
        except AppError as exc:
            results["split"] = exc.code
        except Exception as exc:
            results["split"] = f"ERROR:{type(exc).__name__}:{exc}"

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=_qty("60"),
                        inputs=[{"batch_no": "RM-1", "quantity": _qty("60")}],  # type: ignore[list-item]
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [split_worker, produce_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["split"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert results["produce"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert results["split"] != results["produce"], results  # 一成一拒

    with session_scope() as s:
        parent = s.get(Batch, "RM-1")
        child = s.get(Batch, "RM-1-A")
        fg = s.get(Batch, "FG-1")
        if results["split"] == "ok":
            # 分装先：父余量 40，子批次 60 存在，FG-1 未创建
            assert child is not None and child.remaining_qty == Decimal("60.0000")
            assert parent.remaining_qty == Decimal("40.0000")
            assert fg is None
        else:
            # 投料先：父余量 40，FG-1 60 存在，子批次未创建
            assert fg is not None and fg.remaining_qty == Decimal("60.0000")
            assert parent.remaining_qty == Decimal("40.0000")
            assert child is None


def test_split_concurrent_with_flag_equivalent_to_serial(engine):
    """分装 RM-1 与“标记 RM-1 疑似”并发：

    结局 A：标记先 -> 分装被 BATCH_QUARANTINED 拒绝，子批次不存在；
    结局 B：分装先 -> 分装成功，标记沿分装边把子批次一并隔离（不漏隔离）。
    """
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def split_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.split_batch(
                    s,
                    "RM-1",
                    SplitIn(
                        splits=[{"batch_no": "RM-1-A", "quantity": _qty("30")}]  # type: ignore[list-item]
                    ),
                )
            results["split"] = "ok"
        except AppError as exc:
            results["split"] = exc.code
        except Exception as exc:
            results["split"] = f"ERROR:{type(exc).__name__}:{exc}"

    def flag_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.mark_suspect(s, SuspectIn(batch_no="RM-1"))
            results["flag"] = "ok"
        except Exception as exc:
            results["flag"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [split_worker, flag_worker]))

    assert results["flag"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["split"] in ("ok", "BATCH_QUARANTINED"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        child = s.get(Batch, "RM-1-A")
        assert rm1.is_suspect is True and rm1.is_quarantined is True
        if results["split"] == "ok":
            # 分装先提交：标记传播必须沿分装边补隔离
            assert child is not None
            assert child.is_quarantined is True
            assert rm1.remaining_qty == Decimal("70.0000")
        else:
            assert child is None
            assert rm1.remaining_qty == Decimal("100.0000")
