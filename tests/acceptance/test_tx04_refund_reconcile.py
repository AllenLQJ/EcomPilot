"""AC-TX-04 C 端：退款回调超时对账、重复回调与幂等/超额/鉴权。"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from pilot.refunds.reconcile import reconcile_items

_CF_PATH = Path(__file__).resolve().parent / "conftest_f.py"
_CF_NAME = "tests_acceptance_conftest_f"
if _CF_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CF_NAME, _CF_PATH)
    _CF = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CF_NAME] = _CF
    _SPEC.loader.exec_module(_CF)
else:
    _CF = sys.modules[_CF_NAME]

f_assert_approve_http_ok = _CF.f_assert_approve_http_ok
f_confirm_aftersale = _CF.f_confirm_aftersale
f_defective_stock = _CF.f_defective_stock
f_order_status = _CF.f_order_status
f_place_heater_order = _CF.f_place_heater_order
f_refund_rows_for_claim = _CF.f_refund_rows_for_claim
f_sql_scalar = _CF.f_sql_scalar
F_HEATER_PAYABLE_CENTS = _CF.F_HEATER_PAYABLE_CENTS
F_HEATER_SKU = _CF.F_HEATER_SKU
F_INTERNAL_TOKEN = _CF.F_INTERNAL_TOKEN
F_USER_A = _CF.F_USER_A


@pytest.fixture
def f_acceptance_stack():
    with _CF.f_live_stack_cm() as stack:
        yield stack


def _set_fault(stack, mode: str) -> None:
    with httpx.Client(base_url=stack.ops_base_url, timeout=15.0) as client:
        r = client.post("/stub/admin/fault", json={"mode": mode})
        assert r.status_code == 200, r.text


def _approve(stack, claim_no: str, approved_refund_cents: int = F_HEATER_PAYABLE_CENTS) -> httpx.Response:
    with httpx.Client(base_url=stack.ops_base_url, timeout=60.0) as client:
        return client.post(
            "/stub/admin/approve",
            json={"claim_no": claim_no, "approved_refund_cents": approved_refund_cents},
        )


def _pending_items(stack) -> list:
    with httpx.Client(base_url=stack.ops_base_url, timeout=15.0) as client:
        r = client.get("/stub/admin/pending")
        assert r.status_code == 200, r.text
        body = r.json()
        if isinstance(body, list):
            return list(body)
        return list(body.get("items") or [])


def _wait_pending(stack, min_items: int = 1, timeout: float = 20.0) -> list:
    deadline = time.time() + timeout
    items: list = []
    while time.time() < deadline:
        items = _pending_items(stack)
        if len(items) >= min_items:
            return items
        time.sleep(0.2)
    return items


def _open_aftersale(stack, user_id: str = F_USER_A) -> tuple[dict, str]:
    order = f_place_heater_order(stack, user_id, pay=True, fulfill=True)
    r = f_confirm_aftersale(stack, user_id, order["order_id"], order["order_item_id"])
    assert r.status_code == 202, r.text
    body = r.json()
    claim_no = body["claim_no"]
    assert f_order_status(order["order_id"]) == "AFTER_SALE_OPEN"
    if "order_status" in body:
        assert body["order_status"] == "AFTER_SALE_OPEN"
    return order, claim_no


def test_tx04_timeout_concurrent_approve_reconcile_single_refund(f_acceptance_stack):
    stack = f_acceptance_stack
    defective_before = f_defective_stock(F_HEATER_SKU)
    order, claim_no = _open_aftersale(stack)

    _set_fault(stack, "refund_callback_timeout")

    results: list[httpx.Response] = []
    lock = threading.Lock()

    def _one():
        resp = _approve(stack, claim_no, F_HEATER_PAYABLE_CENTS)
        with lock:
            results.append(resp)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_one) for _ in range(2)]
        for fut in futs:
            fut.result(timeout=90)

    assert len(results) == 2
    for resp in results:
        f_assert_approve_http_ok(resp.status_code)

    items = _wait_pending(stack, min_items=1)
    assert items, "expected pending reconcile items after refund_callback_timeout"

    outcomes = reconcile_items(items, stack.pilot_base_url, F_INTERNAL_TOKEN)
    assert outcomes, outcomes
    assert all(o.get("outcome") in ("RECONCILED", "REPLAYED") for o in outcomes), outcomes
    assert all(o.get("outcome") != "RETRY_LATER" for o in outcomes), outcomes

    rows = f_refund_rows_for_claim(claim_no)
    assert len(rows) == 1, rows
    assert rows[0]["refunded_cents"] == F_HEATER_PAYABLE_CENTS
    assert f_order_status(order["order_id"]) == "REFUNDED_FULL"
    assert int(f_sql_scalar("SELECT refunded_cents FROM orders WHERE order_id = :o", {"o": order["order_id"]})) == (
        F_HEATER_PAYABLE_CENTS
    )
    assert f_defective_stock(F_HEATER_SKU) == defective_before + 1


def test_tx04_duplicate_callback_still_single_refund(f_acceptance_stack):
    stack = f_acceptance_stack
    order, claim_no = _open_aftersale(stack)

    _set_fault(stack, "duplicate_callback")
    resp = _approve(stack, claim_no, F_HEATER_PAYABLE_CENTS)
    f_assert_approve_http_ok(resp.status_code)

    # 给桩双发回调一点时间落库
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if f_order_status(order["order_id"]) == "REFUNDED_FULL":
            break
        time.sleep(0.2)

    rows = f_refund_rows_for_claim(claim_no)
    assert len(rows) == 1, rows
    assert rows[0]["refunded_cents"] == F_HEATER_PAYABLE_CENTS
    assert f_order_status(order["order_id"]) == "REFUNDED_FULL"


def test_tx04_execute_replay_exceeds_and_missing_token(f_acceptance_stack):
    stack = f_acceptance_stack
    order, claim_no = _open_aftersale(stack)

    _set_fault(stack, "none")
    resp = _approve(stack, claim_no, F_HEATER_PAYABLE_CENTS)
    f_assert_approve_http_ok(resp.status_code)

    deadline = time.time() + 15.0
    while time.time() < deadline and f_order_status(order["order_id"]) != "REFUNDED_FULL":
        time.sleep(0.2)
    assert f_order_status(order["order_id"]) == "REFUNDED_FULL"

    rows = f_refund_rows_for_claim(claim_no)
    assert len(rows) == 1
    key = rows[0]["refund_idempotency_key"]

    execute_body = {
        "refund_idempotency_key": key,
        "claim_no": claim_no,
        "order_id": order["order_id"],
        "order_item_id": order["order_item_id"],
        "approved_refund_cents": F_HEATER_PAYABLE_CENTS,
        "restock_policy": "DEFECTIVE_WAREHOUSE",
        "approval_mode": "SUPERVISOR_CONFIRMED",
        "supervisor_user_id": "SUP_007",
    }

    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        replay = client.post(
            "/internal/v1/refunds/execute",
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
            json=execute_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json().get("idempotent_replayed") is True

        # 新售后单上超额退款
        order2, claim2 = _open_aftersale(stack)
        exceeds = client.post(
            "/internal/v1/refunds/execute",
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
            json={
                "refund_idempotency_key": f"REFUND_{claim2}_OVER",
                "claim_no": claim2,
                "order_id": order2["order_id"],
                "order_item_id": order2["order_item_id"],
                "approved_refund_cents": F_HEATER_PAYABLE_CENTS + 1,
                "restock_policy": "DEFECTIVE_WAREHOUSE",
                "approval_mode": "SUPERVISOR_CONFIRMED",
                "supervisor_user_id": "SUP_007",
            },
        )
        assert exceeds.status_code == 422, exceeds.text
        assert exceeds.json()["error"]["code"] == "REFUND_EXCEEDS_LIMIT"

        no_tok = client.post("/internal/v1/refunds/execute", json=execute_body)
        assert no_tok.status_code == 401, no_tok.text
