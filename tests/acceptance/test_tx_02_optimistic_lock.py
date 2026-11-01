"""AC-TX-02：乐观锁版本冲突与真实缺货区分。"""
from __future__ import annotations

import threading
import uuid

import httpx

USER_A = "USR_A"
USER_B = "USR_B"
USER_C = "USR_C"
ROUNDS = 5


def _create_order(
    base_url: str,
    user_id: str,
    quote_id: str,
    idem_key: str,
    barrier: threading.Barrier,
    out: list,
    lock: threading.Lock,
    errors: list,
    auth_fn,
) -> None:
    try:
        headers = auth_fn(user_id)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            barrier.wait(timeout=15)
            r = c.post(
                "/api/v1/orders/create",
                json={"idempotency_key": idem_key, "quote_id": quote_id},
                headers=headers,
            )
            with lock:
                out.append(r)
    except BaseException as e:  # noqa: BLE001
        with lock:
            errors.append(e)


def test_ac_tx_02_two_buyers_succeed_third_insufficient(
    server: str, client: httpx.Client, tx
):
    tx.clear_promo_rules()

    for round_idx in range(ROUNDS):
        tx.clear_orders()
        tx.set_stock(tx.SKU_STOCK_TEST, 10)
        assert tx.get_stock(tx.SKU_STOCK_TEST) == 10, (
            f"round {round_idx}: stock reset failed"
        )

        headers_a = tx.auth(USER_A)
        headers_b = tx.auth(USER_B)
        quote_a = tx.make_quote(
            client, headers_a, items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 2}]
        )
        quote_b = tx.make_quote(
            client, headers_b, items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 2}]
        )

        barrier = threading.Barrier(2)
        results: list[httpx.Response] = []
        lock = threading.Lock()
        errors: list[BaseException] = []
        key_a = f"idem-tx02-a-r{round_idx}-{uuid.uuid4().hex}"
        key_b = f"idem-tx02-b-r{round_idx}-{uuid.uuid4().hex}"

        t1 = threading.Thread(
            target=_create_order,
            args=(
                server,
                USER_A,
                quote_a["quote_id"],
                key_a,
                barrier,
                results,
                lock,
                errors,
                tx.auth,
            ),
        )
        t2 = threading.Thread(
            target=_create_order,
            args=(
                server,
                USER_B,
                quote_b["quote_id"],
                key_b,
                barrier,
                results,
                lock,
                errors,
                tx.auth,
            ),
        )
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)

        assert not errors, f"round {round_idx}: worker exceptions {errors!r}"
        assert len(results) == 2, (
            f"round {round_idx}: expected 2 responses, got {len(results)}"
        )

        for r in results:
            code = tx.error_code(r)
            assert code not in ("INSUFFICIENT_STOCK", "OUT_OF_STOCK"), (
                f"round {round_idx}: must not report stock shortage for qty=2+2 "
                f"on stock=10; status={r.status_code} body={r.text}"
            )
            assert r.status_code == 201, (
                f"round {round_idx}: both buyers must get 201 Created, "
                f"got {r.status_code}: {r.text}"
            )

        stock_mid = tx.get_stock(tx.SKU_STOCK_TEST)
        assert stock_mid == 6, (
            f"round {round_idx}: after two qty=2 orders, stock must be 6, got {stock_mid}"
        )

    # 最终一轮库存已为 6；USR_C 买 20 → 真实缺货
    headers_c = tx.auth(USER_C)
    quote_c = tx.make_quote(
        client, headers_c, items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 20}]
    )
    stock_before_c = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_before_c == 6, f"pre-USR_C stock must be 6, got {stock_before_c}"

    resp_c = client.post(
        "/api/v1/orders/create",
        json={
            "idempotency_key": f"idem-tx02-c-{uuid.uuid4().hex}",
            "quote_id": quote_c["quote_id"],
        },
        headers=headers_c,
    )
    assert resp_c.status_code == 422, (
        f"USR_C qty=20 must return 422, got {resp_c.status_code}: {resp_c.text}"
    )
    assert tx.error_code(resp_c) == "INSUFFICIENT_STOCK", (
        f"expected INSUFFICIENT_STOCK, got {resp_c.text}"
    )
    stock_after_c = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_after_c == 6, (
        f"INSUFFICIENT_STOCK must not change stock: before={stock_before_c}, "
        f"after={stock_after_c}"
    )


def test_ac_tx_02_pressure_20_buyers_allow_version_conflict(
    server: str, client: httpx.Client, tx
):
    tx.clear_promo_rules()
    tx.clear_orders()
    tx.set_stock(tx.SKU_STOCK_TEST, 100)
    assert tx.get_stock(tx.SKU_STOCK_TEST) == 100

    n_users = 20
    user_ids = [f"USR_P{i:02d}" for i in range(1, n_users + 1)]
    quotes: dict[str, str] = {}
    for uid in user_ids:
        q = tx.make_quote(
            client, tx.auth(uid), items=[{"sku_id": tx.SKU_STOCK_TEST, "qty": 1}]
        )
        quotes[uid] = q["quote_id"]

    barrier = threading.Barrier(n_users)
    results: list[tuple[str, httpx.Response]] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(uid: str) -> None:
        try:
            headers = tx.auth(uid)
            with httpx.Client(base_url=server, timeout=60.0) as c:
                barrier.wait(timeout=30)
                r = c.post(
                    "/api/v1/orders/create",
                    json={
                        "idempotency_key": f"idem-tx02-p-{uid}-{uuid.uuid4().hex}",
                        "quote_id": quotes[uid],
                    },
                    headers=headers,
                )
                with lock:
                    results.append((uid, r))
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in user_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"pressure worker exceptions: {errors!r}"
    assert len(results) == n_users, f"expected {n_users} responses, got {len(results)}"

    success = 0
    conflicts = 0
    for uid, r in results:
        code = tx.error_code(r)
        assert code not in ("INSUFFICIENT_STOCK", "OUT_OF_STOCK"), (
            f"pressure: never report shortage when stock=100 and each buys 1; "
            f"user={uid} status={r.status_code} body={r.text}"
        )
        if r.status_code in (200, 201):
            success += 1
        elif r.status_code == 503:
            assert code == "CONCURRENT_VERSION_CONFLICT", (
                f"pressure: 503 must use CONCURRENT_VERSION_CONFLICT, "
                f"user={uid} body={r.text}"
            )
            conflicts += 1
        else:
            raise AssertionError(
                f"pressure: unexpected status for {uid}: {r.status_code} {r.text}"
            )

    stock_final = tx.get_stock(tx.SKU_STOCK_TEST)
    assert stock_final == 100 - success, (
        f"stock must equal 100 - success_count: stock={stock_final}, "
        f"success={success}, conflicts={conflicts}"
    )
    assert success + conflicts == n_users, (
        f"every request must be success or version-conflict: "
        f"success={success}, conflicts={conflicts}"
    )
