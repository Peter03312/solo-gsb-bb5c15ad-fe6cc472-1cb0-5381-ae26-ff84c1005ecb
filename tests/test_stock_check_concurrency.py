"""盘点并发测试：盘点与盘点 / 调拨 / 投料 / 分装 / 撤销交错时原子核对扣减。

不变式：
- 绝不超扣：任何库位余额、批次全局余量都不会变成负数；
- 不覆盖新余额：后到事务要么在锁后读到新账面（STOCK_CHECK_BOOK_CHANGED
  整单拒绝），要么按新账面核对通过；
- 两层恒等：每个批次各库位余额之和恒等于全局余量。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import text

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch, BatchLocation, DEFAULT_LOCATION_CODE
from app.schemas import (
    ProductionIn,
    RawBatchIn,
    SplitIn,
    StockCheckIn,
    StockCheckLineIn,
    TransferIn,
)


def _qty(v: str) -> Decimal:
    return Decimal(v)


def _register(no: str = "RM-1", qty: str = "100") -> None:
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no=no, unit="kg", quantity=_qty(qty))
        )


def _check_in(no: str, batch: str, book: str, actual: str, loc: str = DEFAULT_LOCATION_CODE):
    return StockCheckIn(
        stock_check_no=no,
        lines=[StockCheckLineIn(
            batch_no=batch, location_code=loc,
            book_qty=_qty(book), actual_qty=_qty(actual))],
    )


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


def test_concurrent_stock_checks_same_line_exactly_one_succeeds(engine):
    """两个线程对同一库位提交同一账面的盘亏：
    成功者扣减，另一事务随后读到新余额，账面不符整单拒绝；绝不超扣。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(no: str):
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, _check_in(no, "RM-1", "100", "90"))
        results.append(_guard(action)[0])

    _run([lambda: worker("SC-1"), lambda: worker("SC-2")])

    assert results.count("ok") == 1, results
    assert results.count("STOCK_CHECK_BOOK_CHANGED") == 1, results
    global_qty, loc_sum = _totals()
    assert global_qty == Decimal("90.0000") == loc_sum


def test_concurrent_stock_checks_chained_book_values_both_apply(engine):
    """后到者按新账面申报时两笔盘点可先后生效，累计扣减正确。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(no, book, actual):
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, _check_in(no, "RM-1", book, actual))
        results.append(_guard(action)[0])

    # 线程 A 认为账面 100 实点 90；线程 B 认为账面 90 实点 85
    _run([
        lambda: worker("SC-1", "100", "90"),
        lambda: worker("SC-2", "90", "85"),
    ])
    # 恰好一种串行顺序成立：
    #   A 先（100->90 成功）、B 后（90->85 成功）=> ok/ok，余 85
    #   B 先（账面100 申报90 不符）失败、A 成功 => BOOK_CHANGED/ok，余 90
    assert set(results) in ({"ok"}, {"ok", "STOCK_CHECK_BOOK_CHANGED"}), results
    global_qty, loc_sum = _totals()
    if results.count("ok") == 2:
        assert global_qty == Decimal("85.0000") == loc_sum
    else:
        assert global_qty == Decimal("90.0000") == loc_sum


def test_concurrent_stock_check_and_transfer_never_overuse_or_overwrite(engine):
    """盘点（DEFAULT 100->80）与调拨（DEFAULT -> A, 30）并发：
    任一串行结局下余额非负、两层恒等、盘点绝不覆盖调拨后的新余额。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def check_worker():
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, _check_in("SC-1", "RM-1", "100", "80"))
        results.append(("check",) + _guard(action))

    def transfer_worker():
        barrier.wait()
        def action(s):
            return service.transfer_stock(s, TransferIn(
                transfer_no="T1", batch_no="RM-1",
                source_location=DEFAULT_LOCATION_CODE, target_location="A",
                quantity=_qty("30")))
        results.append(("transfer",) + _guard(action))

    _run([check_worker, transfer_worker])

    by_kind = {kind: code for kind, code, _ in results}
    assert by_kind["transfer"] == "ok", results
    # 盘点：若调拨先落库（DEFAULT=70），账面100核对失败；若盘点先落库，
    # 调拨在盘点后的 80 上扣 30 成功（DEFAULT=50,A=30,全局80）。
    assert by_kind["check"] in ("ok", "STOCK_CHECK_BOOK_CHANGED"), results
    global_qty, loc_sum = _totals()
    assert global_qty == loc_sum  # 两层恒等
    with session_scope() as s:
        rows = dict(s.execute(
            text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
        ).all())
    assert all(Decimal(v) >= 0 for v in rows.values())
    assert Decimal(rows["A"]) == Decimal("30.0000")
    if by_kind["check"] == "ok":
        # 盘点先：DEFAULT 100 -20 盘亏 =80，再 -30 调拨 =50；全局 80
        assert Decimal(rows[DEFAULT_LOCATION_CODE]) == Decimal("50.0000")
        assert global_qty == Decimal("80.0000")
    else:
        # 调拨先：DEFAULT 100-30=70，盘点整单拒绝；全局 100
        assert Decimal(rows[DEFAULT_LOCATION_CODE]) == Decimal("70.0000")
        assert global_qty == Decimal("100.0000")


def test_concurrent_stock_check_and_production_consumption_never_overuse(engine):
    """盘点（DEFAULT 100->95）与指定库位投料（DEFAULT 消耗 20）并发：
    两种串行结局都合法，但绝不超扣、两层恒等。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def check_worker():
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, _check_in("SC-1", "RM-1", "100", "95"))
        results.append(("check",) + _guard(action))

    def production_worker():
        barrier.wait()
        def action(s):
            return service.submit_production(s, ProductionIn(
                output_batch_no="FG-1", unit="kg", output_quantity=_qty("20"),
                inputs=[{"batch_no": "RM-1", "quantity": _qty("20"),
                         "location_code": DEFAULT_LOCATION_CODE}],
            ))
        results.append(("prod",) + _guard(action))

    _run([check_worker, production_worker])
    by_kind = {kind: code for kind, code, _ in results}
    assert by_kind["prod"] == "ok", results
    assert by_kind["check"] in ("ok", "STOCK_CHECK_BOOK_CHANGED"), results
    global_qty, loc_sum = _totals()
    assert global_qty == loc_sum
    with session_scope() as s:
        default_qty = Decimal(s.execute(
            text("SELECT qty FROM batch_locations WHERE batch_no='RM-1' AND location_code=:c"),
            {"c": DEFAULT_LOCATION_CODE},
        ).scalar_one())
        fg_qty = Decimal(s.get(Batch, "FG-1").remaining_qty)
    assert fg_qty == Decimal("20.0000")
    assert default_qty >= 0
    if by_kind["check"] == "ok":
        # 盘点先扣 5（95），投料再扣 20（75）；或投料先扣20（80），盘点账面100不符
        assert default_qty == Decimal("75.0000")
        assert global_qty == Decimal("75.0000")
    else:
        assert default_qty == Decimal("80.0000")
        assert global_qty == Decimal("80.0000")


def test_concurrent_stock_check_and_split_never_overuse(engine):
    """盘点（DEFAULT 100->90）与分装（父批次扣 40 到子批次，默认库位）并发。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def check_worker():
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, _check_in("SC-1", "RM-1", "100", "90"))
        results.append(("check",) + _guard(action))

    def split_worker():
        barrier.wait()
        def action(s):
            return service.split_batch(s, "RM-1", SplitIn(
                splits=[{"batch_no": "RM-1-A", "quantity": _qty("40")}],
            ))
        results.append(("split",) + _guard(action))

    _run([check_worker, split_worker])
    by_kind = {kind: code for kind, code, _ in results}
    assert by_kind["split"] == "ok", results
    assert by_kind["check"] in ("ok", "STOCK_CHECK_BOOK_CHANGED"), results
    parent_global, parent_loc = _totals("RM-1")
    assert parent_global == parent_loc
    child_global, child_loc = _totals("RM-1-A")
    assert child_global == child_loc == Decimal("40.0000")
    if by_kind["check"] == "ok":
        assert parent_global == Decimal("50.0000")  # 100-10 盘亏-40 分装
    else:
        assert parent_global == Decimal("60.0000")  # 100-40 分装


def test_concurrent_stock_check_and_cancel_serializable(engine):
    """盘亏核销产出与撤销该产出单并发，只有两种合法串行结局：
    - 盘点先：盘点成功，撤销随后以 OUTPUT_HAS_STOCK_LOSS 拒绝；
    - 撤销先：撤销成功（返还来源、产出作废但余额行保留），盘点随后
      仍可核销已撤销产出（规格允许）并成功。
    不会出现“撤销被拒且库存也未盘亏”之类矛盾，两层余额恒等。"""
    _register(qty="100")
    # 先建一笔生产 RM-1 -20-> FG-1(20)
    with session_scope() as s:
        service.submit_production(s, ProductionIn(
            output_batch_no="FG-1", unit="kg", output_quantity=_qty("20"),
            inputs=[{"batch_no": "RM-1", "quantity": _qty("20")}],
        ))

    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def check_worker():
        barrier.wait()
        def action(s):
            # FG-1 默认库位账面 20，实点 5，盘亏 15
            return service.submit_stock_check(s, _check_in("SC-1", "FG-1", "20", "5"))
        code, _ = _guard(action)
        results.append(("check", code))

    def cancel_worker():
        barrier.wait()
        from app.schemas import CancelIn
        def action(s):
            return service.cancel_production(s, "FG-1", CancelIn(cancel_reason="录入错误"))
        code, _ = _guard(action)
        results.append(("cancel", code))

    _run([check_worker, cancel_worker])
    by_kind = {kind: code for kind, code in results}
    assert by_kind["check"] == "ok", results  # 两种结局下盘点都成立
    assert by_kind["cancel"] in ("ok", "OUTPUT_HAS_STOCK_LOSS"), results
    fg_global, fg_loc = _totals("FG-1")
    assert fg_global == fg_loc == Decimal("5.0000")  # 盘亏必然生效
    rm_global, rm_loc = _totals("RM-1")
    assert rm_global == rm_loc
    if by_kind["cancel"] == "OUTPUT_HAS_STOCK_LOSS":
        # 盘点先：撤销被拒，来源不返还
        assert rm_global == Decimal("80.0000")
        with session_scope() as s:
            assert service.get_production_record(s, "FG-1").is_cancelled is False
    else:
        # 撤销先：来源已返还，再盘点已撤销产出
        assert rm_global == Decimal("100.0000")
        with session_scope() as s:
            assert service.get_production_record(s, "FG-1").is_cancelled is True


def test_concurrent_identical_stock_check_no_exactly_one_wins(engine):
    """相同盘点单号并发提交：唯一约束兜底，恰好一笔成功，另一笔单号复用拒绝，
    库存只扣一次。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, _check_in("SC-SAME", "RM-1", "100", "90"))
        results.append(_guard(action)[0])

    _run([worker, worker])
    assert results.count("ok") == 1, results
    assert "STOCK_CHECK_NO_REUSED" in results, results
    global_qty, loc_sum = _totals()
    assert global_qty == Decimal("90.0000") == loc_sum


def test_concurrent_stock_check_multi_batch_keeps_all_invariants(engine):
    """一张盘点单覆盖两个批次，与跨批次调拨并发：所有批次两层恒等、无负余额。"""
    _register("RM-1", "100")
    _register("RM-2", "100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def check_worker():
        barrier.wait()
        def action(s):
            return service.submit_stock_check(s, StockCheckIn(
                stock_check_no="SC-1",
                lines=[
                    StockCheckLineIn(batch_no="RM-1", location_code=DEFAULT_LOCATION_CODE,
                                     book_qty=_qty("100"), actual_qty=_qty("90")),
                    StockCheckLineIn(batch_no="RM-2", location_code=DEFAULT_LOCATION_CODE,
                                     book_qty=_qty("100"), actual_qty=_qty("95")),
                ],
            ))
        results.append(("check",) + _guard(action))

    def transfer_worker():
        barrier.wait()
        def action(s):
            return service.transfer_stock(s, TransferIn(
                transfer_no="T1", batch_no="RM-2",
                source_location=DEFAULT_LOCATION_CODE, target_location="B",
                quantity=_qty("40")))
        results.append(("transfer",) + _guard(action))

    _run([check_worker, transfer_worker])
    by_kind = {kind: code for kind, code, _ in results}
    assert by_kind["transfer"] == "ok", results
    assert by_kind["check"] in ("ok", "STOCK_CHECK_BOOK_CHANGED"), results
    for batch in ("RM-1", "RM-2"):
        g, loc = _totals(batch)
        assert g == loc
        assert g >= 0
    # RM-2 调拨必然发生：B=40；若盘点也成功，DEFAULT 再 -5
    with session_scope() as s:
        b_qty = Decimal(s.execute(
            text("SELECT qty FROM batch_locations WHERE batch_no='RM-2' AND location_code='B'")
        ).scalar_one())
    assert b_qty == Decimal("40.0000")
