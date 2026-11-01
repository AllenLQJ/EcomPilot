"""AC-TX-03：重复取消订单防刷库存；带券取消释放券。"""
from __future__ import annotations

import threading
import uuid

import httpx

USER_A = "USR_A"
QTY = 2
COUPON_ID = "CPN_A_001"
SKU_SHAMPOO = "SKU_SHAMPOO_001"


def _place_stock_locked_order(
    client: httpx.Client,
    tx,
    user_id: str,
    sku_id: str,
    qty: int,
    coupon_ids: list[str] | None = None,
) -> tuple[str, dict[str, str]]:
    headers = tx.auth(user_id)
    quote = tx.make_quote(
        client,
        headers,
        items=[{"sku_id": sku_id, "qty": qty}],
        coupon_ids=coupon_ids,
    )
    resp = client.post(
        "/api/v1/orders/create",
        json={
            "idempotency_key": f"idem-tx03-{uuid.uuid4().hex}",
            "quote_id": quote["quote_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 201, (
        f"create order expected 201, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    order_id = body["order_id"]
    assert body.get("order_status") == "STOCK_LOCKED", (
        f"new order must be STOCK_LOCKED, got {body}"
    )
    assert tx.get_order_status(order_id) == "STOCK_LOCKED", (
        f"DB order_status must be STOCK_LOCKED for {order_id}"
    )
    return order_id, headers


def test_ac_tx_03_sequential_cancel_restocks_once(server: str, client: httpx.Client, tx):
    tx.clear_promo_rules()
    tx.set_stock(tx.SKU_STOCK_TEST, 10)
    stock_before = tx.get_stock(tx.SKU_STOCK_TEST)

    order_id, headers = _place_stock_locked_order(
        client, tx, USER_A, tx.SKU_STOCK_TEST, QTY
    )
    stock_locked = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_locked == stock_before - QTY, (
        f"after create, stock must drop by {QTY}: "
        f"before={stock_before}, locked={stock_locked}"
    )

    responses: list[httpx.Response] = []
    for i in range(3):
        r = client.post(f"/api/v1/orders/{order_id}/cancel", headers=headers)
        responses.append(r)
        assert r.status_code == 200, (
            f"cancel #{i + 1} must return 200, got {r.status_code}: {r.text}"
        )

    assert tx.get_order_status(order_id) == "CANCELLED", (
        f"after first cancel, order must be CANCELLED, "
        f"got {tx.get_order_status(order_id)}"
    )
    first_body = responses[0].json()
    assert first_body.get("order_status") == "CANCELLED", (
        f"first cancel response order_status must be CANCELLED, got {first_body}"
    )

    stock_after_first = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_after_first == stock_before, (
        f"first cancel must restore stock once to {stock_before}, "
        f"got {stock_after_first}"
    )

    stock_after_all = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_after_all == stock_before, (
        f"repeat cancels must not restock again: expected {stock_before}, "
        f"got {stock_after_all}"
    )


def test_ac_tx_03_concurrent_cancel_restocks_once(server: str, client: httpx.Client, tx):
    tx.clear_promo_rules()
    tx.set_stock(tx.SKU_STOCK_TEST, 10)
    stock_before = tx.get_stock(tx.SKU_STOCK_TEST)

    order_id, headers = _place_stock_locked_order(
        client, tx, USER_A, tx.SKU_STOCK_TEST, QTY
    )
    assert tx.get_stock(tx.SKU_STOCK_TEST) == stock_before - QTY

    barrier = threading.Barrier(3)
    results: list[httpx.Response] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with httpx.Client(base_url=server, timeout=30.0) as c:
                barrier.wait(timeout=10)
                r = c.post(f"/api/v1/orders/{order_id}/cancel", headers=headers)
                with lock:
                    results.append(r)
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent cancel exceptions: {errors!r}"
    assert len(results) == 3, f"expected 3 cancel responses, got {len(results)}"
    for i, r in enumerate(results):
        assert r.status_code == 200, (
            f"concurrent cancel #{i} must be 200, got {r.status_code}: {r.text}"
        )

    assert tx.get_order_status(order_id) == "CANCELLED", (
        f"order must end CANCELLED, got {tx.get_order_status(order_id)}"
    )
    stock_final = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_final == stock_before, (
        f"concurrent cancels must restore stock exactly once: "
        f"expected {stock_before}, got {stock_final}"
    )


def test_ac_tx_03_cancel_releases_coupon(server: str, client: httpx.Client, tx):
    """带券下单后取消，user_coupons.status 回到 UNUSED。"""
    tx.clear_promo_rules()
    # 洗护券 CPN_A_001：满 50 减 8，适用 SKU_SHAMPOO_001（5900 分）
    assert tx.get_coupon_status(COUPON_ID) == "UNUSED", (
        f"seed coupon {COUPON_ID} must start UNUSED"
    )

    order_id, headers = _place_stock_locked_order(
        client,
        tx,
        USER_A,
        SKU_SHAMPOO,
        qty=1,
        coupon_ids=[COUPON_ID],
    )
    assert tx.get_coupon_status(COUPON_ID) == "LOCKED", (
        f"after create, coupon must be LOCKED, got {tx.get_coupon_status(COUPON_ID)}"
    )

    cancel = client.post(f"/api/v1/orders/{order_id}/cancel", headers=headers)
    assert cancel.status_code == 200, (
        f"cancel expected 200, got {cancel.status_code}: {cancel.text}"
    )
    assert tx.get_order_status(order_id) == "CANCELLED", (
        f"order must be CANCELLED after cancel, got {tx.get_order_status(order_id)}"
    )
    assert tx.get_coupon_status(COUPON_ID) == "UNUSED", (
        f"cancel must release coupon to UNUSED, got {tx.get_coupon_status(COUPON_ID)}"
    )
