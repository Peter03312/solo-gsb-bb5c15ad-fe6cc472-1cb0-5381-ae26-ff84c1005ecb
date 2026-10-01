"""仓库盘点：多行盘亏提交、账面核对、原子扣减、隔离/撤销产出核销、撤销拦截。"""
from __future__ import annotations

from decimal import Decimal

from app import service
from app.db import session_scope
from app.schemas import ProductionIn, RawBatchIn, StockCheckIn, StockCheckLineIn, TransferIn
from app.models import DEFAULT_LOCATION_CODE


# ------------------------------------------------------------ 辅助


def _qty(v: str) -> Decimal:
    return Decimal(v)


def _line(batch_no: str, book: str, actual: str, location: str = DEFAULT_LOCATION_CODE):
    return {
        "batch_no": batch_no,
        "location_code": location,
        "book_qty": str(book),
        "actual_qty": str(actual),
    }


def _check(client, no, lines, key=None):
    return client.post(
        "/stock-checks",
        json={"stock_check_no": no, "lines": lines},
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty), "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _transfer(client, no, batch, src, dst, qty, key=None):
    return client.post(
        "/transfers",
        json={
            "transfer_no": no, "batch_no": batch,
            "source_location": src, "target_location": dst, "quantity": str(qty),
        },
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    total = sum((Decimal(str(item[1])) for item in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [
                {
                    "batch_no": batch_no,
                    "quantity": str(qty),
                    **({"location_code": loc} if loc is not None else {}),
                }
                for batch_no, qty, loc in inputs
            ],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _loc_map(client, batch_no):
    body = client.get(f"/batches/{batch_no}/locations").json()
    return (
        {row["location_code"]: Decimal(str(row["quantity"])) for row in body["locations"]},
        Decimal(str(body["remaining_qty"])),
    )


# ------------------------------------------------------------ 成功路径


def test_stock_check_posted_response_shape(client):
    _raw(client, "RM-1", "100")
    r = _check(client, "SC-1", [_line("RM-1", "100", "92")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stock_check_no"] == "SC-1"
    assert body["status"] == "POSTED"
    assert isinstance(body["checked_at"], str) and "T" in body["checked_at"]
    assert body["lines"] == [
        {
            "batch_no": "RM-1",
            "location_code": DEFAULT_LOCATION_CODE,
            "book_qty": 100.0,
            "actual_qty": 92.0,
            "loss_qty": 8.0,
            "unit": "kg",
        }
    ]


def test_stock_check_multi_line_multi_batch_multi_location_deducts(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50", unit="L")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")

    r = _check(client, "SC-1", [
        _line("RM-1", "70", "60", DEFAULT_LOCATION_CODE),   # 亏 10
        _line("RM-1", "30", "30", "A"),                    # 平
        _line("RM-2", "50", "45.5", DEFAULT_LOCATION_CODE),  # 亏 4.5，单位 L
    ])
    assert r.status_code == 201, r.text
    units = {ln["batch_no"]: ln["unit"] for ln in r.json()["lines"]}
    assert units == {"RM-1": "kg", "RM-2": "L"}

    locs1, global1 = _loc_map(client, "RM-1")
    assert locs1 == {DEFAULT_LOCATION_CODE: Decimal("60"), "A": Decimal("30")}
    assert sum(locs1.values()) == global1 == Decimal("90")
    locs2, global2 = _loc_map(client, "RM-2")
    assert locs2 == {DEFAULT_LOCATION_CODE: Decimal("45.5")}
    assert sum(locs2.values()) == global2 == Decimal("45.5")


def test_stock_check_full_loss_zero_actual_allowed(client):
    _raw(client, "RM-1", "10")
    r = _check(client, "SC-1", [_line("RM-1", "10", "0")])
    assert r.status_code == 201, r.text
    body = client.get("/batches/RM-1").json()
    assert body["remaining_qty"] == 0.0
    locs, total = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("0")} == {DEFAULT_LOCATION_CODE: total}


def test_stock_check_book_zero_actual_zero_on_zero_balance_location(client):
    _raw(client, "RM-1", "10")
    # 把全部数量移到 A 后，DEFAULT 库位行仍存在且余额为 0
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "10")
    r = _check(client, "SC-1", [
        _line("RM-1", "0", "0", DEFAULT_LOCATION_CODE),
        _line("RM-1", "10", "7", "A"),
    ])
    assert r.status_code == 201, r.text


# ------------------------------------------------------------ 查询


def test_get_stock_check_returns_status_lines_loss_unit(client):
    _raw(client, "RM-1", "100")
    _check(client, "SC-1", [_line("RM-1", "100", "99")])
    r = client.get("/stock-checks/SC-1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "POSTED"
    assert body["lines"] == [
        {
            "batch_no": "RM-1",
            "location_code": DEFAULT_LOCATION_CODE,
            "book_qty": 100.0,
            "actual_qty": 99.0,
            "loss_qty": 1.0,
            "unit": "kg",
        }
    ]


def test_get_stock_check_unknown_404(client):
    r = client.get("/stock-checks/GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "STOCK_CHECK_NOT_FOUND"


def test_stock_check_records_are_immutable_snapshot(client):
    """再次盘点/调拨不改变已固化盘点单原值。"""
    _raw(client, "RM-1", "100")
    _check(client, "SC-1", [_line("RM-1", "100", "90")])
    _check(client, "SC-2", [_line("RM-1", "90", "80")])
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "10")
    body = client.get("/stock-checks/SC-1").json()
    assert body["lines"][0]["book_qty"] == 100.0
    assert body["lines"][0]["actual_qty"] == 90.0
    assert body["lines"][0]["loss_qty"] == 10.0


# ------------------------------------------------------------ 输入/业务校验


def test_stock_check_requires_at_least_one_loss(client):
    _raw(client, "RM-1", "100")
    # 平账行（可与亏损行混合，但整单至少一行 actual < book）
    r = _check(client, "SC-1", [_line("RM-1", "100", "100")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCK_CHECK_NO_LOSS"


def test_stock_check_actual_above_book_rejected_422(client):
    _raw(client, "RM-1", "100")
    r = _check(client, "SC-1", [_line("RM-1", "100", "101")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_stock_check_negative_quantity_rejected_422(client):
    _raw(client, "RM-1", "100")
    for book, actual in (("-1", "0"), ("0", "-0.0001"), ("-5", "-3")):
        r = _check(client, "SC-X", [_line("RM-1", book, actual)], key=f"k-{book}-{actual}")
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_stock_check_empty_lines_rejected_422(client):
    r = client.post(
        "/stock-checks",
        json={"stock_check_no": "SC-1", "lines": []},
        headers={"Idempotency-Key": "k1"},
    )
    assert r.status_code == 422


def test_stock_check_duplicate_line_rejected_422(client):
    _raw(client, "RM-1", "100")
    r = _check(client, "SC-1", [
        _line("RM-1", "60", "50", DEFAULT_LOCATION_CODE),
        _line("RM-1", "40", "39", "A"),
        _line("RM-1", "60", "55", DEFAULT_LOCATION_CODE),  # 与第一行重复
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_STOCK_CHECK_LINE"
    assert err["details"] == {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE}


def test_stock_check_batch_not_found_404(client):
    # 含盘亏行以通过“整单至少一条盘亏”的纯输入校验
    r = _check(client, "SC-1", [
        _line("GHOST", "10", "5"),
        _line("OTHER", "8", "8"),
    ])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_stock_check_location_not_found_404_and_creates_nothing(client):
    _raw(client, "RM-1", "100")
    r = _check(client, "SC-1", [
        _line("RM-1", "10", "5", "NOPE"),
        _line("RM-1", "100", "99", DEFAULT_LOCATION_CODE),
    ])
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "LOCATION_NOT_FOUND"
    assert err["details"] == {"batch_no": "RM-1", "location_code": "NOPE"}
    # 整单拒绝：无盘点记录、余额不变、未创建库位
    assert client.get("/stock-checks/SC-1").status_code == 404
    locs, total = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("100")} == {DEFAULT_LOCATION_CODE: total}
    # 即便申报 NOPE 账面 0（与另一行真实盘亏混合）也不自动建行
    r = _check(client, "SC-2", [
        _line("RM-1", "0", "0", "NOPE"),
        _line("RM-1", "100", "99", DEFAULT_LOCATION_CODE),
    ])
    assert r.status_code == 404
    assert r.json()["error"]["details"]["location_code"] == "NOPE"
    locs, _ = _loc_map(client, "RM-1")
    assert "NOPE" not in locs


def test_stock_check_book_changed_rejected_409_and_atomic(client):
    """账面已变动（含他行已可扣）时整单拒绝：不扣任何库存、不留记录。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    # RM-1 DEFAULT 实存 70 却申报 100；RM-2 行本可盘亏
    r = _check(client, "SC-1", [
        _line("RM-1", "100", "50", DEFAULT_LOCATION_CODE),
        _line("RM-2", "50", "10", DEFAULT_LOCATION_CODE),
    ])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "STOCK_CHECK_BOOK_CHANGED"
    assert err["details"] == {
        "batch_no": "RM-1",
        "location_code": DEFAULT_LOCATION_CODE,
        "book_qty": "100",
        "current_qty": "70.0000",
    }
    assert client.get("/stock-checks/SC-1").status_code == 404
    # 两个批次余额均未变
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert client.get("/batches/RM-2").json()["remaining_qty"] == 50.0


def test_stock_check_no_must_not_be_reused(client):
    _raw(client, "RM-1", "100")
    assert _check(client, "SC-1", [_line("RM-1", "100", "90")]).status_code == 201
    r = _check(client, "SC-1", [_line("RM-1", "90", "80")], key="other-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STOCK_CHECK_NO_REUSED"
    # 原单不受影响
    assert client.get("/stock-checks/SC-1").json()["lines"][0]["actual_qty"] == 90.0


def test_failed_stock_check_leaves_no_record_and_is_retryable(client):
    _raw(client, "RM-1", "100")
    # 失败：账面不符（同键失败不固化）
    r = _check(client, "SC-1", [_line("RM-1", "99", "90")], key="k1")
    assert r.status_code == 409
    assert client.get("/stock-checks/SC-1").status_code == 404
    # 同键可修正后重试成功
    r = _check(client, "SC-1", [_line("RM-1", "100", "90")], key="k1")
    assert r.status_code == 201, r.text


# ------------------------------------------------------------ 幂等


def test_stock_check_idempotent_replay_applies_once(client):
    _raw(client, "RM-1", "100")
    payload = {"stock_check_no": "SC-1", "lines": [_line("RM-1", "100", "90")]}
    r1 = client.post("/stock-checks", json=payload, headers={"Idempotency-Key": "ik1"})
    r2 = client.post("/stock-checks", json=payload, headers={"Idempotency-Key": "ik1"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    # 只扣一次
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0
    locs, _ = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("90")}


def test_stock_check_idempotent_conflict_on_different_body(client):
    _raw(client, "RM-1", "100")
    client.post(
        "/stock-checks",
        json={"stock_check_no": "SC-1", "lines": [_line("RM-1", "100", "90")]},
        headers={"Idempotency-Key": "ik1"},
    )
    r = client.post(
        "/stock-checks",
        json={"stock_check_no": "SC-9", "lines": [_line("RM-1", "90", "89")]},
        headers={"Idempotency-Key": "ik1"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突不写入、不扣减
    assert client.get("/stock-checks/SC-9").status_code == 404
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_stock_check_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post("/stock-checks", json={"stock_check_no": "SC-1", "lines": [_line("RM-1", "100", "90")]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ------------------------------------------------------------ 隔离 / 撤销


def test_quarantined_batch_can_still_be_checked(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/contamination",
        json={"batch_no": "RM-1", "note": "疑似污染"},
        headers={"Idempotency-Key": "cont1"},
    )
    assert r.status_code == 200
    r = _check(client, "SC-1", [_line("RM-1", "100", "40")])
    assert r.status_code == 201, r.text
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 40.0


def test_cancelled_output_can_still_be_checked(client):
    _raw(client, "RM-1", "100")
    assert _produce(client, "FG-1", [("RM-1", "20", None)], out_qty="20").status_code == 201
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200
    # 已撤销产出 FG-1 默认库位仍保留 20，可核销盘亏
    r = _check(client, "SC-1", [_line("FG-1", "20", "0")])
    assert r.status_code == 201, r.text
    assert client.get("/batches/FG-1").json()["remaining_qty"] == 0.0


def test_output_with_stock_loss_cannot_be_cancelled(client):
    _raw(client, "RM-1", "100")
    assert _produce(client, "FG-1", [("RM-1", "20", None)], out_qty="20").status_code == 201
    assert _check(client, "SC-1", [_line("FG-1", "20", "15")]).status_code == 201
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "OUTPUT_HAS_STOCK_LOSS"
    assert err["details"] == {"batch_no": "FG-1", "stock_check_nos": ["SC-1"]}
    # 拒绝撤销：来源不返还、生产单未撤销
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 80.0
    assert client.get("/productions/FG-1").json()["is_cancelled"] is False


def test_output_zero_loss_check_still_allows_cancellation(client):
    """产出只出现在平账行（无盘亏）不阻止撤销。"""
    _raw(client, "RM-1", "100")
    assert _produce(client, "FG-1", [("RM-1", "20", None)], out_qty="20").status_code == 201
    # FG-1 平账；RM-1 承担整单的盘亏行
    assert _check(client, "SC-1", [
        _line("FG-1", "20", "20"),
        _line("RM-1", "80", "79"),
    ]).status_code == 201
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text


def test_source_batch_stock_loss_does_not_block_output_cancellation(client):
    """来源批次发生盘亏不影响其下游产出单撤销。"""
    _raw(client, "RM-1", "100")
    assert _produce(client, "FG-1", [("RM-1", "20", None)], out_qty="20").status_code == 201
    # 来源 RM-1 自身盘亏（不是产出 FG-1）
    assert _check(client, "SC-1", [_line("RM-1", "80", "70")]).status_code == 201
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text
    # 撤销按原投料返还 20（80-10 盘亏 -> 70 + 20 返还 = 90，恒不超过初始 100）
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_stock_check_does_not_change_lineage(client):
    """盘点不产生谱系边，历史投料谱系不改。"""
    _raw(client, "RM-1", "100")
    assert _produce(client, "FG-1", [("RM-1", "20", None)], out_qty="20").status_code == 201
    _check(client, "SC-1", [_line("FG-1", "20", "10")])
    body = client.get("/batches/RM-1/lineage").json()
    assert [e for e in body["edges"]] == [{"from": "RM-1", "to": "FG-1", "kind": "PRODUCTION"}]
    assert [n["batch_no"] for n in body["downstream"]] == ["FG-1"]


# ------------------------------------------------------------ 服务层直测


def test_service_rejects_when_book_changed_directly(engine):
    import pytest
    from app.errors import AppError
    from app.models import Batch, StockCheck

    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity=_qty("100")))
    with session_scope() as s:
        with pytest.raises(AppError) as exc:
            service.submit_stock_check(s, StockCheckIn(
                stock_check_no="SC-1",
                lines=[StockCheckLineIn(
                    batch_no="RM-1", location_code=DEFAULT_LOCATION_CODE,
                    book_qty=_qty("50"), actual_qty=_qty("40"))],
            ))
        assert exc.value.code == "STOCK_CHECK_BOOK_CHANGED"
    # 回滚后无记录、余额不变
    with session_scope() as s:
        assert s.query(StockCheck).count() == 0
        assert s.get(Batch, "RM-1").remaining_qty == _qty("100")
