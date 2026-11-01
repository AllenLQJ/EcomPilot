"""AC-TX-01：同一 idempotency_key + quote_id 并发下单幂等。"""
from __future__ import annotations

import threading
import uuid

import httpx

USER_A = "USR_A"
QTY = 2


def test_ac_tx_01_concurrent_idempotent_create(server: str, client: httpx.Client, tx):
    tx.clear_promo_rules()
    tx.set_stock(tx.SKU_STOCK_TEST, 10)
    stock_before = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_before == 10, f"expected initial stock 10, got {stock_before}"

    headers = tx.auth(USER_A)
    quote = tx.make_quote(
        client,
        headers,
        items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": QTY}],
    )
    quote_id = quote["quote_id"]
    idem_key = f"idem-tx01-{uuid.uuid4().hex}"

    barrier = threading.Barrier(5)
    results: list[httpx.Response] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with httpx.Client(base_url=server, timeout=30.0) as c:
                barrier.wait(timeout=10)
                r = c.post(
                    "/api/v1/orders/create",
                    json={"idempotency_key": idem_key, "quote_id": quote_id},
                    headers=headers,
                )
                with lock:
                    results.append(r)
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"worker exceptions: {errors!r}"
    assert len(results) == 5, f"expected 5 responses, got {len(results)}"

    statuses = [r.status_code for r in results]
    assert all(s in (200, 201) for s in statuses), (
        f"all responses must be 200/201, got statuses={statuses}, "
        f"bodies={[r.text for r in results]}"
    )
    created = [r for r in results if r.status_code == 201]
    replayed = [r for r in results if r.status_code == 200]
    assert len(created) <= 1, (
        f"at most one 201 Created allowed, got {len(created)}: "
        f"{[r.text for r in created]}"
    )
    for r in replayed:
        body = r.json()
        assert body.get("idempotent_replayed") is True, (
            f"200 replay must set idempotent_replayed=true, got {body}"
        )

    bodies = [r.json() for r in results]
    order_ids = {b.get("order_id") for b in bodies}
    assert len(order_ids) == 1 and None not in order_ids, (
        f"all responses must share the same order_id, got {order_ids}"
    )
    order_id = next(iter(order_ids))

    n_rows = tx.count_orders_by_idempotency_key(USER_A, idem_key)
    assert n_rows == 1, (
        f"orders table must have exactly 1 row for idempotency_key={idem_key!r}, "
        f"got {n_rows}"
    )

    stock_after = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_after == stock_before - QTY, (
        f"stock must be deducted once by qty={QTY}: "
        f"before={stock_before}, after={stock_after}"
    )
    assert order_id, "order_id must be non-empty"


def test_ac_tx_01_idempotency_key_conflict_different_quote(
    server: str, client: httpx.Client, tx
):
    tx.clear_promo_rules()
    tx.set_stock(tx.SKU_STOCK_TEST, 10)
    headers = tx.auth(USER_A)

    quote1 = tx.make_quote(
        client, headers, items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 1}]
    )
    quote2 = tx.make_quote(
        client, headers, items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 1}]
    )
    assert quote1["quote_id"] != quote2["quote_id"], "need two distinct quotes"

    idem_key = f"idem-tx01-conflict-{uuid.uuid4().hex}"
    first = client.post(
        "/api/v1/orders/create",
        json={"idempotency_key": idem_key, "quote_id": quote1["quote_id"]},
        headers=headers,
    )
    assert first.status_code in (200, 201), (
        f"initial create expected 200/201, got {first.status_code}: {first.text}"
    )

    conflict = client.post(
        "/api/v1/orders/create",
        json={"idempotency_key": idem_key, "quote_id": quote2["quote_id"]},
        headers=headers,
    )
    assert conflict.status_code == 409, (
        f"same key + different quote_id must be 409, got {conflict.status_code}: "
        f"{conflict.text}"
    )
    assert tx.error_code(conflict) == "IDEMPOTENCY_KEY_CONFLICT", (
        f"expected error.code=IDEMPOTENCY_KEY_CONFLICT, got {conflict.text}"
    )


def test_ac_tx_01_expired_quote_rejected(server: str, client: httpx.Client, tx):
    tx.clear_promo_rules()
    tx.set_stock(tx.SKU_STOCK_TEST, 10)
    headers = tx.auth(USER_A)

    quote = tx.make_quote(
        client, headers, items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 1}]
    )
    tx.expire_quote(quote["quote_id"])
    stock_before = tx.get_stock(tx.SKU_STOCK_TEST)

    resp = client.post(
        "/api/v1/orders/create",
        json={
            "idempotency_key": f"idem-tx01-expired-{uuid.uuid4().hex}",
            "quote_id": quote["quote_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 409, (
        f"expired quote must return 409, got {resp.status_code}: {resp.text}"
    )
    assert tx.error_code(resp) == "QUOTE_EXPIRED_OR_CHANGED", (
        f"expected error.code=QUOTE_EXPIRED_OR_CHANGED, got {resp.text}"
    )
    stock_after = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_after == stock_before, (
        f"expired quote must not deduct stock: before={stock_before}, after={stock_after}"
    )
