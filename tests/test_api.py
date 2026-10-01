"""API 端到端测试：登记、投料、余量、拒绝规则、谱系、污染隔离、幂等。"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": qty, "note": note},
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


def test_register_and_balance(client):
    r = _raw(client, "RM-1", "100")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["remaining_qty"] == 100.0
    assert body["is_quarantined"] is False

    r = client.get("/batches/RM-1")
    assert r.status_code == 200
    assert r.json()["remaining_qty"] == 100.0


def test_batch_no_reuse_rejected(client):
    assert _raw(client, "RM-1", "10").status_code == 201
    r = _raw(client, "RM-1", "20", key="other-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NO_REUSED"
    # 余量未被第二次请求改变
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 10.0


def test_production_consumes_balances(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    r = _produce(client, "FG-1", [("RM-1", "30"), ("RM-2", "20")], out_qty=45)
    assert r.status_code == 201, r.text
    assert r.json() == {
        "output_batch_no": "FG-1",
        "unit": "kg",
        "output_quantity": 45.0,
        "inputs": [
            {"batch_no": "RM-1", "quantity": 30.0},
            {"batch_no": "RM-2", "quantity": 20.0},
        ],
    }
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 70.0
    assert client.get("/batches/RM-2").json()["remaining_qty"] == 30.0
    assert client.get("/batches/FG-1").json()["remaining_qty"] == 45.0


def test_unknown_source_entire_request_rejected(client):
    _raw(client, "RM-1", "100")
    r = _produce(client, "FG-X", [("RM-1", "10"), ("GHOST", "5")])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    # 整笔拒绝：RM-1 余量不变，产出未创建
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert client.get("/batches/FG-X").status_code == 404


def test_insufficient_balance_rejected_without_change(client):
    _raw(client, "RM-1", "10")
    r = _produce(client, "FG-1", [("RM-1", "11")])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 10.0
    assert client.get("/batches/FG-1").status_code == 404


def test_non_positive_qty_rejected(client):
    r = client.post(
        "/raw-batches",
        json={"batch_no": "RM-1", "unit": "kg", "quantity": "0"},
        headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 422
    r = client.post(
        "/raw-batches",
        json={"batch_no": "RM-1", "unit": "kg", "quantity": "-3"},
        headers={"Idempotency-Key": "k2"},
    )
    assert r.status_code == 422


def test_output_cannot_exceed_total_input(client):
    _raw(client, "RM-1", "10")
    _raw(client, "RM-2", "5")
    r = _produce(client, "FG-1", [("RM-1", "10"), ("RM-2", "5")], out_qty=16)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OUTPUT_EXCEEDS_INPUT"
    # 所有余量保持不变
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 10.0
    assert client.get("/batches/RM-2").json()["remaining_qty"] == 5.0
    assert client.get("/batches/FG-1").status_code == 404


def test_loss_allowed_output_lt_input(client):
    _raw(client, "RM-1", "100")
    r = _produce(client, "FG-1", [("RM-1", "100")], out_qty=97)
    assert r.status_code == 201
    assert client.get("/batches/FG-1").json()["remaining_qty"] == 97.0


def test_unit_mismatch_rejected(client):
    _raw(client, "RM-1", "100", unit="kg")
    r = _produce(client, "FG-1", [("RM-1", "10")], unit="L", key="p1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_duplicate_input_in_same_request(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/productions",
        json={
            "output_batch_no": "FG-1",
            "unit": "kg",
            "output_quantity": 20,
            "inputs": [
                {"batch_no": "RM-1", "quantity": 10},
                {"batch_no": "RM-1", "quantity": 10},
            ],
        },
        headers={"Idempotency-Key": "p1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_INPUT"


def test_idempotent_retry_returns_original_result(client):
    payload = {"batch_no": "RM-1", "unit": "kg", "quantity": "100"}
    r1 = client.post("/raw-batches", json=payload, headers={"Idempotency-Key": "K1"})
    assert r1.status_code == 201
    # 同键同内容重试：返回原结果（同为 201），只建了一个批次
    r2 = client.post("/raw-batches", json=payload, headers={"Idempotency-Key": "K1"})
    assert r2.status_code == r1.status_code
    assert r2.json() == r1.json()

    _produce(client, "FG-1", [("RM-1", "40")], key="P1")
    body = {
        "output_batch_no": "FG-2",
        "unit": "kg",
        "output_quantity": 20,
        "inputs": [{"batch_no": "RM-1", "quantity": 20}],
    }
    r3 = client.post("/productions", json=body, headers={"Idempotency-Key": "P2"})
    r4 = client.post("/productions", json=body, headers={"Idempotency-Key": "P2"})
    assert r3.status_code == 201 and r4.status_code == 201
    assert r3.json() == r4.json()
    # 重试没有重复扣减
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 40.0


def test_same_key_different_content_conflicts(client):
    _raw(client, "RM-1", "100")
    body1 = {"batch_no": "RM-X", "unit": "kg", "quantity": "10"}
    body2 = {"batch_no": "RM-Y", "unit": "kg", "quantity": "10"}
    assert client.post(
        "/raw-batches", json=body1, headers={"Idempotency-Key": "K9"}
    ).status_code == 201
    r = client.post("/raw-batches", json=body2, headers={"Idempotency-Key": "K9"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突请求没有副作用
    assert client.get("/batches/RM-Y").status_code == 404


def test_idempotency_key_required(client):
    r = client.post(
        "/raw-batches", json={"batch_no": "RM-1", "unit": "kg", "quantity": "1"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_lineage_multi_level_and_edges(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "60")], key="1")
    _produce(client, "MIX-2", [("RM-A", "10")], out_qty=9, key="2")
    _produce(client, "FG-1", [("MIX-1", "30"), ("MIX-2", "9")], key="3")

    lin = client.get("/batches/RM-A/lineage").json()
    down_nos = {n["batch_no"] for n in lin["downstream"]}
    assert down_nos == {"MIX-1", "MIX-2", "FG-1"}
    fg = next(n for n in lin["downstream"] if n["batch_no"] == "FG-1")
    assert fg["depth"] == 2

    up = client.get("/batches/FG-1/lineage").json()
    up_nos = {n["batch_no"] for n in up["upstream"]}
    assert up_nos == {"MIX-1", "MIX-2", "RM-A", "RM-B"}

    edges = {(e["from"], e["to"]) for e in up["edges"]}
    assert ("RM-A", "MIX-1") in edges
    assert ("RM-B", "MIX-1") in edges
    assert ("MIX-1", "FG-1") in edges
    assert ("MIX-2", "FG-1") in edges


def test_contamination_quarantines_all_descendants(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "60")], key="1")
    _produce(client, "FG-1", [("MIX-1", "50")], key="2")
    # 与污染源无关的批次不受影响
    _produce(client, "FG-CLEAN", [("RM-B", "10")], key="3")

    r = client.post(
        "/contamination",
        json={"batch_no": "RM-A", "note": "抽检异常"},
        headers={"Idempotency-Key": "C1"},
    )
    assert r.status_code == 200, r.text
    affected = set(r.json()["affected_batch_nos"])
    assert affected == {"MIX-1", "FG-1"}

    for no in ("RM-A", "MIX-1", "FG-1"):
        assert client.get(f"/batches/{no}").json()["is_quarantined"] is True
    assert client.get("/batches/FG-CLEAN").json()["is_quarantined"] is False
    assert client.get("/batches/RM-B").json()["is_quarantined"] is False


def test_quarantined_batch_cannot_be_consumed(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "60")], key="1")
    client.post(
        "/contamination", json={"batch_no": "RM-A"}, headers={"Idempotency-Key": "C"}
    )
    # 隔离的 MIX-1 不得再投料
    r = _produce(client, "FG-1", [("MIX-1", "10")], key="2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 100.0

    # 直接隔离的原料批次同样不能投料
    r = _produce(client, "MIX-2", [("RM-A", "5")], key="3")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"


def test_failed_request_not_stored_same_key_retryable(client):
    body = {
        "output_batch_no": "FG-1",
        "unit": "kg",
        "output_quantity": 5,
        "inputs": [{"batch_no": "RM-LATE", "quantity": 5}],
    }
    # 首次因来源不存在失败，失败请求不固化幂等记录
    r1 = client.post("/productions", json=body, headers={"Idempotency-Key": "K1"})
    assert r1.status_code == 404
    # 之后该来源批次被登记进来（另一笔业务）
    _raw(client, "RM-LATE", "100", key="raw-late")
    # 同键同内容重试：正常执行成功
    r2 = client.post("/productions", json=body, headers={"Idempotency-Key": "K1"})
    assert r2.status_code == 201, r2.text
    assert r2.json()["output_batch_no"] == "FG-1"
    # 再次重试仍返回同一结果、不重复扣减
    r3 = client.post("/productions", json=body, headers={"Idempotency-Key": "K1"})
    assert r3.status_code == 201
    assert client.get("/batches/RM-LATE").json()["remaining_qty"] == 95.0


def test_contamination_descendants_endpoint(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="2")
    r = client.post(
        "/contamination",
        json={"batch_no": "MIX-1"},
        headers={"Idempotency-Key": "C"},
    )
    assert r.json()["affected_batch_nos"] == ["FG-1"]
    assert r.json()["newly_quarantined"] == ["FG-1"]
    # RM-A 是上游，不被隔离
    assert client.get("/batches/RM-A").json()["is_quarantined"] is False


# ------------------------------------------------------------- 质检复核（误报撤销）


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


def _build_mixed_lineage(client):
    # RM-A、RM-B 混料 -> MIX-1 -> FG-1；RM-B 另有一条干净后代 FG-CLEAN
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "55")], key="1")
    _produce(client, "FG-1", [("MIX-1", "50")], key="2")
    _produce(client, "FG-CLEAN", [("RM-B", "10")], key="3")


def test_review_releases_suspect_and_all_descendants(client):
    _build_mixed_lineage(client)
    assert _flag(client, "RM-A", "c1").status_code == 200

    r = _review(client, "RM-A", "留样复测合格，确认为误报", "r1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reviewed_batch_no"] == "RM-A"
    assert body["is_suspect"] is False
    assert body["review_note"] == "留样复测合格，确认为误报"
    # 涉及的后代（重算范围）
    assert body["affected_batch_nos"] == ["MIX-1", "FG-1"]
    # 本次解除隔离：自身及全部后代；与 RM-A 无关的 FG-CLEAN 从不在内
    assert body["released_batch_nos"] == ["FG-1", "MIX-1", "RM-A"]
    assert body["still_quarantined"] == []

    for no in ("RM-A", "MIX-1", "FG-1"):
        d = client.get(f"/batches/{no}").json()
        assert d["is_suspect"] is False
        assert d["is_quarantined"] is False
    # 查询与谱系同步反映
    lin = client.get("/batches/RM-A/lineage").json()
    assert all(n["is_quarantined"] is False for n in lin["downstream"])
    assert all(n["is_suspect"] is False for n in lin["downstream"])

    # 解除后隔离批次可重新投料
    r = _produce(client, "FG-2", [("MIX-1", "5")], key="4")
    assert r.status_code == 201, r.text


def test_review_mixed_keeps_quarantine_while_other_source_suspect(client):
    _build_mixed_lineage(client)
    _flag(client, "RM-A", "c1")
    _flag(client, "RM-B", "c2")

    # 撤销 RM-A：混料后代 MIX-1/FG-1 仍因 RM-B 疑似而继续隔离
    r = _review(client, "RM-A", "RM-A 误报", "r1")
    assert r.status_code == 200
    body = r.json()
    assert body["released_batch_nos"] == ["RM-A"]
    assert body["still_quarantined"] == ["FG-1", "MIX-1"]

    rm_a = client.get("/batches/RM-A").json()
    assert rm_a["is_suspect"] is False and rm_a["is_quarantined"] is False
    for no in ("MIX-1", "FG-1", "RM-B"):
        d = client.get(f"/batches/{no}").json()
        assert d["is_quarantined"] is True
    # 仍隔离的混料批次不得投料
    r = _produce(client, "FG-X", [("MIX-1", "5")], key="x")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"

    # 撤销 RM-B 后全部解除（FG-CLEAN 从未被隔离，不在解除列表中）
    r = _review(client, "RM-B", "RM-B 误报", "r2")
    assert r.status_code == 200
    assert r.json()["released_batch_nos"] == ["FG-1", "FG-CLEAN", "MIX-1", "RM-B"]
    assert r.json()["still_quarantined"] == []
    for no in ("RM-B", "MIX-1", "FG-1", "FG-CLEAN"):
        assert client.get(f"/batches/{no}").json()["is_quarantined"] is False


def test_review_nonexistent_batch_errors_without_change(client):
    _build_mixed_lineage(client)
    r = _review(client, "GHOST", "查无此批", "r1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_review_non_suspect_batch_rejected(client):
    _build_mixed_lineage(client)
    # 从未疑似
    r = _review(client, "RM-A", "并未标记过", "r1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NOT_SUSPECT"
    assert client.get("/batches/RM-A").json()["is_suspect"] is False
    assert client.get("/batches/RM-A").json()["is_quarantined"] is False

    # 已撤销：再复核必须报错且不改变状态
    _flag(client, "RM-A", "c1")
    assert _review(client, "RM-A", "第一次撤销", "r2").status_code == 200
    r = _review(client, "RM-A", "重复撤销", "r3")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NOT_SUSPECT"
    d = client.get("/batches/RM-A").json()
    assert d["is_suspect"] is False and d["is_quarantined"] is False


def test_recan_flag_after_review_then_release_descendants_again(client):
    _build_mixed_lineage(client)
    _flag(client, "RM-A", "c1")
    assert _review(client, "RM-A", "误报", "r1").status_code == 200
    # 重新标记疑似（同批次可再次进入疑似状态），后代再次隔离
    r = _flag(client, "RM-A", "c2")
    assert r.status_code == 200
    assert set(r.json()["newly_quarantined"]) == {"MIX-1", "FG-1"}
    assert client.get("/batches/MIX-1").json()["is_quarantined"] is True
    # 再次复核解除
    r = _review(client, "RM-A", "再次确认误报", "r2")
    assert r.status_code == 200
    assert set(r.json()["released_batch_nos"]) == {"RM-A", "MIX-1", "FG-1"}


def test_review_requires_idempotency_key(client):
    _build_mixed_lineage(client)
    _flag(client, "RM-A", "c1")
    r = client.post("/reviews", json={"batch_no": "RM-A", "review_note": "无键"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    # 状态未被改变
    assert client.get("/batches/RM-A").json()["is_suspect"] is True


def test_review_idempotent_retry_returns_first_result(client):
    _build_mixed_lineage(client)
    _flag(client, "RM-A", "c1")
    payload = {"batch_no": "RM-A", "review_note": "误报，复测合格"}
    r1 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "R1"})
    assert r1.status_code == 200
    r2 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "R1"})
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # 疑似标记只被撤销一次（无 BatchReview 重复行 -> 再次执行会 409）
    r3 = client.post("/reviews", json=payload, headers={"Idempotency-Key": "R2"})
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "BATCH_NOT_SUSPECT"


def test_review_same_key_different_content_conflicts(client):
    _build_mixed_lineage(client)
    _flag(client, "RM-A", "c1")
    _flag(client, "RM-B", "c2")
    p1 = {"batch_no": "RM-A", "review_note": "RM-A 误报"}
    p2 = {"batch_no": "RM-B", "review_note": "RM-B 误报"}
    assert client.post("/reviews", json=p1, headers={"Idempotency-Key": "K"}).status_code == 200
    r = client.post("/reviews", json=p2, headers={"Idempotency-Key": "K"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突请求无副作用：RM-B 仍疑似/隔离
    d = client.get("/batches/RM-B").json()
    assert d["is_suspect"] is True and d["is_quarantined"] is True


def test_review_blank_note_rejected(client):
    _build_mixed_lineage(client)
    _flag(client, "RM-A", "c1")
    r = client.post(
        "/reviews",
        json={"batch_no": "RM-A", "review_note": "   "},
        headers={"Idempotency-Key": "R1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 未撤销
    assert client.get("/batches/RM-A").json()["is_suspect"] is True


def test_review_descendants_partial_release_with_unrelated_suspect_branch(client):
    # RM-A -> MIX-1 -> FG-1；另有 RM-A 与 RM-C 混料的 MIX-2；
    # 撤销 RM-A 时，MIX-2 因 RM-C 仍疑似而保留隔离。
    _raw(client, "RM-A", "100")
    _raw(client, "RM-C", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], out_qty=40, key="1")
    _produce(client, "MIX-2", [("RM-A", "10"), ("RM-C", "10")], key="2")
    _produce(client, "FG-1", [("MIX-1", "20")], key="3")
    _flag(client, "RM-A", "c1")
    _flag(client, "RM-C", "c2")

    r = _review(client, "RM-A", "误报", "r1")
    assert set(r.json()["released_batch_nos"]) == {"FG-1", "MIX-1", "RM-A"}
    # RM-C 不属于 RM-A 后代，不在本次重算范围；但它是 MIX-2 的疑似祖先，
    # MIX-2（RM-A 的混料后代）仍在隔离名单中
    assert set(r.json()["still_quarantined"]) == {"MIX-2"}
    assert client.get("/batches/MIX-2").json()["is_quarantined"] is True
    assert client.get("/batches/RM-C").json()["is_quarantined"] is True
    assert client.get("/batches/FG-1").json()["is_quarantined"] is False
