"""T2-D：下单 → 履约 → 聊天售后确认 → 超时回调对账全链路。"""
from __future__ import annotations

import importlib.util
import sys
import time
import uuid
from pathlib import Path

import httpx
from pilot.refunds.reconcile import reconcile_items

_CF_PATH = Path(__file__).resolve().parent / "conftest.py"
_CF_NAME = "tests_e2e_conftest_helpers"
if _CF_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CF_NAME, _CF_PATH)
    _CF = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CF_NAME] = _CF
    _SPEC.loader.exec_module(_CF)
else:
    _CF = sys.modules[_CF_NAME]

HEATER_PAYABLE_CENTS = _CF.HEATER_PAYABLE_CENTS
HEATER_SKU = _CF.HEATER_SKU
INTERNAL_TOKEN = _CF.INTERNAL_TOKEN
chat_stream = _CF.chat_stream
count_aftersale_requests = _CF.count_aftersale_requests
issue_dev_token = _CF.issue_dev_token
order_status = _CF.order_status
refund_rows_for_claim = _CF.refund_rows_for_claim
sql_scalar = _CF.sql_scalar
sse_cards = _CF.sse_cards
sse_tokens_text = _CF.sse_tokens_text
unique_user_id = _CF.unique_user_id


def test_e2e_full_chain(e2e_stack):
    stack = e2e_stack
    user_id = unique_user_id("USR_CHAIN")
    token = issue_dev_token(stack, user_id)
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        q = client.post(
            "/api/v1/quotes",
            headers=headers,
            json={"items": [{"sku_id": HEATER_SKU, "qty": 1}]},
        )
        assert q.status_code == 201, f"quotes expected 201, got {q.status_code}: {q.text}"
        quote = q.json()
        assert quote.get("total_payable_cents") == HEATER_PAYABLE_CENTS, (
            f"heater quote payable expected {HEATER_PAYABLE_CENTS}, "
            f"got {quote.get('total_payable_cents')}: {quote}"
        )

        c = client.post(
            "/api/v1/orders/create",
            headers=headers,
            json={"idempotency_key": f"IDK_{uuid.uuid4().hex}", "quote_id": quote["quote_id"]},
        )
        assert c.status_code in (200, 201), (
            f"orders/create expected 200/201, got {c.status_code}: {c.text}"
        )
        order = c.json()
        order_id = order["order_id"]
        order_item_id = order["items"][0]["order_item_id"]

        p = client.post(f"/api/v1/orders/{order_id}/pay", headers=headers)
        assert p.status_code == 200, f"pay expected 200, got {p.status_code}: {p.text}"

        f = client.post(
            f"/internal/v1/orders/{order_id}/fulfill",
            headers={"X-Internal-Token": stack.internal_token},
        )
        assert f.status_code == 200, f"fulfill expected 200, got {f.status_code}: {f.text}"
        assert order_status(order_id) == "FULFILLED", (
            f"order should be FULFILLED after fulfill, status={order_status(order_id)!r}, body={f.text}"
        )

    session_id = f"SESS_{uuid.uuid4().hex[:12]}"
    r1, events1 = chat_stream(
        stack,
        token,
        session_id=session_id,
        message="刚买的热水器打不着火还漏水，怎么退货？",
    )
    assert r1.status_code == 200, (
        f"aftersales chat expected HTTP 200, got {r1.status_code}: {r1.text[:800]}"
    )

    cand_cards = sse_cards(events1, "candidate_orders")
    assert cand_cards, (
        f"expected event:card type=candidate_orders after aftersales chat; events={events1!r}"
    )
    candidates = cand_cards[0].get("candidates") or []
    cand_ids = {
        (c.get("order_id") if isinstance(c, dict) else None)
        for c in candidates
    }
    assert order_id in cand_ids, (
        f"candidate_orders should include fulfilled order {order_id}, got candidates={candidates!r}"
    )

    comfort = sse_tokens_text(events1)
    assert comfort.strip(), (
        f"expected non-empty 安抚文本 via event:token; events={events1!r}"
    )

    assert count_aftersale_requests() == 0, (
        "candidates 阶段不得写入 aftersale_requests，但表中已有记录"
    )
    assert order_status(order_id) == "FULFILLED", (
        f"candidates 阶段订单应仍为 FULFILLED，实际 {order_status(order_id)!r}"
    )

    r2, events2 = chat_stream(
        stack,
        token,
        session_id=session_id,
        message="确认这一单申请退货退款",
        confirmed_order_id=order_id,
        confirmed_order_item_id=order_item_id,
    )
    assert r2.status_code == 200, (
        f"confirm chat expected HTTP 200, got {r2.status_code}: {r2.text[:800]}"
    )

    claim_cards = sse_cards(events2, "claim_submitted")
    assert claim_cards, (
        f"expected event:card type=claim_submitted after confirm; events={events2!r}"
    )
    claim_no = claim_cards[0].get("claim_no")
    assert claim_no, f"claim_submitted card missing claim_no: {claim_cards[0]!r}"

    assert order_status(order_id) == "AFTER_SALE_OPEN", (
        f"confirm 后订单应为 AFTER_SALE_OPEN，实际 {order_status(order_id)!r}"
    )

    with httpx.Client(base_url=stack.ops_base_url, timeout=60.0) as ops:
        claims_r = ops.get("/stub/admin/claims")
        assert claims_r.status_code == 200, f"GET /stub/admin/claims failed: {claims_r.text}"
        claims_body = claims_r.json()
        claim_items = (
            claims_body if isinstance(claims_body, list) else list(claims_body.get("items") or [])
        )
        claim_nos = {
            (it.get("claim_no") if isinstance(it, dict) else None)
            for it in claim_items
        }
        assert claim_no in claim_nos, (
            f"Ops 桩应可见 claim_no={claim_no!r}，实际 items={claim_items!r}"
        )

        fault = ops.post("/stub/admin/fault", json={"mode": "refund_callback_timeout"})
        assert fault.status_code == 200, f"set fault failed: {fault.status_code} {fault.text}"

        approve = ops.post(
            "/stub/admin/approve",
            json={"claim_no": claim_no, "approved_refund_cents": HEATER_PAYABLE_CENTS},
        )
        assert approve.status_code == 504, (
            f"refund_callback_timeout 下 approve 预期 504，实际 {approve.status_code}: {approve.text}"
        )

        pending: list = []
        deadline = time.time() + 15.0
        while time.time() < deadline:
            pr = ops.get("/stub/admin/pending")
            assert pr.status_code == 200, f"GET /stub/admin/pending failed: {pr.text}"
            body = pr.json()
            pending = body if isinstance(body, list) else list(body.get("items") or [])
            if pending:
                break
            time.sleep(0.2)
        assert pending, "expected pending reconcile items after refund_callback_timeout"

    outcomes = reconcile_items(pending, stack.pilot_base_url, INTERNAL_TOKEN)
    assert outcomes, f"reconcile_items returned empty: {outcomes!r}"
    assert all(o.get("outcome") in ("RECONCILED", "REPLAYED") for o in outcomes), (
        f"reconcile outcomes should be RECONCILED/REPLAYED, got {outcomes!r}"
    )

    rows = refund_rows_for_claim(claim_no)
    assert len(rows) == 1, f"refund_executions 应仅 1 行，实际 {len(rows)}: {rows!r}"
    assert rows[0]["refunded_cents"] == HEATER_PAYABLE_CENTS, (
        f"refunded_cents expected {HEATER_PAYABLE_CENTS}, got {rows[0].get('refunded_cents')}"
    )
    assert order_status(order_id) == "REFUNDED_FULL", (
        f"对账后订单应为 REFUNDED_FULL，实际 {order_status(order_id)!r}"
    )
    db_refunded = int(
        sql_scalar("SELECT refunded_cents FROM orders WHERE order_id = :o", {"o": order_id}) or 0
    )
    assert db_refunded == HEATER_PAYABLE_CENTS, (
        f"orders.refunded_cents expected {HEATER_PAYABLE_CENTS}, got {db_refunded}"
    )

    key = rows[0]["refund_idempotency_key"]
    with httpx.Client(base_url=stack.pilot_base_url, timeout=15.0) as client:
        st = client.get(
            "/internal/v1/refunds/status",
            params={"refund_idempotency_key": key},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
        assert st.status_code == 200, f"refunds/status expected 200, got {st.status_code}: {st.text}"
        assert st.json().get("execution_status") == "SUCCESS", (
            f"状态接口 execution_status 应为 SUCCESS，实际 {st.json()!r}"
        )
