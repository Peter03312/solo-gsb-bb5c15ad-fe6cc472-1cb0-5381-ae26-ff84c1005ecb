"""客户发货 / 退货 API 端到端测试。

覆盖：
- 整单原子扣减各批次各库位与全局余量，库位之和恒等于全局余量；
- 发货单查询：逐行发出量、累计退回量、可退量、行状态与整单状态；
- 退货按原批次原库位返还、累计退回不得超过原行发出量、逐次退货保留；
- 隔离 / 已撤销产出不可发货；发货后才隔离仍可退货、返还后继续隔离；
- 生产产出一旦发货即使全退也不得撤销原生产单；
- 单号 / 行重复、批次 / 库位 / 发货单 / 原行不存在、数量无效、
  余额不足、超退均整单拒绝且无部分变更；
- 写请求沿用既有 Idempotency-Key 重放 / 冲突语义。
"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty)},
        headers={"Idempotency-Key": key or f"raw-{batch_no}"},
    )


def _transfer(client, no, batch, src, dst, qty, key=None):
    return client.post(
        "/transfers",
        json={
            "transfer_no": no,
            "batch_no": batch,
            "source_location": src,
            "target_location": dst,
            "quantity": str(qty),
        },
        headers={"Idempotency-Key": key or f"tr-{no}"},
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
        headers={"Idempotency-Key": key or f"prod-{out_no}"},
    )


def _ship(client, no, lines, key=None):
    return client.post(
        "/shipments",
        json={
            "shipment_no": no,
            "lines": [
                {"batch_no": b, "location_code": loc, "quantity": str(q)}
                for b, loc, q in lines
            ],
        },
        headers={"Idempotency-Key": key or f"ship-{no}"},
    )


def _return(client, ret_no, ship_no, lines, key=None):
    return client.post(
        "/returns",
        json={
            "return_no": ret_no,
            "shipment_no": ship_no,
            "lines": [{"line_no": n, "quantity": str(q)} for n, q in lines],
        },
        headers={"Idempotency-Key": key or f"ret-{ret_no}"},
    )


def _locations(client, batch):
    body = client.get(f"/batches/{batch}/locations").json()
    return body["remaining_qty"], {r["location_code"]: r["quantity"] for r in body["locations"]}


# ---------------------------------------------------------------- 发货


def test_shipment_atomically_deducts_locations_and_global(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    _transfer(client, "T-1", "RM-1", "DEFAULT", "A-01", "40")

    r = _ship(client, "SH-1", [
        ("RM-1", "DEFAULT", "30"),
        ("RM-1", "A-01", "10"),
        ("RM-2", "DEFAULT", "20"),
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "SHIPPED"
    assert [(ln["line_no"], ln["shipped_qty"], ln["returned_qty"],
             ln["returnable_qty"], ln["status"]) for ln in body["lines"]] == [
        (1, 30.0, 0.0, 30.0, "SHIPPED"),
        (2, 10.0, 0.0, 10.0, "SHIPPED"),
        (3, 20.0, 0.0, 20.0, "SHIPPED"),
    ]
    assert body["returns"] == []

    # 两层库存同步扣减且恒等
    remaining, locs = _locations(client, "RM-1")
    assert remaining == 60.0
    assert locs == {"DEFAULT": 30.0, "A-01": 30.0}
    remaining2, locs2 = _locations(client, "RM-2")
    assert remaining2 == 30.0 and locs2 == {"DEFAULT": 30.0}


def test_shipment_rejects_duplicate_batch_location_line(client):
    _raw(client, "RM-1", "100")
    r = _ship(client, "SH-1", [
        ("RM-1", "DEFAULT", "10"),
        ("RM-1", "DEFAULT", "5"),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_SHIPMENT_LINE"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_shipment_rejects_reused_number(client):
    _raw(client, "RM-1", "100")
    assert _ship(client, "SH-1", [("RM-1", "DEFAULT", "10")]).status_code == 201
    r = _ship(client, "SH-1", [("RM-1", "DEFAULT", "5")], key="other")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIPMENT_NO_REUSED"
    # 只扣一次
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_shipment_missing_batch_entire_order_rejected(client):
    _raw(client, "RM-1", "100")
    r = _ship(client, "SH-1", [
        ("RM-1", "DEFAULT", "10"),
        ("GHOST", "DEFAULT", "5"),
    ])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert client.get("/shipments/SH-1").status_code == 404


def test_shipment_missing_location_rejected_without_change(client):
    _raw(client, "RM-1", "100")
    r = _ship(client, "SH-1", [("RM-1", "NOPE", "10")])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LOCATION_NOT_FOUND"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_shipment_location_insufficient_even_if_global_enough(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T-1", "RM-1", "DEFAULT", "A-01", "30")
    # A-01 仅 30，请求 40（全局仍有 100）
    r = _ship(client, "SH-1", [
        ("RM-1", "DEFAULT", "10"),
        ("RM-1", "A-01", "40"),
    ])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_BALANCE"
    assert err["details"]["location_code"] == "A-01"
    # 整单拒绝：DEFAULT 也不能被扣
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_shipment_invalid_quantity_rejected(client):
    _raw(client, "RM-1", "100")
    for qty in ("0", "-3"):
        r = client.post(
            "/shipments",
            json={"shipment_no": f"SH-{qty}",
                  "lines": [{"batch_no": "RM-1", "location_code": "DEFAULT",
                             "quantity": qty}]},
            headers={"Idempotency-Key": f"bad-qty-{qty}"},
        )
        assert r.status_code == 422
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_quarantined_and_cancelled_batches_cannot_ship(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _produce(client, "FG-1", [("RM-2", "30")], out_qty=30)

    # RM-1 标记疑似 -> 隔离，发货拒绝
    r = client.post("/contamination", json={"batch_no": "RM-1", "note": "污染"},
                    headers={"Idempotency-Key": "cont-1"})
    assert r.status_code == 200
    r = _ship(client, "SH-Q", [("RM-1", "DEFAULT", "1")])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"

    # 已撤销产出不可发货
    r = client.post("/productions/FG-1/cancellation",
                    json={"cancel_reason": "录入错误"},
                    headers={"Idempotency-Key": "cancel-fg1"})
    assert r.status_code == 200
    r = _ship(client, "SH-C", [("FG-1", "DEFAULT", "1")])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CANCELLED"


# ---------------------------------------------------------------- 退货


def test_return_restores_original_batch_and_location(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T-1", "RM-1", "DEFAULT", "A-01", "40")
    _ship(client, "SH-1", [
        ("RM-1", "DEFAULT", "30"),
        ("RM-1", "A-01", "10"),
    ])

    r = _return(client, "RT-1", "SH-1", [(1, "10"), (2, "4")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["shipment_status"] == "PARTIALLY_RETURNED"
    assert [(ln["line_no"], ln["quantity"], ln["returned_qty"],
             ln["returnable_qty"], ln["status"]) for ln in body["lines"]] == [
        (1, 10.0, 10.0, 20.0, "PARTIALLY_RETURNED"),
        (2, 4.0, 4.0, 6.0, "PARTIALLY_RETURNED"),
    ]

    # 严格返还到原库位
    remaining, locs = _locations(client, "RM-1")
    assert remaining == 74.0
    assert locs == {"DEFAULT": 40.0, "A-01": 34.0}


def test_shipment_query_tracks_each_return_and_statuses(client):
    _raw(client, "RM-1", "100")
    _ship(client, "SH-1", [("RM-1", "DEFAULT", "30")])

    # 第一次部分退
    assert _return(client, "RT-1", "SH-1", [(1, "10")]).status_code == 201
    body = client.get("/shipments/SH-1").json()
    assert body["status"] == "PARTIALLY_RETURNED"
    assert body["lines"][0]["returned_qty"] == 10.0
    assert body["lines"][0]["returnable_qty"] == 20.0
    assert len(body["returns"]) == 1

    # 第二次把余量退完 -> 全退回
    r = _return(client, "RT-2", "SH-1", [(1, "20")])
    assert r.status_code == 201
    assert r.json()["shipment_status"] == "FULLY_RETURNED"
    body = client.get("/shipments/SH-1")
    assert body.json()["status"] == "FULLY_RETURNED"
    line = body.json()["lines"][0]
    assert (line["shipped_qty"], line["returned_qty"], line["returnable_qty"]) == (
        30.0, 30.0, 0.0,
    )
    assert line["status"] == "FULLY_RETURNED"
    # 逐次退货记录均保留（按提交顺序）
    assert [r0["return_no"] for r0 in body.json()["returns"]] == ["RT-1", "RT-2"]
    assert [r0["lines"][0]["quantity"] for r0 in body.json()["returns"]] == [10.0, 20.0]
    # 全退后库存完全还原
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_partial_status_when_some_lines_unreturned(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    assert _ship(client, "SH-2", [
        ("RM-1", "DEFAULT", "10"),
        ("RM-2", "DEFAULT", "20"),
    ]).status_code == 201
    # 只退第一行：整单部分退回
    assert _return(client, "RT-1", "SH-2", [(1, "10")]).status_code == 201
    body = client.get("/shipments/SH-2").json()
    assert body["status"] == "PARTIALLY_RETURNED"
    assert body["lines"][0]["status"] == "FULLY_RETURNED"
    assert body["lines"][1]["status"] == "SHIPPED"


def test_return_over_shipped_rejected_without_change(client):
    _raw(client, "RM-1", "100")
    _ship(client, "SH-1", [("RM-1", "DEFAULT", "10")])
    assert _return(client, "RT-1", "SH-1", [(1, "4")]).status_code == 201

    # 再退 7：累计 11 > 发出 10
    r = _return(client, "RT-2", "SH-1", [(1, "7")])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "RETURN_EXCEEDS_SHIPPED"
    assert err["details"] == {
        "shipment_no": "SH-1", "line_no": 1,
        "requested": "7", "shipped_qty": "10.0000",
        "already_returned": "4.0000", "returnable_qty": "6.0000",
    }
    # 退货单不留记录、库存未再返还
    body = client.get("/shipments/SH-1").json()
    assert [r0["return_no"] for r0 in body["returns"]] == ["RT-1"]
    assert body["lines"][0]["returned_qty"] == 4.0
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 94.0


def test_return_duplicate_line_in_one_order_rejected(client):
    _raw(client, "RM-1", "100")
    _ship(client, "SH-1", [("RM-1", "DEFAULT", "10")])
    r = _return(client, "RT-1", "SH-1", [(1, "1"), (1, "2")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_RETURN_LINE"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_return_unknown_shipment_or_line(client):
    _raw(client, "RM-1", "100")
    _ship(client, "SH-1", [("RM-1", "DEFAULT", "10")])

    r = _return(client, "RT-X", "GHOST", [(1, "1")])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIPMENT_NOT_FOUND"

    r = _return(client, "RT-Y", "SH-1", [(99, "1")])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIPMENT_LINE_NOT_FOUND"


def test_return_number_reuse_rejected(client):
    _raw(client, "RM-1", "100")
    _ship(client, "SH-1", [("RM-1", "DEFAULT", "10")])
    assert _return(client, "RT-1", "SH-1", [(1, "1")]).status_code == 201
    r = _return(client, "RT-1", "SH-1", [(1, "1")], key="other-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RETURN_NO_REUSED"
    # 只退一次
    assert client.get("/shipments/SH-1").json()["lines"][0]["returned_qty"] == 1.0


def test_return_invalid_quantity_rejected(client):
    _raw(client, "RM-1", "100")
    _ship(client, "SH-1", [("RM-1", "DEFAULT", "10")])
    for qty in ("0", "-5"):
        r = client.post(
            "/returns",
            json={"return_no": f"RT-{qty}", "shipment_no": "SH-1",
                  "lines": [{"line_no": 1, "quantity": qty}]},
            headers={"Idempotency-Key": f"bad-ret-qty-{qty}"},
        )
        assert r.status_code == 422
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_return_allowed_after_quarantine_and_batch_stays_quarantined(client):
    """发货后才隔离的批次仍可退货，返还后继续隔离。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _produce(client, "FG-1", [("RM-2", "40")], out_qty=40)
    # 发货
    assert _ship(client, "SH-1", [("FG-1", "DEFAULT", "10")]).status_code == 201
    # 发货后污染来源 RM-2，FG-1 被连带隔离
    assert client.post(
        "/contamination", json={"batch_no": "RM-2", "note": "事后污染"},
        headers={"Idempotency-Key": "cont-after-ship"},
    ).status_code == 200
    assert client.get("/batches/FG-1").json()["is_quarantined"] is True

    # 隔离状态下仍可退货
    r = _return(client, "RT-1", "SH-1", [(1, "10")])
    assert r.status_code == 201, r.text
    fg = client.get("/batches/FG-1").json()
    assert fg["is_quarantined"] is True          # 返还后继续隔离
    assert fg["remaining_qty"] == 40.0           # 库存已返还原批次
    locs = client.get("/batches/FG-1/locations").json()
    assert locs["locations"][0]["quantity"] == 40.0


def test_shipped_output_cannot_cancel_production_even_fully_returned(client):
    """生产产出一旦发货，即使全退也不得撤销原生产单。"""
    _raw(client, "RM-1", "100")
    _produce(client, "FG-1", [("RM-1", "40")], out_qty=40)
    assert _ship(client, "SH-1", [("FG-1", "DEFAULT", "20")]).status_code == 201
    assert _return(client, "RT-1", "SH-1", [(1, "20")]).status_code == 201
    body = client.get("/shipments/SH-1").json()
    assert body["status"] == "FULLY_RETURNED"

    r = client.post("/productions/FG-1/cancellation",
                    json={"cancel_reason": "录入错误"},
                    headers={"Idempotency-Key": "cancel-after-return"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_ALREADY_SHIPPED"
    assert r.json()["error"]["details"]["shipment_nos"] == ["SH-1"]
    # 生产单仍有效
    assert client.get("/productions/FG-1").json()["is_cancelled"] is False


def test_unshipped_output_can_still_cancel(client):
    """未发货的产出撤销行为不受本轮改动影响。"""
    _raw(client, "RM-1", "100")
    _produce(client, "FG-1", [("RM-1", "10")], out_qty=10)
    r = client.post("/productions/FG-1/cancellation",
                    json={"cancel_reason": "录入错误"},
                    headers={"Idempotency-Key": "cancel-fresh"})
    assert r.status_code == 200
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


# ---------------------------------------------------------------- 幂等


def test_shipment_and_return_idempotent_replay(client):
    _raw(client, "RM-1", "100")
    ship_body = {"shipment_no": "SH-1",
                 "lines": [{"batch_no": "RM-1", "location_code": "DEFAULT",
                            "quantity": "10"}]}
    h = {"Idempotency-Key": "ship-key", "Content-Type": "application/json"}
    r1 = client.post("/shipments", json=ship_body, headers=h)
    r2 = client.post("/shipments", json=ship_body, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0

    ret_body = {"return_no": "RT-1", "shipment_no": "SH-1",
                "lines": [{"line_no": 1, "quantity": "4"}]}
    rh = {"Idempotency-Key": "ret-key", "Content-Type": "application/json"}
    r3 = client.post("/returns", json=ret_body, headers=rh)
    r4 = client.post("/returns", json=ret_body, headers=rh)
    assert r3.status_code == r4.status_code == 201
    assert r3.json() == r4.json()
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 94.0


def test_shipment_idempotency_conflict_on_different_body(client):
    _raw(client, "RM-1", "100")
    h = {"Content-Type": "application/json"}
    client.post("/shipments",
                json={"shipment_no": "SH-1",
                      "lines": [{"batch_no": "RM-1", "location_code": "DEFAULT",
                                 "quantity": "10"}]},
                headers={**h, "Idempotency-Key": "same"})
    r = client.post("/shipments",
                    json={"shipment_no": "SH-2",
                          "lines": [{"batch_no": "RM-1", "location_code": "DEFAULT",
                                     "quantity": "11"}]},
                    headers={**h, "Idempotency-Key": "same"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_shipment_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post("/shipments",
                    json={"shipment_no": "SH-1",
                          "lines": [{"batch_no": "RM-1", "location_code": "DEFAULT",
                                     "quantity": "1"}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_get_unknown_shipment_404(client):
    r = client.get("/shipments/GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIPMENT_NOT_FOUND"
