"""发货 / 退货并发测试。

不变式：
- 绝不超扣 / 超退：库位余额、全局余量不会为负，任一原行累计退回不超过发出量；
- 两层恒等：每个批次各库位余额之和恒等于全局余量；
- 发货 / 退货与投料、分装、调拨、盘点、疑似标记、复核、撤销交错时，
  结果等价于某一串行顺序（PostgreSQL 行锁；SQLite BEGIN IMMEDIATE 全库串行）。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import text

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch, DEFAULT_LOCATION_CODE
from app.schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    ReturnIn,
    ShipmentIn,
    SplitIn,
    StockCheckIn,
    StockCheckLineIn,
    SuspectIn,
    TransferIn,
)


def _qty(v: str) -> Decimal:
    return Decimal(v)


def _register(no: str = "RM-1", qty: str = "100") -> None:
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no=no, unit="kg", quantity=_qty(qty))
        )


def _ship_in(no: str, batch: str, qty: str, loc: str = DEFAULT_LOCATION_CODE) -> ShipmentIn:
    return ShipmentIn(lines=[dict(batch_no=batch, location_code=loc, quantity=_qty(qty))],
                      shipment_no=no)  # type: ignore[arg-type]


def _return_in(ret_no: str, ship_no: str, line_no: int, qty: str) -> ReturnIn:
    return ReturnIn(return_no=ret_no, shipment_no=ship_no,
                    lines=[{"line_no": line_no, "quantity": _qty(qty)}])  # type: ignore[list-item]


def _totals(batch: str = "RM-1") -> tuple[Decimal, Decimal]:
    """返回 (全局余量, 各库位余额之和)。"""
    with session_scope() as s:
        global_qty = Decimal(s.get(Batch, batch).remaining_qty)
        loc_sum = Decimal(s.execute(
            text("SELECT COALESCE(SUM(qty), 0) FROM batch_locations WHERE batch_no=:b"),
            {"b": batch},
        ).scalar_one())
        return global_qty, loc_sum


def _run(workers):
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        return list(pool.map(lambda f: f(), workers))


def _guard(fn):
    try:
        with session_scope() as s:
            return ("ok", fn(s))
    except AppError as exc:
        return (exc.code, None)
    except Exception as exc:  # noqa: BLE001
        return (f"ERROR:{type(exc).__name__}:{exc}", None)


def test_concurrent_shipments_never_overdraw(engine):
    """6 笔各 15 的发货并发（余量 100）：全成功，最终余量恰 10。"""
    _register(qty="100")
    barrier = threading.Barrier(6)
    results: list[str] = []

    def worker(idx: int):
        barrier.wait()
        results.append(_guard(lambda s: service.submit_shipment(
            s, _ship_in(f"SH-{idx}", "RM-1", "15")))[0])

    _run([lambda i=i: worker(i) for i in range(6)])
    assert results == ["ok"] * 6, results
    g, loc = _totals()
    assert g == loc == Decimal("10.0000")


def test_concurrent_shipments_overallocation_rejects_excess(engine):
    """4 笔各 10 的发货并发（余量 20）：恰好 2 成功、2 余额不足，余 0。"""
    _register(qty="20")
    barrier = threading.Barrier(4)
    results: list[str] = []

    def worker(idx: int):
        barrier.wait()
        results.append(_guard(lambda s: service.submit_shipment(
            s, _ship_in(f"SH-{idx}", "RM-1", "10")))[0])

    _run([lambda i=i: worker(i) for i in range(4)])
    assert results.count("ok") == 2, results
    assert results.count("INSUFFICIENT_BALANCE") == 2, results
    assert not [r for r in results if r.startswith("ERROR")], results
    g, loc = _totals()
    assert g == loc == 0


def test_concurrent_identical_shipment_no_exactly_one_wins(engine):
    """相同发货单号并发：唯一约束兜底，恰好一笔成功，库存只扣一次。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        barrier.wait()
        results.append(_guard(lambda s: service.submit_shipment(
            s, _ship_in("SH-SAME", "RM-1", "10")))[0])

    _run([worker, worker])
    assert results.count("ok") == 1, results
    assert "SHIPMENT_NO_REUSED" in results, results
    g, loc = _totals()
    assert g == loc == Decimal("90.0000")


def test_concurrent_shipment_and_transfer_never_overdraw(engine):
    """发货（DEFAULT 出 30）与调拨（DEFAULT -> A, 40）并发：

    两种串行结局：
    - 发货先：DEFAULT 100-30=70，调拨再走 40 -> 都成功，全局 70；
    - 调拨先：DEFAULT 60，发货 30 仍成功 -> 全局 70；
    本例两种顺序均成功（数量不冲突），关键是两层恒等、无负余额。
    """
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def ship_worker():
        barrier.wait()
        results.append(("ship",) + _guard(lambda s: service.submit_shipment(
            s, _ship_in("SH-1", "RM-1", "30"))))

    def transfer_worker():
        barrier.wait()
        results.append(("tr",) + _guard(lambda s: service.transfer_stock(s, TransferIn(
            transfer_no="T-1", batch_no="RM-1", source_location=DEFAULT_LOCATION_CODE,
            target_location="A", quantity=_qty("40")))))

    _run([ship_worker, transfer_worker])
    by_kind = {k: v for k, v, _ in results}
    assert by_kind == {"ship": "ok", "tr": "ok"}, results
    g, loc = _totals()
    assert g == loc == Decimal("70.0000")


def test_concurrent_shipment_and_production_consumption_never_overuse(engine):
    """发货 60 与生产投料 60 并发（余量 100）：任一串行结局都不超扣。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def ship_worker():
        barrier.wait()
        results.append(("ship",) + _guard(lambda s: service.submit_shipment(
            s, _ship_in("SH-1", "RM-1", "60"))))

    def prod_worker():
        barrier.wait()
        results.append(("prod",) + _guard(lambda s: service.submit_production(s, ProductionIn(
            output_batch_no="FG-1", unit="kg", output_quantity=_qty("60"),
            inputs=[{"batch_no": "RM-1", "quantity": _qty("60")}]))))  # type: ignore[list-item]

    _run([ship_worker, prod_worker])
    by_kind = {k: v for k, v, _ in results}
    assert set(by_kind) == {"ship", "prod"}
    # 恰好一个成功、另一个余额不足（60+60>100）
    assert results.count(("ship", "ok", None)) + sum(
        1 for k, c, _ in results if k == "prod" and c == "ok") == 1, results
    assert "ok" in (by_kind["ship"], by_kind["prod"]), results
    assert set(by_kind.values()) <= {"ok", "INSUFFICIENT_BALANCE"}, results
    g, loc = _totals()
    assert g == loc == Decimal("40.0000")
    with session_scope() as s:
        fg = s.get(Batch, "FG-1")
        if by_kind["prod"] == "ok":
            assert fg is not None and Decimal(fg.remaining_qty) == Decimal("60.0000")
        else:
            assert fg is None


def test_concurrent_shipment_and_stock_check_serializable(engine):
    """发货 20 与盘点（DEFAULT 100->90）并发：合法结局下两层恒等、绝不超扣。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def ship_worker():
        barrier.wait()
        results.append(("ship",) + _guard(lambda s: service.submit_shipment(
            s, _ship_in("SH-1", "RM-1", "20"))))

    def check_worker():
        barrier.wait()
        results.append(("check",) + _guard(lambda s: service.submit_stock_check(s, StockCheckIn(
            stock_check_no="SC-1",
            lines=[StockCheckLineIn(batch_no="RM-1", location_code=DEFAULT_LOCATION_CODE,
                                    book_qty=_qty("100"), actual_qty=_qty("90"))]))))

    _run([ship_worker, check_worker])
    by_kind = {k: v for k, v, _ in results}
    assert by_kind["ship"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert by_kind["check"] in ("ok", "STOCK_CHECK_BOOK_CHANGED"), results
    assert not [r for r in results if str(r[1]).startswith("ERROR")], results
    g, loc = _totals()
    assert g == loc
    # 发货成功（20 出）与盘点成功（10 亏）若同时成立则为 70；
    # 盘点被账面变动拒绝则仅发货为 80。
    assert g in (Decimal("70.0000"), Decimal("80.0000"))


def test_concurrent_returns_same_original_line_never_over_return(engine):
    """两笔退货对同一原行（发出 10）各退 8 并发：
    恰好一笔成功、另一笔超退拒绝，累计退回恰为 8，库存只返还一次。"""
    _register(qty="100")
    with session_scope() as s:
        service.submit_shipment(s, _ship_in("SH-1", "RM-1", "10"))

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(ret_no: str):
        barrier.wait()
        results.append(_guard(lambda s: service.submit_return(
            s, _return_in(ret_no, "SH-1", 1, "8")))[0])

    _run([lambda: worker("RT-1"), lambda: worker("RT-2")])
    assert results.count("ok") == 1, results
    assert results.count("RETURN_EXCEEDS_SHIPPED") == 1, results
    assert not [r for r in results if r.startswith("ERROR")], results
    g, loc = _totals()
    assert g == loc == Decimal("98.0000")  # 100 - 10 发货 + 8 退货
    with session_scope() as s:
        view = service.get_shipment(s, "SH-1")
    assert view["lines"][0]["returned_qty"] == 8.0
    assert view["status"] == "PARTIALLY_RETURNED"


def test_concurrent_distinct_returns_both_apply(engine):
    """两笔对不同原行的退货并发：都成功，分别返还原库位，库存正确。"""
    _register("RM-1", "100")
    _register("RM-2", "50")
    with session_scope() as s:
        service.submit_shipment(s, ShipmentIn(
            shipment_no="SH-1",
            lines=[
                {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE, "quantity": _qty("30")},  # type: ignore[dict-item]
                {"batch_no": "RM-2", "location_code": DEFAULT_LOCATION_CODE, "quantity": _qty("20")},  # type: ignore[dict-item]
            ],
        ))

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(ret_no: str, line_no: int):
        barrier.wait()
        results.append(_guard(lambda s: service.submit_return(
            s, _return_in(ret_no, "SH-1", line_no, "10")))[0])

    _run([lambda: worker("RT-1", 1), lambda: worker("RT-2", 2)])
    assert results == ["ok", "ok"], results
    g1, l1 = _totals("RM-1")
    g2, l2 = _totals("RM-2")
    assert g1 == l1 == Decimal("80.0000")   # 100-30+10
    assert g2 == l2 == Decimal("40.0000")   # 50-20+10
    with session_scope() as s:
        view = service.get_shipment(s, "SH-1")
    assert view["status"] == "PARTIALLY_RETURNED"


def test_concurrent_identical_return_no_exactly_one_wins(engine):
    """相同退货单号并发：唯一约束兜底，恰好一笔成功。"""
    _register(qty="100")
    with session_scope() as s:
        service.submit_shipment(s, _ship_in("SH-1", "RM-1", "10"))

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        barrier.wait()
        results.append(_guard(lambda s: service.submit_return(
            s, _return_in("RT-SAME", "SH-1", 1, "5")))[0])

    _run([worker, worker])
    assert results.count("ok") == 1, results
    assert "RETURN_NO_REUSED" in results, results
    g, loc = _totals()
    assert g == loc == Decimal("95.0000")


def test_concurrent_return_and_transfer_keep_two_layer_invariant(engine):
    """退货（DEFAULT 回 10）与调拨（DEFAULT -> A, 20）并发：两层恒等、无负余额。"""
    _register(qty="100")
    with session_scope() as s:
        service.submit_shipment(s, _ship_in("SH-1", "RM-1", "30"))
    # 当前 DEFAULT=70
    barrier = threading.Barrier(2)
    results: list[str] = []

    def return_worker():
        barrier.wait()
        results.append(("ret",) + _guard(lambda s: service.submit_return(
            s, _return_in("RT-1", "SH-1", 1, "10"))))

    def transfer_worker():
        barrier.wait()
        results.append(("tr",) + _guard(lambda s: service.transfer_stock(s, TransferIn(
            transfer_no="T-1", batch_no="RM-1", source_location=DEFAULT_LOCATION_CODE,
            target_location="A", quantity=_qty("20")))))

    _run([return_worker, transfer_worker])
    by_kind = {k: v for k, v, _ in results}
    assert by_kind == {"ret": "ok", "tr": "ok"}, results
    g, loc = _totals()
    # 退货 +10 全局，调拨不改全局：100-30+10 = 80
    assert g == loc == Decimal("80.0000")
    with session_scope() as s:
        rows = dict(s.execute(
            text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
        ).all())
    assert all(Decimal(v) >= 0 for v in rows.values())
    assert Decimal(rows["A"]) == Decimal("20.0000")


def test_concurrent_shipment_and_flag_equivalent_to_serial(engine):
    """发货与污染标记并发：等价于某一串行顺序——标记先则发货被
    BATCH_QUARANTINED 拒绝；发货先则发货成功（标记不影响已成交发货）。
    不允许出现锁错误，库存两层恒等。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def ship_worker():
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_shipment(s, _ship_in("SH-1", "RM-1", "10"))
            results["ship"] = "ok"
        except AppError as exc:
            results["ship"] = exc.code
        except Exception as exc:  # noqa: BLE001
            results["ship"] = f"ERROR:{type(exc).__name__}:{exc}"

    def flag_worker():
        barrier.wait()
        try:
            with session_scope() as s:
                service.mark_suspect(s, SuspectIn(batch_no="RM-1"))
            results["flag"] = "ok"
        except Exception as exc:  # noqa: BLE001
            results["flag"] = f"ERROR:{type(exc).__name__}:{exc}"

    _run([ship_worker, flag_worker])
    assert results["flag"] == "ok", results
    assert results["ship"] in ("ok", "BATCH_QUARANTINED"), results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        assert batch.is_suspect is True and batch.is_quarantined is True
        g = Decimal(batch.remaining_qty)
        loc = Decimal(s.execute(
            text("SELECT COALESCE(SUM(qty),0) FROM batch_locations WHERE batch_no='RM-1'")
        ).scalar_one())
    assert g == loc
    assert g == (Decimal("90.0000") if results["ship"] == "ok" else Decimal("100.0000"))


def test_concurrent_cancel_and_shipment_never_both_succeed(engine):
    """撤销产出生产单与对该产出发货并发：只可能撤销成功（发货被
    BATCH_CANCELLED 拒绝）或发货成功（撤销被 OUTPUT_ALREADY_SHIPPED 拒绝），
    绝不允许二者都成功。"""
    _register(qty="100")
    with session_scope() as s:
        service.submit_production(s, ProductionIn(
            output_batch_no="FG-1", unit="kg", output_quantity=_qty("30"),
            inputs=[{"batch_no": "RM-1", "quantity": _qty("30")}]))  # type: ignore[list-item]

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def ship_worker():
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_shipment(s, _ship_in("SH-1", "FG-1", "10"))
            results["ship"] = "ok"
        except AppError as exc:
            results["ship"] = exc.code
        except Exception as exc:  # noqa: BLE001
            results["ship"] = f"ERROR:{type(exc).__name__}:{exc}"

    def cancel_worker():
        barrier.wait()
        try:
            with session_scope() as s:
                service.cancel_production(s, "FG-1", CancelIn(cancel_reason="录入错误"))
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:  # noqa: BLE001
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    _run([ship_worker, cancel_worker])
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["ship"] in ("ok", "BATCH_CANCELLED"), results
    assert results["cancel"] in ("ok", "OUTPUT_ALREADY_SHIPPED"), results
    assert results["ship"] == "ok" or results["cancel"] == "ok", results
    assert not (results["ship"] == "ok" and results["cancel"] == "ok"), results
    with session_scope() as s:
        prod = service.get_production_record(s, "FG-1")
        if results["cancel"] == "ok":
            assert prod.is_cancelled is True
            assert results["ship"] == "BATCH_CANCELLED"
        else:
            assert prod.is_cancelled is False
            assert results["ship"] == "ok"


def test_concurrent_shipment_and_split_never_overuse(engine):
    """发货 40 与分装 40 并发（余量 100）：都成功，全局 20、两层恒等。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def ship_worker():
        barrier.wait()
        results.append(("ship",) + _guard(lambda s: service.submit_shipment(
            s, _ship_in("SH-1", "RM-1", "40"))))

    def split_worker():
        barrier.wait()
        results.append(("split",) + _guard(lambda s: service.split_batch(s, "RM-1", SplitIn(
            splits=[{"batch_no": "RM-1-A", "quantity": _qty("40")}]))))  # type: ignore[list-item]

    _run([ship_worker, split_worker])
    by_kind = {k: v for k, v, _ in results}
    assert by_kind == {"ship": "ok", "split": "ok"}, results
    g, loc = _totals("RM-1")
    assert g == loc == Decimal("20.0000")
    g2, l2 = _totals("RM-1-A")
    assert g2 == l2 == Decimal("40.0000")
