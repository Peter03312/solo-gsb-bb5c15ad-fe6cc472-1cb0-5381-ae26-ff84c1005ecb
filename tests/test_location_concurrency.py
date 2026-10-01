"""库位并发测试：调拨 / 投料 / 分装 / 撤销并发时绝不超用、两层余额恒等。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import text

from app import db, service
from app.db import session_scope
from app.errors import AppError
from app.models import DEFAULT_LOCATION_CODE, Batch, BatchLocation
from app.schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    SplitIn,
    TransferIn,
)


def _qty(v: str) -> Decimal:
    return Decimal(v)


def _register(no: str = "RM-1", qty: str = "100") -> None:
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no=no, unit="kg", quantity=_qty(qty))
        )


def _location_totals() -> tuple[Decimal, Decimal]:
    """返回 (全局余量, 全部库位余额之和)；测试末尾断言两者相等。"""
    with session_scope() as s:
        global_qty = s.get(Batch, "RM-1").remaining_qty
        loc_sum = s.execute(
            text("SELECT COALESCE(SUM(qty), 0) FROM batch_locations WHERE batch_no='RM-1'")
        ).scalar_one()
        return Decimal(global_qty), Decimal(loc_sum)


def test_concurrent_transfers_out_of_same_location_never_overuse(engine):
    """默认库位余量 100，8 个线程各从默认库位调拨 15 到各自目标库位：
    恰 6 个成功、2 个来源不足拒绝，库位层与全局层均不失配。"""
    _register(qty="100")
    barrier = threading.Barrier(8)
    results: list[str] = []

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(
                        transfer_no=f"TR-{idx}",
                        batch_no="RM-1",
                        source_location=DEFAULT_LOCATION_CODE,
                        target_location=f"L-{idx}",
                        quantity=_qty("15"),
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert results.count("ok") == 6, results
    assert results.count("INSUFFICIENT_BALANCE") == 2, results
    assert not [r for r in results if r.startswith("ERROR")], results

    # 调拨只移动库位分布、不改变全局余量：全局仍为 100；
    # 成功的 6 笔共移出 90（DEFAULT=10，其余 90 分散在 6 个目标库位）。
    global_qty, loc_sum = _location_totals()
    assert global_qty == Decimal("100.0000")
    assert loc_sum == global_qty
    with session_scope() as s:
        default_qty = Decimal(
            s.execute(
                text(
                    "SELECT qty FROM batch_locations "
                    "WHERE batch_no='RM-1' AND location_code=:c"
                ),
                {"c": DEFAULT_LOCATION_CODE},
            ).scalar_one()
        )
    assert default_qty == Decimal("10.0000")


def test_concurrent_transfers_between_same_two_locations(engine):
    """两个方向相反的调拨并发：A→DEFAULT 与 DEFAULT→A，最终余额自洽、
    全局余量不变，绝不出现负余额。"""
    _register(qty="100")
    with session_scope() as s:
        service.transfer_stock(
            s,
            TransferIn(transfer_no="T0", batch_no="RM-1",
                       source_location=DEFAULT_LOCATION_CODE,
                       target_location="A", quantity=_qty("50")),
        )

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(no: str, src: str, dst: str) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(transfer_no=no, batch_no="RM-1",
                               source_location=src, target_location=dst,
                               quantity=_qty("30")),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda f: f(),
            [
                lambda: worker("T1", DEFAULT_LOCATION_CODE, "A"),
                lambda: worker("T2", "A", DEFAULT_LOCATION_CODE),
            ],
        ))

    assert results == ["ok", "ok"], results
    with session_scope() as s:
        rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
            ).all()
        }
    # 起始 DEFAULT=50,A=50；各移动 30，两个方向各成一笔：
    # DEFAULT 50-30+30=50，A 50+30-30=50（串行顺序不影响最终值）
    assert rows == {DEFAULT_LOCATION_CODE: Decimal("50.0000"),
                    "A": Decimal("50.0000")}, rows
    global_qty, loc_sum = _location_totals()
    assert loc_sum == global_qty == Decimal("100.0000")


def test_concurrent_transfer_and_production_same_location(engine):
    """从同一库位并发“调拨 60 出去”与“指定该库位投料 60”（库位共 100）：
    一成一拒，绝不超用、两层余额恒等。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def transfer_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(transfer_no="TR-1", batch_no="RM-1",
                               source_location=DEFAULT_LOCATION_CODE,
                               target_location="A", quantity=_qty("60")),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

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
                        inputs=[{"batch_no": "RM-1",
                                 "quantity": _qty("60"),
                                 "location_code": DEFAULT_LOCATION_CODE}],
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [transfer_worker, produce_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["transfer"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert results["produce"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert results["transfer"] != results["produce"], results  # 一成一拒

    with session_scope() as s:
        rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
            ).all()
        }
        fg1 = s.get(Batch, "FG-1")
        if results["transfer"] == "ok":
            # 调拨先：A=60，DEFAULT=40，投料被拒、FG-1 不存在
            assert rows["A"] == Decimal("60.0000")
            assert rows[DEFAULT_LOCATION_CODE] == Decimal("40.0000")
            assert fg1 is None
        else:
            # 投料先：DEFAULT=40，FG-1 在默认库位 60，调拨被拒、A 不存在
            assert rows[DEFAULT_LOCATION_CODE] == Decimal("40.0000")
            assert "A" not in rows
            assert fg1 is not None
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("40.0000")
        assert sum(rows.values()) == Decimal("40.0000")


def test_concurrent_transfer_and_split_different_locations(engine):
    """并发：DEFAULT→A 调拨 30 与“从 A 库位分装 30”（A 初始 30）。

    结局 A：调拨先提交再分装（若分装在调拨之后执行，A=60 充足，两笔都成功）；
    结局 B：分装先（A=0），调拨仍成功（DEFAULT→A 增加 A），也两笔成功。
    关键：两笔操作针对同一批次但不同库位行，结果必须自洽——
    分装总量只从 A 扣，调拨只在 DEFAULT/A 间移动，两层余额恒等。
    """
    _register(qty="100")
    with session_scope() as s:
        service.transfer_stock(
            s,
            TransferIn(transfer_no="T0", batch_no="RM-1",
                       source_location=DEFAULT_LOCATION_CODE,
                       target_location="A", quantity=_qty("30")),
        )

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def transfer_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(transfer_no="T1", batch_no="RM-1",
                               source_location=DEFAULT_LOCATION_CODE,
                               target_location="A", quantity=_qty("30")),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

    def split_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.split_batch(
                    s,
                    "RM-1",
                    SplitIn(
                        splits=[{"batch_no": "RM-1-S", "quantity": _qty("30")}],
                        location_code="A",
                    ),
                )
            results["split"] = "ok"
        except AppError as exc:
            results["split"] = exc.code
        except Exception as exc:
            results["split"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [transfer_worker, split_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results == {"transfer": "ok", "split": "ok"}, results

    with session_scope() as s:
        parent_rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
            ).all()
        }
        # 起始 DEFAULT=70,A=30；无论串行顺序：A 30+30-30=30（分装扣 30、
        # 调拨入 30），DEFAULT=70-30=40。父批次全局余量 = 70。
        assert parent_rows == {
            DEFAULT_LOCATION_CODE: Decimal("40.0000"),
            "A": Decimal("30.0000"),
        }, parent_rows
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("70.0000")
        assert sum(parent_rows.values()) == Decimal("70.0000")
        # 子批次在默认库位 30
        child_rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations "
                     "WHERE batch_no='RM-1-S'")
            ).all()
        }
        assert child_rows == {DEFAULT_LOCATION_CODE: Decimal("30.0000")}


def test_concurrent_split_from_location_and_production_from_other(engine):
    """并发：从 A 库位分装 60 与从 DEFAULT 库位（编号顺序不同）投料 40，
    两库位独立，均应成功，互不阻塞超额。"""
    _register(qty="100")
    with session_scope() as s:
        service.transfer_stock(
            s,
            TransferIn(transfer_no="T0", batch_no="RM-1",
                       source_location=DEFAULT_LOCATION_CODE,
                       target_location="A", quantity=_qty("60")),
        )
    # 起始 A=60, DEFAULT=40

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
                        splits=[{"batch_no": "RM-1-S", "quantity": _qty("60")}],
                        location_code="A",
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
                        output_quantity=_qty("40"),
                        inputs=[{"batch_no": "RM-1",
                                 "quantity": _qty("40"),
                                 "location_code": DEFAULT_LOCATION_CODE}],
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [split_worker, produce_worker]))

    assert results == {"split": "ok", "produce": "ok"}, results
    with session_scope() as s:
        rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
            ).all()
        }
        assert rows == {
            DEFAULT_LOCATION_CODE: Decimal("0.0000"),
            "A": Decimal("0.0000"),
        }, rows
        assert s.get(Batch, "RM-1").remaining_qty == 0


def test_concurrent_duplicate_transfer_no_only_one_succeeds(engine):
    """同一调拨单号并发：唯一约束兜底，恰好一笔成功。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(transfer_no="TR-DUP", batch_no="RM-1",
                               source_location=DEFAULT_LOCATION_CODE,
                               target_location=f"A{idx}", quantity=_qty("10")),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, range(2)))

    assert results.count("ok") == 1, results
    assert results.count("TRANSFER_NO_REUSED") == 1, results
    global_qty, loc_sum = _location_totals()
    assert loc_sum == global_qty == Decimal("100.0000")


def test_concurrent_cancel_returns_to_original_locations(engine):
    """并发撤销一张“跨库位投料”的生产单：撤销返还到原消耗库位，
    与并发调拨互不矛盾，两层余额恒等。"""
    _register(qty="100")
    with session_scope() as s:
        service.transfer_stock(
            s,
            TransferIn(transfer_no="T0", batch_no="RM-1",
                       source_location=DEFAULT_LOCATION_CODE,
                       target_location="A", quantity=_qty("30")),
        )
        # 未指定库位消耗 60：A 30 + DEFAULT 30
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="FG-1",
                unit="kg",
                output_quantity=_qty("60"),
                inputs=[{"batch_no": "RM-1", "quantity": _qty("60")}],
            ),
        )
    # 撤销前：A=0, DEFAULT=40

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def cancel_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.cancel_production(
                    s, "FG-1", CancelIn(cancel_reason="录入错误")
                )
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    def transfer_worker() -> None:
        # 撤销进行中把 DEFAULT 的 10 移到 A（来源始终存在且余额充足，
        # 无论它在撤销前还是撤销后拿到行锁都成立：撤销后 DEFAULT=70）
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(transfer_no="T1", batch_no="RM-1",
                               source_location=DEFAULT_LOCATION_CODE,
                               target_location="A", quantity=_qty("10")),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            # 撤销先拿到行锁时 DEFAULT=40 仍 >= 10，因此理论恒成功；
            # 若出现来源不足也记录以便断言
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [cancel_worker, transfer_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["cancel"] == "ok", results
    assert results["transfer"] == "ok", results

    with session_scope() as s:
        rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
            ).all()
        }
        global_qty = Decimal(s.get(Batch, "RM-1").remaining_qty)
        # 两种串行结局：
        # 调拨先（DEFAULT 40->30），撤销返还 A+30、DEFAULT+30
        #   -> DEFAULT=60, A=40
        # 撤销先（A=30, DEFAULT=70），调拨 DEFAULT->A 10
        #   -> DEFAULT=60, A=40
        assert rows == {
            DEFAULT_LOCATION_CODE: Decimal("60.0000"),
            "A": Decimal("40.0000"),
        }, rows
        assert sum(rows.values()) == global_qty == Decimal("100.0000")
