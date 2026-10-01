"""批次分装（split）：拆分子批次、校验规则、谱系与隔离传播、撤销拦截、幂等。"""
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


def _split(client, parent, targets, key):
    """targets: [(child_no, qty), ...]"""
    return client.post(
        f"/batches/{parent}/splits",
        json={"splits": [{"batch_no": no, "quantity": str(q)} for no, q in targets]},
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


def _cancel(client, output_no, reason, key):
    return client.post(
        f"/productions/{output_no}/cancellation",
        json={"cancel_reason": reason},
        headers={"Idempotency-Key": key},
    )


# --------------------------------------------------------------- 主流程


def test_split_creates_children_and_deducts_parent(client):
    assert _raw(client, "RM-1", 100).status_code == 201
    r = _split(client, "RM-1", [("RM-1-A", 30), ("RM-1-B", 40)], "s1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["parent_batch_no"] == "RM-1"
    assert body["parent_remaining_qty"] == 30.0
    assert body["splits"] == [
        {
            "batch_no": "RM-1-A",
            "unit": "kg",
            "quantity": 30.0,
            "remaining_qty": 30.0,
            "is_suspect": False,
            "is_quarantined": False,
        },
        {
            "batch_no": "RM-1-B",
            "unit": "kg",
            "quantity": 40.0,
            "remaining_qty": 40.0,
            "is_suspect": False,
            "is_quarantined": False,
        },
    ]
    assert body["relations"] == [
        {"parent_batch_no": "RM-1", "child_batch_no": "RM-1-A", "quantity": 30.0},
        {"parent_batch_no": "RM-1", "child_batch_no": "RM-1-B", "quantity": 40.0},
    ]

    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 30.0 and d["initial_qty"] == 100.0
    assert client.get("/batches/RM-1-A").json()["remaining_qty"] == 30.0
    assert client.get("/batches/RM-1-B").json()["unit"] == "kg"


def test_split_inherits_unit(client):
    _raw(client, "RM-L", 50, unit="L")
    r = _split(client, "RM-L", [("RM-L-1", 20)], "s1")
    assert r.status_code == 201
    assert r.json()["splits"][0]["unit"] == "L"
    # 子批次以父批次单位投料：一致通过；用错单位被拒
    r = _produce(client, "FG-L", [("RM-L-1", 5)], unit="L", key="p1")
    assert r.status_code == 201, r.text
    r = _produce(client, "FG-X", [("RM-L", 5)], unit="kg", key="p2")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"


def test_split_allows_consuming_exact_remaining(client):
    _raw(client, "RM-1", 10)
    r = _split(client, "RM-1", [("RM-1-A", 4), ("RM-1-B", 6)], "s1")
    assert r.status_code == 201
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 0.0
    # 父批次余量为 0 后仍可再对其子批次分装
    r = _split(client, "RM-1-A", [("RM-1-A-1", 4)], "s2")
    assert r.status_code == 201, r.text
    assert client.get("/batches/RM-1-A").json()["remaining_qty"] == 0.0
    assert client.get("/batches/RM-1-A-1").json()["remaining_qty"] == 4.0


def test_split_creates_no_production(client):
    # 分装不应产生生产单：子批次编号在生产单查询中报 PRODUCTION_NOT_FOUND
    _raw(client, "RM-1", 100)
    _split(client, "RM-1", [("RM-1-A", 30)], "s1")
    r = client.get("/productions/RM-1-A")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"


def test_child_batch_is_normal_production_source(client):
    _raw(client, "RM-A", 100)
    _raw(client, "RM-B", 100)
    _split(client, "RM-A", [("RM-A-1", 40)], "s1")
    # 子批次与原料批次一样可作为正常生产来源混料
    r = _produce(client, "MIX-1", [("RM-A-1", 40), ("RM-B", 50)], key="p1")
    assert r.status_code == 201, r.text
    assert client.get("/batches/RM-A-1").json()["remaining_qty"] == 0.0
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 50.0
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 90.0


# --------------------------------------------------------------- 拒绝规则


def test_split_parent_not_found(client):
    r = _split(client, "GHOST", [("GHOST-A", 1)], "s1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_split_empty_list_rejected(client):
    _raw(client, "RM-1", 10)
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": []},
        headers={"Idempotency-Key": "s1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_split_non_positive_qty_rejected(client):
    _raw(client, "RM-1", 10)
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "RM-1-A", "quantity": "0"}]},
        headers={"Idempotency-Key": "s1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "RM-1-A", "quantity": "-3"}]},
        headers={"Idempotency-Key": "s2"},
    )
    assert r.status_code == 422


def test_split_duplicate_targets_in_same_request(client):
    _raw(client, "RM-1", 100)
    r = _split(client, "RM-1", [("RM-1-A", 10), ("RM-1-A", 20)], "s1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_SPLIT_TARGET"
    # 整笔拒绝：父余量不变、子批次未创建
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert client.get("/batches/RM-1-A").status_code == 404


def test_split_exceeding_remaining_rejected_entirely(client):
    _raw(client, "RM-1", 10)
    r = _split(client, "RM-1", [("RM-1-A", 6), ("RM-1-B", 5)], "s1")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_BALANCE"
    assert err["details"]["requested"] == "11"
    assert err["details"]["remaining"] == "10.0000"
    # 整笔拒绝，不改变任何余量、不创建任何子批次
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 10.0
    assert client.get("/batches/RM-1-A").status_code == 404
    assert client.get("/batches/RM-1-B").status_code == 404


def test_split_number_not_reusable(client):
    _raw(client, "RM-1", 100)
    _raw(client, "RM-2", 100)
    _split(client, "RM-1", [("CHILD", 10)], "s1")
    # 已存在的子批次编号不得再次用作分装目标
    r = _split(client, "RM-2", [("CHILD", 5)], "s2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NO_REUSED"
    # 与原料/产出编号空间共用
    r = _split(client, "RM-2", [("RM-1", 5)], "s3")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NO_REUSED"
    assert client.get("/batches/RM-2").json()["remaining_qty"] == 100.0


def test_split_quarantined_parent_rejected(client):
    _raw(client, "RM-1", 100)
    _flag(client, "RM-1", "c1")
    r = _split(client, "RM-1", [("RM-1-A", 10)], "s1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    assert client.get("/batches/RM-1-A").status_code == 404
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_split_cancelled_output_parent_rejected(client):
    _raw(client, "RM-1", 100)
    _produce(client, "MIX-1", [("RM-1", 40)], key="p1")
    _cancel(client, "MIX-1", "录入错误", "c1")
    r = _split(client, "MIX-1", [("MIX-1-A", 5)], "s1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CANCELLED"
    assert client.get("/batches/MIX-1-A").status_code == 404


# --------------------------------------------------------------- 谱系


def test_lineage_includes_split_edges_both_directions(client):
    _raw(client, "RM-1", 100)
    _raw(client, "RM-2", 100)
    _split(client, "RM-1", [("RM-1-A", 30), ("RM-1-B", 20)], "s1")
    _produce(client, "MIX-1", [("RM-1-A", 30), ("RM-2", 10)], key="p1")
    _split(client, "RM-1-B", [("RM-1-B-1", 10)], "s2")

    lin = client.get("/batches/RM-1/lineage").json()
    down = {n["batch_no"]: n["depth"] for n in lin["downstream"]}
    # 分装边与投料边一起遍历：深度 1 = 直接子批次/产出
    assert down == {"RM-1-A": 1, "RM-1-B": 1, "RM-1-B-1": 2, "MIX-1": 2}
    edges = {(e["from"], e["to"]): e["kind"] for e in lin["edges"]}
    assert edges[("RM-1", "RM-1-A")] == "SPLIT"
    assert edges[("RM-1", "RM-1-B")] == "SPLIT"
    assert edges[("RM-1-B", "RM-1-B-1")] == "SPLIT"
    assert edges[("RM-1-A", "MIX-1")] == "PRODUCTION"
    # RM-2 不是 RM-1 的祖先/后代（混料旁支），不进入该谱系子图（既有行为）

    # 反向：MIX-1 的祖先经分装边追溯到 RM-1 与 RM-2
    up = client.get("/batches/MIX-1/lineage").json()
    assert {n["batch_no"] for n in up["upstream"]} == {"RM-1-A", "RM-1", "RM-2"}

    # 子批次自身的谱系：上游是父批次
    lin_a = client.get("/batches/RM-1-A/lineage").json()
    assert {n["batch_no"] for n in lin_a["upstream"]} == {"RM-1"}
    assert {n["batch_no"] for n in lin_a["downstream"]} == {"MIX-1"}


def test_lineage_diamond_dag_dedupes_descendant(client):
    # 两个子批次同时投入同一产出：MIX-1 可经两条分装边从 RM-1 到达，
    # 下游节点与污染传播名单都必须只出现一次（取最小深度）
    _raw(client, "RM-1", 100)
    _split(client, "RM-1", [("RM-1-A", 30), ("RM-1-B", 30)], "s1")
    _produce(client, "MIX-1", [("RM-1-A", 30), ("RM-1-B", 30)], key="p1")

    lin = client.get("/batches/RM-1/lineage").json()
    down = [n["batch_no"] for n in lin["downstream"]]
    assert sorted(down) == ["MIX-1", "RM-1-A", "RM-1-B"]
    assert len(down) == len(set(down))
    mix = next(n for n in lin["downstream"] if n["batch_no"] == "MIX-1")
    assert mix["depth"] == 2

    r = _flag(client, "RM-1", "c1")
    affected = r.json()["affected_batch_nos"]
    assert sorted(affected) == ["MIX-1", "RM-1-A", "RM-1-B"]
    assert len(affected) == len(set(affected))


# ----------------------------------------------------- 疑似/隔离沿分装边传播


def test_suspect_propagates_down_split_edges(client):
    _raw(client, "RM-1", 100)
    _raw(client, "RM-2", 100)
    _split(client, "RM-1", [("RM-1-A", 30), ("RM-1-B", 20)], "s1")
    _produce(client, "MIX-1", [("RM-1-A", 30), ("RM-2", 10)], key="p1")
    _split(client, "RM-1-B", [("RM-1-B-1", 10)], "s2")

    r = _flag(client, "RM-1", "c1")
    assert r.status_code == 200
    affected = set(r.json()["affected_batch_nos"])
    # 分装后代与投料后代同等对待；混料后代 MIX-1 整体隔离
    assert affected == {"RM-1-A", "RM-1-B", "RM-1-B-1", "MIX-1"}
    for no in ("RM-1-A", "RM-1-B", "RM-1-B-1", "MIX-1"):
        assert client.get(f"/batches/{no}").json()["is_quarantined"] is True
    # 与污染源无关的 RM-2 不受影响
    assert client.get("/batches/RM-2").json()["is_quarantined"] is False

    # 隔离的子批次不得再投料（子批次是正常生产来源，同样受隔离约束）
    r = _produce(client, "FG-X", [("RM-1-B-1", 5)], key="px")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"


def test_suspect_on_child_propagates_to_its_descendants_only(client):
    _raw(client, "RM-1", 100)
    _split(client, "RM-1", [("RM-1-A", 40), ("RM-1-B", 40)], "s1")
    _produce(client, "FG-A", [("RM-1-A", 10)], key="p1")
    r = _flag(client, "RM-1-A", "c1")
    assert r.status_code == 200
    assert r.json()["affected_batch_nos"] == ["FG-A"]
    # 父批次与兄弟子批次是上游/旁支，不被隔离
    assert client.get("/batches/RM-1").json()["is_quarantined"] is False
    assert client.get("/batches/RM-1-B").json()["is_quarantined"] is False
    assert client.get("/batches/FG-A").json()["is_quarantined"] is True
    assert client.get("/batches/RM-1-A").json()["is_suspect"] is True


def test_review_recomputes_quarantine_across_split_edges(client):
    _raw(client, "RM-1", 100)
    _split(client, "RM-1", [("RM-1-A", 40)], "s1")
    _produce(client, "FG-1", [("RM-1-A", 10)], key="p1")
    _flag(client, "RM-1", "c1")
    r = _review(client, "RM-1", "留样复测合格，误报", "r1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["released_batch_nos"]) == {"FG-1", "RM-1", "RM-1-A"}
    assert body["still_quarantined"] == []
    for no in ("RM-1", "RM-1-A", "FG-1"):
        d = client.get(f"/batches/{no}").json()
        assert d["is_quarantined"] is False and d["is_suspect"] is False
    # 解除后子批次可重新投料
    assert _produce(client, "FG-2", [("RM-1-A", 5)], key="p2").status_code == 201


def test_review_mixed_keeps_quarantine_via_other_suspect_source(client):
    # 混料后代 FG-1 = 分装子批次 RM-1-A + 原料 RM-2；撤销 RM-1 时，
    # FG-1 因 RM-2 仍疑似继续隔离（混料后代按任一疑似来源隔离）
    _raw(client, "RM-1", 100)
    _raw(client, "RM-2", 100)
    _split(client, "RM-1", [("RM-1-A", 40)], "s1")
    _produce(client, "FG-1", [("RM-1-A", 40), ("RM-2", 40)], key="p1")
    _flag(client, "RM-1", "c1")
    _flag(client, "RM-2", "c2")
    r = _review(client, "RM-1", "RM-1 误报", "r1")
    assert r.status_code == 200
    # RM-1 与其纯分装后代 RM-1-A 解除；混料后代 FG-1 因 RM-2 仍疑似继续隔离
    assert r.json()["released_batch_nos"] == ["RM-1", "RM-1-A"]
    assert r.json()["still_quarantined"] == ["FG-1"]
    assert client.get("/batches/FG-1").json()["is_quarantined"] is True
    assert client.get("/batches/RM-1-A").json()["is_quarantined"] is False
    assert client.get("/batches/RM-1-A").json()["is_suspect"] is False
    # RM-2 撤销后混料后代随之全部解除（RM-2 自身也解除）
    r = _review(client, "RM-2", "RM-2 误报", "r2")
    assert r.status_code == 200
    assert set(r.json()["released_batch_nos"]) == {"FG-1", "RM-2"}
    assert client.get("/batches/FG-1").json()["is_quarantined"] is False


# --------------------------------------------- 撤销入口：分装后视为曾被使用


def test_cancel_rejected_after_split_even_if_children_not_consumed(client):
    _raw(client, "RM-1", 100)
    _produce(client, "MIX-1", [("RM-1", 40)], key="p1")
    _split(client, "MIX-1", [("MIX-1-A", 10)], "s1")  # 子批次尚未投料

    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "OUTPUT_ALREADY_SPLIT"
    assert err["details"]["split_into"] == ["MIX-1-A"]
    # 不改库存：来源未返还、生产单未撤销
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 60.0
    rec = client.get("/productions/MIX-1").json()
    assert rec["is_cancelled"] is False


def test_split_on_raw_batch_does_not_block_anything_cancellable(client):
    # 原料批次没有撤销入口；分装原料批次后谱系/投料一切正常（回归保障）
    _raw(client, "RM-1", 100)
    _split(client, "RM-1", [("RM-1-A", 50)], "s1")
    assert _produce(client, "FG-1", [("RM-1-A", 50)], key="p1").status_code == 201
    lin = client.get("/batches/RM-1/lineage").json()
    assert {n["batch_no"] for n in lin["downstream"]} == {"RM-1-A", "FG-1"}


# --------------------------------------------------------------- 幂等


def test_split_requires_idempotency_key(client):
    _raw(client, "RM-1", 100)
    r = client.post(
        "/batches/RM-1/splits",
        json={"splits": [{"batch_no": "RM-1-A", "quantity": "10"}]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert client.get("/batches/RM-1-A").status_code == 404


def test_split_idempotent_replay_returns_first_result(client):
    _raw(client, "RM-1", 100)
    payload = {"splits": [{"batch_no": "RM-1-A", "quantity": "30"}]}
    r1 = client.post(
        "/batches/RM-1/splits", json=payload, headers={"Idempotency-Key": "S1"}
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/batches/RM-1/splits", json=payload, headers={"Idempotency-Key": "S1"}
    )
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    # 只扣一次
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 70.0


def test_split_same_key_different_content_conflicts(client):
    _raw(client, "RM-1", 100)
    p1 = {"splits": [{"batch_no": "RM-1-A", "quantity": "10"}]}
    p2 = {"splits": [{"batch_no": "RM-1-B", "quantity": "10"}]}
    assert client.post(
        "/batches/RM-1/splits", json=p1, headers={"Idempotency-Key": "K"}
    ).status_code == 201
    r = client.post(
        "/batches/RM-1/splits", json=p2, headers={"Idempotency-Key": "K"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert client.get("/batches/RM-1-B").status_code == 404
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_failed_split_not_stored_same_key_reexecutes(client):
    _raw(client, "RM-1", 100)
    payload = {"splits": [{"batch_no": "RM-1-A", "quantity": "20"}]}
    # 父批次隔离期：分装被拒（BATCH_QUARANTINED），失败请求不固化幂等记录
    _flag(client, "RM-1", "c1")
    r1 = client.post(
        "/batches/RM-1/splits", json=payload, headers={"Idempotency-Key": "K"}
    )
    assert r1.status_code == 409
    assert r1.json()["error"]["code"] == "BATCH_QUARANTINED"
    # 复核解除后，同键同体重试必须重新执行并成功（若首次失败被缓存会原样返回 409）
    assert _review(client, "RM-1", "误报", "r1").status_code == 200
    r2 = client.post(
        "/batches/RM-1/splits", json=payload, headers={"Idempotency-Key": "K"}
    )
    assert r2.status_code == 201, r2.text
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 80.0
