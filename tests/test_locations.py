"""库位库存：默认库位、库位余额查询、调拨、按库位投料/分装、撤销返还、隔离。"""
from __future__ import annotations

from decimal import Decimal

from app import db, service
from app.db import session_scope
from app.errors import AppError
from app.models import DEFAULT_LOCATION_CODE, Batch, BatchLocation


# ------------------------------------------------------------ 辅助


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty), "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    total = sum((Decimal(str(q)) for _, q, _ in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [
                {"batch_no": b, "quantity": str(q), **(
                    {"location_code": loc} if loc is not None else {})}
                for b, q, loc in inputs
            ],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
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


def _locations(client, batch_no):
    return client.get(f"/batches/{batch_no}/locations")


def _loc_map(client, batch_no):
    body = _locations(client, batch_no).json()
    return {row["location_code"]: Decimal(str(row["quantity"]))
            for row in body["locations"]}, Decimal(str(body["remaining_qty"]))


# ------------------------------------------------------------ 默认库位与查询


def test_registered_batch_starts_in_default_location(client):
    r = _raw(client, "RM-1", "100")
    assert r.status_code == 201, r.text
    r = _locations(client, "RM-1")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "batch_no": "RM-1",
        "unit": "kg",
        "remaining_qty": 100.0,
        "locations": [{"location_code": DEFAULT_LOCATION_CODE, "quantity": 100.0}],
    }
    # 全局余量字段含义不变
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_locations_query_unknown_batch_404(client):
    assert _locations(client, "GHOST").status_code == 404


def test_locations_sum_equals_global_after_all_ops(client):
    """两层余额恒等式：各库位之和 == 全局余量。"""
    _raw(client, "RM-1", "100")
    assert _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30").status_code == 201
    assert _transfer(client, "T2", "RM-1", "A", "B", "10").status_code == 201
    # 未指定库位投料 50（A=20,B=10,DEFAULT=70 -> 顺序 DEFAULT 先）
    r = _produce(client, "FG-1", [("RM-1", "50", None)])
    assert r.status_code == 201, r.text
    locs, global_qty = _loc_map(client, "RM-1")
    assert sum(locs.values()) == global_qty == Decimal("50")
    # 产出在默认库位
    locs_out, out_global = _loc_map(client, "FG-1")
    assert locs_out == {DEFAULT_LOCATION_CODE: Decimal("50")}
    assert sum(locs_out.values()) == out_global == Decimal("50")
    # 撤销后仍相等
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text
    locs, global_qty = _loc_map(client, "RM-1")
    assert sum(locs.values()) == global_qty == Decimal("100")


# ------------------------------------------------------------ 调拨成功路径


def test_transfer_moves_quantity_and_returns_two_balances(client):
    _raw(client, "RM-1", "100")
    r = _transfer(client, "TR-1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["transfer_no"] == "TR-1"
    assert body["batch_no"] == "RM-1"
    assert body["source_location"] == DEFAULT_LOCATION_CODE
    assert body["target_location"] == "A"
    assert body["quantity"] == 30.0
    assert body["source_balance"] == 70.0
    assert body["target_balance"] == 30.0
    assert body["remaining_qty"] == 100.0  # 全局余量不变
    # locations 按库位编号排序（字典序：A 在 DEFAULT 之前）
    assert body["locations"] == [
        {"location_code": "A", "quantity": 30.0},
        {"location_code": DEFAULT_LOCATION_CODE, "quantity": 70.0},
    ]
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("30")}
    assert sum(locs.values()) == global_qty


def test_transfer_creates_new_location_on_first_move(client):
    _raw(client, "RM-1", "100")
    r = _transfer(client, "TR-1", "RM-1", DEFAULT_LOCATION_CODE, "NEW-LOC", "25")
    assert r.status_code == 201
    assert r.json()["target_balance"] == 25.0
    r = _transfer(client, "TR-2", "RM-1", "NEW-LOC", "ANOTHER", "20")
    assert r.status_code == 201
    body = r.json()
    # NEW-LOC 25-20=5，ANOTHER=20
    assert body["source_balance"] == 5.0
    assert body["target_balance"] == 20.0


def test_transfer_keeps_global_balance_unchanged(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    _transfer(client, "TR-2", "RM-1", "A", "B", "30")
    _transfer(client, "TR-3", "RM-1", DEFAULT_LOCATION_CODE, "A", "40")
    locs, global_qty = _loc_map(client, "RM-1")
    assert global_qty == Decimal("100")
    assert locs == {
        DEFAULT_LOCATION_CODE: Decimal("30"),
        "A": Decimal("40"),
        "B": Decimal("30"),
    }


def test_transfer_allowed_for_quarantined_batch(client):
    """隔离批次不得投料/分装，但可调拨。"""
    _raw(client, "RM-1", "100")
    r = client.post(
        "/contamination",
        json={"batch_no": "RM-1", "note": "疑似"},
        headers={"Idempotency-Key": "flag-1"},
    )
    assert r.status_code == 200
    r = _transfer(client, "TR-1", "RM-1", DEFAULT_LOCATION_CODE, "A", "10")
    assert r.status_code == 201, r.text
    assert r.json()["source_balance"] == 90.0
    assert r.json()["target_balance"] == 10.0
    # 投料/分装仍被拒绝
    r = _produce(client, "FG-1", [("RM-1", "1", "A")])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "C-1", "quantity": "1"}],
              "location_code": "A"},
        headers={"Idempotency-Key": "sp-1"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"


def test_transfer_allowed_for_cancelled_output(client):
    """已撤销产出批次不能投料，但库存仍可调拨。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200
    r = _transfer(client, "TR-1", "MIX-1", DEFAULT_LOCATION_CODE, "A", "5")
    assert r.status_code == 201, r.text
    assert r.json()["target_balance"] == 5.0
    # 投料仍被拒绝
    r = _produce(client, "FG-9", [("MIX-1", "1", "A")])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CANCELLED"


# ------------------------------------------------------------ 调拨拒绝（整单、余额不变）


def test_transfer_blank_location_rejected(client):
    _raw(client, "RM-1", "100")
    # 来源/目标为空（空串或纯空白）
    for src, dst in [("", "A"), ("   ", "A"), ("A", ""), ("A", "   ")]:
        r = client.post(
            "/transfers",
            json={"transfer_no": f"X-{src}-{dst}", "batch_no": "RM-1",
                  "source_location": src, "target_location": dst, "quantity": "1"},
            headers={"Idempotency-Key": f"k-{src}-{dst}"},
        )
        assert r.status_code == 422, (src, dst, r.text)
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("100")}
    assert global_qty == Decimal("100")


def test_transfer_same_location_rejected(client):
    _raw(client, "RM-1", "100")
    r = _transfer(client, "TR-1", "RM-1", "A", "A", "10")
    assert r.status_code == 422
    # 入参层拦截：请求校验错误（库位相同）
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 默认库位对默认库位同样拒绝
    r = _transfer(client, "TR-2", "RM-1", DEFAULT_LOCATION_CODE,
                  DEFAULT_LOCATION_CODE, "10")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 两端空白归一化后相同（" A " 与 "A"）同样拒绝
    r = client.post(
        "/transfers",
        json={"transfer_no": "TR-3", "batch_no": "RM-1",
              "source_location": " A ", "target_location": "A", "quantity": "10"},
        headers={"Idempotency-Key": "k3"},
    )
    assert r.status_code == 422
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("100")}
    assert global_qty == Decimal("100")


def test_transfer_location_code_whitespace_is_normalized(client):
    _raw(client, "RM-1", "100")
    # 库位编号两端空白会被规范化（"  A-01 " -> "A-01"）
    r = client.post(
        "/transfers",
        json={"transfer_no": "TR-4", "batch_no": "RM-1",
              "source_location": f" {DEFAULT_LOCATION_CODE} ",
              "target_location": "  A-01  ", "quantity": "10"},
        headers={"Idempotency-Key": "k4"},
    )
    assert r.status_code == 201, r.text
    assert {x["location_code"] for x in r.json()["locations"]} == {
        DEFAULT_LOCATION_CODE, "A-01"}
    # 后续以规范化后的编号查询/调拨均可命中同一库位
    r2 = client.post(
        "/transfers",
        json={"transfer_no": "TR-5", "batch_no": "RM-1",
              "source_location": "A-01", "target_location": "B", "quantity": "3"},
        headers={"Idempotency-Key": "k5"},
    )
    assert r2.status_code == 201, r2.text


def test_transfer_unknown_batch_rejected(client):
    r = _transfer(client, "TR-1", "GHOST", DEFAULT_LOCATION_CODE, "A", "10")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_transfer_unknown_source_location_rejected(client):
    _raw(client, "RM-1", "100")
    r = _transfer(client, "TR-1", "RM-1", "NOPE", "A", "10")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "LOCATION_NOT_FOUND"
    assert body["error"]["details"]["location_code"] == "NOPE"
    # 目标库位不应被创建，任何余额不变
    locs, _ = _loc_map(client, "RM-1")
    assert set(locs) == {DEFAULT_LOCATION_CODE}


def test_transfer_insufficient_source_rejected_entirely(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T0", "RM-1", DEFAULT_LOCATION_CODE, "A", "10")
    before, _ = _loc_map(client, "RM-1")
    r = _transfer(client, "TR-1", "RM-1", "A", "B", "11")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert body["error"]["details"]["location_code"] == "A"
    assert body["error"]["details"]["requested"] == "11"
    after, global_qty = _loc_map(client, "RM-1")
    assert before == after  # 两库位余额均不变
    assert "B" not in after  # 失败单据不创建目标库位
    assert sum(after.values()) == global_qty


def test_transfer_non_positive_qty_rejected(client):
    _raw(client, "RM-1", "100")
    for q in ("0", "-5"):
        r = client.post(
            "/transfers",
            json={"transfer_no": f"T-{q}", "batch_no": "RM-1",
                  "source_location": DEFAULT_LOCATION_CODE,
                  "target_location": "A", "quantity": q},
            headers={"Idempotency-Key": f"k-{q}"},
        )
        assert r.status_code == 422


def test_transfer_no_reuse_rejected(client):
    _raw(client, "RM-1", "100")
    r1 = _transfer(client, "TR-1", "RM-1", DEFAULT_LOCATION_CODE, "A", "10")
    assert r1.status_code == 201
    # 同单号换内容（含换批次/库位/数量）
    r2 = _transfer(client, "TR-1", "RM-1", "A", "B", "3", key="another-key")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "TRANSFER_NO_REUSED"
    # 同单号完全相同内容但换幂等键：单号唯一仍拒绝
    r3 = _transfer(client, "TR-1", "RM-1", DEFAULT_LOCATION_CODE, "A", "10",
                   key="yet-another-key")
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "TRANSFER_NO_REUSED"
    locs, _ = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("90"), "A": Decimal("10")}


def test_transfer_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/transfers",
        json={"transfer_no": "T1", "batch_no": "RM-1",
              "source_location": DEFAULT_LOCATION_CODE,
              "target_location": "A", "quantity": "1"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_transfer_idempotent_replay_returns_first_result(client):
    _raw(client, "RM-1", "100")
    payload = {"transfer_no": "TR-1", "batch_no": "RM-1",
               "source_location": DEFAULT_LOCATION_CODE,
               "target_location": "A", "quantity": "10"}
    r1 = client.post("/transfers", json=payload,
                     headers={"Idempotency-Key": "k1"})
    r2 = client.post("/transfers", json=payload,
                     headers={"Idempotency-Key": "k1"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    locs, _ = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("90"), "A": Decimal("10")}


def test_transfer_idempotency_conflict_on_different_body(client):
    _raw(client, "RM-1", "100")
    r1 = client.post(
        "/transfers",
        json={"transfer_no": "TR-1", "batch_no": "RM-1",
              "source_location": DEFAULT_LOCATION_CODE,
              "target_location": "A", "quantity": "10"},
        headers={"Idempotency-Key": "shared"},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/transfers",
        json={"transfer_no": "TR-2", "batch_no": "RM-1",
              "source_location": DEFAULT_LOCATION_CODE,
              "target_location": "A", "quantity": "20"},
        headers={"Idempotency-Key": "shared"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 第二笔未生效
    locs, _ = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("90"), "A": Decimal("10")}


# ------------------------------------------------------------ 按库位投料


def test_production_with_location_consumes_only_that_location(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = _produce(client, "FG-1", [("RM-1", "20", "A")], out_qty=20)
    assert r.status_code == 201, r.text
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("10")}
    assert global_qty == Decimal("80")
    # 产出进入默认库位
    assert _loc_map(client, "FG-1")[0] == {DEFAULT_LOCATION_CODE: Decimal("20")}


def test_production_with_location_insufficient_rejected_entirely(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    # A 只有 30 却请求 40：即使全局余量 100 也整笔拒绝
    r = _produce(client, "FG-X", [("RM-1", "40", "A"), ("RM-2", "10", None)])
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert body["error"]["details"]["location_code"] == "A"
    # 全部余额不变，产出未创建
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("30")}
    assert _loc_map(client, "RM-2")[0] == {DEFAULT_LOCATION_CODE: Decimal("100")}
    assert client.get("/batches/FG-X").status_code == 404


def test_production_with_missing_location_rejected_entirely(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    r = _produce(client, "FG-X", [("RM-1", "40", "NOPE"),
                                  ("RM-2", "10", None)])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LOCATION_NOT_FOUND"
    assert _loc_map(client, "RM-1")[0] == {DEFAULT_LOCATION_CODE: Decimal("100")}
    assert _loc_map(client, "RM-2")[0] == {DEFAULT_LOCATION_CODE: Decimal("100")}
    assert client.get("/batches/FG-X").status_code == 404


def test_production_without_location_deducts_in_location_order(client):
    """未指定库位：按库位编号顺序扣减。"""
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    _transfer(client, "T2", "RM-1", DEFAULT_LOCATION_CODE, "B", "20")
    # DEFAULT=50, A=30, B=20；消耗 65 -> DEFAULT 50 + A 15
    r = _produce(client, "FG-1", [("RM-1", "65", None)], out_qty=65)
    assert r.status_code == 201, r.text
    # 库位编号字典序 A < B < DEFAULT：A 30 + B 20 + DEFAULT 15
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {
        DEFAULT_LOCATION_CODE: Decimal("35"),
        "A": Decimal("0"),
        "B": Decimal("0"),
    }
    assert global_qty == Decimal("35")


def test_production_without_location_exhausts_multiple_locations(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    _transfer(client, "T2", "RM-1", DEFAULT_LOCATION_CODE, "B", "20")
    # 全部消耗 100：DEFAULT 50 -> A 30 -> B 20
    r = _produce(client, "FG-1", [("RM-1", "100", None)], out_qty=100)
    assert r.status_code == 201, r.text
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {
        DEFAULT_LOCATION_CODE: Decimal("0"),
        "A": Decimal("0"),
        "B": Decimal("0"),
    }
    assert global_qty == 0


def test_production_mixed_location_inputs(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = _produce(
        client, "FG-1",
        [("RM-1", "30", "A"), ("RM-2", "20", None)],
        out_qty=50,
    )
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("0")}
    assert _loc_map(client, "RM-2")[0] == {DEFAULT_LOCATION_CODE: Decimal("80")}
    assert _loc_map(client, "FG-1")[0] == {DEFAULT_LOCATION_CODE: Decimal("50")}


# ------------------------------------------------------------ 按库位分装


def test_split_with_location_consumes_only_that_location(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "RM-1-S1", "quantity": "20"}],
              "location_code": "A"},
        headers={"Idempotency-Key": "sp1"},
    )
    assert r.status_code == 201, r.text
    # 父批次只扣 A 库位
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("10")}
    # 子批次进入默认库位
    assert _loc_map(client, "RM-1-S1")[0] == {DEFAULT_LOCATION_CODE: Decimal("20")}


def test_split_without_location_deducts_in_order(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [
            {"batch_no": "RM-1-S1", "quantity": "40"},
            {"batch_no": "RM-1-S2", "quantity": "30"},
        ]},
        headers={"Idempotency-Key": "sp1"},
    )
    assert r.status_code == 201, r.text
    # 总装出 70，库位编号字典序 A(30) 先、再 DEFAULT(40)
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("30"), "A": Decimal("0")}


def test_split_location_insufficient_rejected_entirely(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "RM-1-S1", "quantity": "31"}],
              "location_code": "A"},
        headers={"Idempotency-Key": "sp1"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert body["error"]["details"]["location_code"] == "A"
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("30")}
    assert client.get("/batches/RM-1-S1").status_code == 404


def test_split_missing_location_404(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "RM-1-S1", "quantity": "1"}],
              "location_code": "NOPE"},
        headers={"Idempotency-Key": "sp1"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LOCATION_NOT_FOUND"


# ------------------------------------------------------------ 撤销按原消耗库位返还


def test_cancel_returns_to_original_consumed_locations(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    _transfer(client, "T2", "RM-1", DEFAULT_LOCATION_CODE, "B", "20")
    # 消耗 85，库位编号字典序 A(30) -> B(20) -> DEFAULT(35)
    _produce(client, "FG-1", [("RM-1", "85", None)], out_qty=85)
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("15"),
        "A": Decimal("0"),
        "B": Decimal("0"),
    }
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text
    # 严格返还到原消耗库位（A 30、B 20、DEFAULT 35），而不是统一回默认库位
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("50"),
        "A": Decimal("30"),
        "B": Decimal("20"),
    }


def test_cancel_returns_to_specified_location(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    _produce(client, "FG-1", [("RM-1", "20", "A")], out_qty=20)
    # 撤销前把别处的 5 单位调拨进 A（模拟撤销前库存继续流动）
    assert _transfer(client, "T2", "RM-1", DEFAULT_LOCATION_CODE, "A", "5").status_code == 201
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200
    # A: 30-20+5+20(返还) = 35；DEFAULT: 70-5 = 65
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("65"), "A": Decimal("35")}


# ------------------------------------------------------------ 服务层直接验证（回填）


def test_backfill_existing_batch_and_inputs(engine):
    """旧库升级：既有批次与历史投料回填默认库位。"""
    from sqlalchemy import text

    from app.schemas import RawBatchIn

    # 以服务层造一个批次，再删掉它的库位行，模拟库位层上线前的旧库
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity=Decimal("100"))
        )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM batch_locations"))

    db._backfill_locations(engine)

    with session_scope() as s:
        rows = s.execute(
            text("SELECT location_code, qty FROM batch_locations ORDER BY location_code")
        ).all()
        assert [(r[0], r[1]) for r in rows] == [(DEFAULT_LOCATION_CODE, Decimal("100.0000"))]
