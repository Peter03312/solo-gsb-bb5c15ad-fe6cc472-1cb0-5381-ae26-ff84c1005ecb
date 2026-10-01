"""生产单撤销（作废）：返还来源投料、作废产出/投料边、拒绝规则与幂等。"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty), "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    from decimal import Decimal

    total = sum((Decimal(str(q)) for _, q in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [{"batch_no": b, "quantity": str(q)} for b, q in inputs],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _cancel(client, output_no, reason, key):
    return client.post(
        f"/productions/{output_no}/cancellation",
        json={"cancel_reason": reason},
        headers={"Idempotency-Key": key},
    )


def _flag(client, batch_no, key, note=None):
    return client.post(
        "/contamination",
        json={"batch_no": batch_no, "note": note},
        headers={"Idempotency-Key": key},
    )


def _review(client, batch_no, note, key):
    return client.post(
        "/reviews",
        json={"batch_no": batch_no, "review_note": note},
        headers={"Idempotency-Key": key},
    )


def _build_mix(client):
    _raw(client, "RM-A", 100)
    _raw(client, "RM-B", 50)
    r = _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "20")], out_qty=55)
    assert r.status_code == 201, r.text
    return r


# --------------------------------------------------------------- 撤销主流程


def test_cancel_returns_inputs_and_marks_cancelled(client):
    _build_mix(client)
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 30.0

    r = _cancel(client, "MIX-1", "产出编号录入错误，应为 MIX-1B", "c1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output_batch_no"] == "MIX-1"
    assert body["status"] == "CANCELLED"
    assert body["cancel_reason"] == "产出编号录入错误，应为 MIX-1B"
    assert body["cancelled_at"]
    returned = {x["batch_no"]: x["returned_quantity"] for x in body["returned_inputs"]}
    assert returned == {"RM-A": 40.0, "RM-B": 20.0}

    # 同一事务内返还各来源原投料量
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 50.0


def test_cancelled_batch_is_retained_and_marked(client):
    _build_mix(client)
    _cancel(client, "MIX-1", "录入错误", "c1")
    d = client.get("/batches/MIX-1").json()
    assert d["is_cancelled"] is True
    # 产出批次行保留，编号空间仍被占用
    assert d["remaining_qty"] == 55.0

    # 原单据（含撤销原因/时间）可查询
    rec = client.get("/productions/MIX-1").json()
    assert rec["output_batch_no"] == "MIX-1"
    assert rec["is_cancelled"] is True
    assert rec["cancel_reason"] == "录入错误"
    assert rec["cancelled_at"]
    assert {(x["batch_no"], x["quantity"]) for x in rec["inputs"]} == {
        ("RM-A", 40.0),
        ("RM-B", 20.0),
    }


def test_production_record_queryable_before_and_after_cancel(client):
    _build_mix(client)
    rec = client.get("/productions/MIX-1").json()
    assert rec["is_cancelled"] is False
    assert rec["cancel_reason"] is None and rec["cancelled_at"] is None

    # 原料批次没有生产单
    r = client.get("/productions/RM-A")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"
    # 编号不存在
    r = client.get("/productions/GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_cancelled_number_not_reusable(client):
    _build_mix(client)
    _cancel(client, "MIX-1", "录入错误", "c1")
    r = _produce(client, "MIX-1", [("RM-A", "5")], key="dup")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NO_REUSED"
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_cancelled_output_cannot_be_consumed(client):
    _build_mix(client)
    _cancel(client, "MIX-1", "录入错误", "c1")
    r = _produce(client, "FG-1", [("MIX-1", "5")], key="fg")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CANCELLED"
    assert client.get("/batches/FG-1").status_code == 404


def test_refunded_balance_is_reusable(client):
    _build_mix(client)
    _cancel(client, "MIX-1", "录入错误", "c1")
    # 返还后的余量可再次投入生产（编号不得复用 MIX-1，使用新产出编号）
    r = _produce(client, "MIX-9", [("RM-A", "100"), ("RM-B", "50")], key="redo")
    assert r.status_code == 201, r.text
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 0.0
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 0.0


# --------------------------------------------------------------- 谱系与传播


def test_cancelled_edges_removed_from_lineage(client):
    # RM-A/RM-B -> MIX-1 -> FG-1（保留的有效链）；RM-A 另有从未被下游投料的 MIX-S
    _raw(client, "RM-A", 100)
    _raw(client, "RM-B", 100)
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "20")], key="m1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="fg")
    _produce(client, "MIX-S", [("RM-A", "5")], key="ms")
    down = {n["batch_no"] for n in client.get("/batches/RM-A/lineage").json()["downstream"]}
    assert down == {"MIX-1", "FG-1", "MIX-S"}

    # MIX-S 从未被后续投料，可撤销；其边 RM-A -> MIX-S 从有效谱系消失
    assert _cancel(client, "MIX-S", "录入错误", "cs").status_code == 200

    lin = client.get("/batches/RM-A/lineage").json()
    assert {n["batch_no"] for n in lin["downstream"]} == {"MIX-1", "FG-1"}
    assert {(e["from"], e["to"]) for e in lin["edges"]} == {
        ("RM-A", "MIX-1"),
        ("MIX-1", "FG-1"),
    }
    assert lin["is_cancelled"] is False  # RM-A 是原料批次

    lin_mix = client.get("/batches/MIX-S/lineage").json()
    assert lin_mix["is_cancelled"] is True
    # 撤销边后 MIX-S 与 RM-A 之间不再有有效上游
    assert lin_mix["upstream"] == []
    assert lin_mix["edges"] == []


def test_contamination_does_not_propagate_through_cancelled_edge(client):
    _raw(client, "RM-A", 100)
    _produce(client, "MIX-1", [("RM-A", "40")], key="m")
    _cancel(client, "MIX-1", "录入错误", "c1")

    r = _flag(client, "RM-A", "g1")
    assert r.status_code == 200
    assert r.json()["affected_batch_nos"] == []
    d = client.get("/batches/MIX-1").json()
    assert d["is_quarantined"] is False and d["is_suspect"] is False


def test_cancel_releases_inherited_quarantine_but_keeps_source_quarantine(client):
    _raw(client, "RM-A", 100)
    _raw(client, "RM-B", 90)
    _produce(client, "MIX-1", [("RM-A", "30"), ("RM-B", "10")], key="m")
    _flag(client, "RM-A", "g1")
    assert client.get("/batches/MIX-1").json()["is_quarantined"] is True

    # 产出自身非疑似（仅因祖先被连带隔离）：允许撤销
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 200, r.text
    mix = client.get("/batches/MIX-1").json()
    assert mix["is_quarantined"] is False and mix["is_suspect"] is False
    # 疑似来源 RM-A 保持隔离；干净来源 RM-B 不受影响；二者均得到返还
    assert client.get("/batches/RM-A").json()["is_quarantined"] is True
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 90.0


# --------------------------------------------------------------- 拒绝规则


def test_cancel_unknown_number_not_found(client):
    r = _cancel(client, "GHOST", "x", "c1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_cancel_raw_batch_rejected(client):
    _raw(client, "RM-A", 100)
    r = _cancel(client, "RM-A", "误把原料当产出", "c1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"
    # 库存不变
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_cancel_consumed_output_rejected(client):
    _build_mix(client)
    _produce(client, "FG-1", [("MIX-1", "10")], key="fg")
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "OUTPUT_ALREADY_CONSUMED"
    assert err["details"]["consumed_by"] == ["FG-1"]
    # 明确拒绝且不改库存
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False


def test_cancel_consumed_output_still_rejected_after_downstream_cancelled(client):
    _build_mix(client)
    _produce(client, "FG-1", [("MIX-1", "10")], key="fg")
    assert _cancel(client, "FG-1", "下游先撤销", "cf").status_code == 200
    # 撤销的投料边保留记录：MIX-1 仍按“曾被投料”拒绝
    r = _cancel(client, "MIX-1", "再撤销", "cm")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_ALREADY_CONSUMED"


def test_cancel_suspect_output_rejected(client):
    _build_mix(client)
    _flag(client, "MIX-1", "g1")
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_IS_SUSPECT"
    # 不改库存、未撤销
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False


def test_cancel_output_with_review_record_rejected(client):
    _build_mix(client)
    _flag(client, "MIX-1", "g1")
    assert _review(client, "MIX-1", "复检合格，误报", "r1").status_code == 200
    # 已非疑似但留下复核记录：仍拒绝撤销
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRODUCTION_HAS_REVIEW"
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0


def test_duplicate_cancel_rejected(client):
    _build_mix(client)
    assert _cancel(client, "MIX-1", "第一次撤销", "c1").status_code == 200
    r = _cancel(client, "MIX-1", "重复撤销", "c2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRODUCTION_ALREADY_CANCELLED"
    # 只返还一次
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_cancel_blank_reason_rejected(client):
    _build_mix(client)
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "   "},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False


def test_cancel_requires_idempotency_key(client):
    _build_mix(client)
    r = client.post(
        "/productions/MIX-1/cancellation", json={"cancel_reason": "x"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False


# --------------------------------------------------------------- 幂等


def test_cancel_idempotent_replay_returns_first_result(client):
    _build_mix(client)
    payload = {"cancel_reason": "录入错误"}
    r1 = client.post(
        "/productions/MIX-1/cancellation",
        json=payload,
        headers={"Idempotency-Key": "IK1"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/productions/MIX-1/cancellation",
        json=payload,
        headers={"Idempotency-Key": "IK1"},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # 只返还一次
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_cancel_same_key_different_content_conflicts(client):
    _build_mix(client)
    assert client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "原因A"},
        headers={"Idempotency-Key": "IK"},
    ).status_code == 200
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "原因B"},
        headers={"Idempotency-Key": "IK"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    rec = client.get("/productions/MIX-1").json()
    assert rec["cancel_reason"] == "原因A"


def test_failed_cancel_not_stored_same_key_reexecutes(client):
    _build_mix(client)
    payload = {"cancel_reason": "录入错误"}
    # 产出疑似 -> 撤销失败；失败请求不得固化幂等记录
    _flag(client, "MIX-1", "g1")
    r1 = client.post(
        "/productions/MIX-1/cancellation",
        json=payload,
        headers={"Idempotency-Key": "IK"},
    )
    assert r1.status_code == 409 and r1.json()["error"]["code"] == "OUTPUT_IS_SUSPECT"
    # 复核撤销疑似（留下复核记录）后，同键同体重试必须重新执行业务校验：
    # 若首次失败被缓存，会原样返回 OUTPUT_IS_SUSPECT；实际应得到新结论
    assert _review(client, "MIX-1", "误报", "r1").status_code == 200
    r2 = client.post(
        "/productions/MIX-1/cancellation",
        json=payload,
        headers={"Idempotency-Key": "IK"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "PRODUCTION_HAS_REVIEW"
    # 自始未撤销、库存未变
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
