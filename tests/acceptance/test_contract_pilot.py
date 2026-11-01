"""T2-E：Pilot OpenAPI 契约一致性（对照 contracts/openapi/pilot.yaml + errors.md）。"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest

_CC_PATH = Path(__file__).resolve().parent / "conftest_contract.py"
_CC_NAME = "tests_acceptance_conftest_contract"
if _CC_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CC_NAME, _CC_PATH)
    _CC = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CC_NAME] = _CC
    _SPEC.loader.exec_module(_CC)
else:
    _CC = sys.modules[_CC_NAME]

assert_error_body = _CC.assert_error_body
assert_response_matches_schema = _CC.assert_response_matches_schema
bearer = _CC.bearer
expire_quote = _CC.expire_quote
internal_headers = _CC.internal_headers
load_error_codes = _CC.load_error_codes
load_openapi = _CC.load_openapi
pilot_live_stack_cm = _CC.pilot_live_stack_cm
set_stock = _CC.set_stock
SKU_HEATER = _CC.SKU_HEATER
SKU_STOCK_TEST = _CC.SKU_STOCK_TEST
USER_A = _CC.USER_A


@pytest.fixture(scope="module")
def pilot_spec():
    return load_openapi("pilot.yaml")


@pytest.fixture(scope="module")
def error_codes():
    return load_error_codes()


@pytest.fixture(scope="module")
def stack():
    with pilot_live_stack_cm() as s:
        yield s


@pytest.fixture(scope="module")
def client(stack):
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as c:
        yield c


def _ctx(method: str, path: str, note: str = "") -> str:
    base = f"{method.upper()} {path}"
    return f"{base} ({note})" if note else base


def test_contract_health(client: httpx.Client, pilot_spec):
    path, method = "/health", "get"
    r = client.get(path)
    assert_response_matches_schema(
        pilot_spec,
        path,
        method,
        r,
        context=_ctx(method, path),
        require_json_schema=False,
    )
    assert r.status_code == 200, f"GET /health expected 200, got {r.status_code}: {r.text}"


def test_contract_quotes_orders_pay_cancel_fulfill_aftersales_refunds(
    client: httpx.Client, stack, pilot_spec
):
    """主成功路径：quotes → create(+replay) → list/get → pay → fulfill → candidates/confirm → refund。"""
    headers = bearer(USER_A)
    ih = internal_headers(stack.internal_token)
    set_stock(SKU_HEATER, 50)

    # --- quotes 201 → Quote ---
    path, method = "/api/v1/quotes", "post"
    q = client.post(path, headers=headers, json={"items": [{"sku_id": SKU_HEATER, "qty": 1}]})
    quote = assert_response_matches_schema(
        pilot_spec, path, method, q, context=_ctx(method, path, "create quote")
    )
    assert q.status_code == 201, f"quotes expected 201, got {q.status_code}: {q.text}"
    assert isinstance(quote, dict)
    quote_id = quote["quote_id"]

    # --- orders/create 201 → Order ---
    path, method = "/api/v1/orders/create", "post"
    idem = f"IDK_CONTRACT_{uuid.uuid4().hex}"
    c1 = client.post(
        path,
        headers=headers,
        json={"idempotency_key": idem, "quote_id": quote_id},
    )
    order = assert_response_matches_schema(
        pilot_spec, path, method, c1, context=_ctx(method, path, "create")
    )
    assert c1.status_code == 201, f"orders/create expected 201, got {c1.status_code}: {c1.text}"
    assert isinstance(order, dict)
    order_id = order["order_id"]
    order_item_id = order["items"][0]["order_item_id"]

    # --- orders/create 200 replay → Order + idempotent_replayed ---
    c2 = client.post(
        path,
        headers=headers,
        json={"idempotency_key": idem, "quote_id": quote_id},
    )
    replay = assert_response_matches_schema(
        pilot_spec, path, method, c2, context=_ctx(method, path, "idempotent replay")
    )
    assert c2.status_code == 200, (
        f"orders/create replay expected 200, got {c2.status_code}: {c2.text}"
    )
    assert isinstance(replay, dict)
    assert replay.get("idempotent_replayed") is True, (
        f"replay must set idempotent_replayed=true, got {replay!r}"
    )
    assert replay.get("order_id") == order_id, (
        f"replay order_id must match original: {replay.get('order_id')!r} vs {order_id!r}"
    )

    # --- orders list 200 ---
    path, method = "/api/v1/orders", "get"
    lst = client.get(path, headers=headers)
    assert_response_matches_schema(
        pilot_spec, path, method, lst, context=_ctx(method, path)
    )
    assert lst.status_code == 200, f"orders list expected 200, got {lst.status_code}: {lst.text}"

    # --- orders detail 200 → Order ---
    path, method = "/api/v1/orders/{order_id}", "get"
    det = client.get(f"/api/v1/orders/{order_id}", headers=headers)
    assert_response_matches_schema(
        pilot_spec, path, method, det, context=_ctx(method, path, "detail")
    )
    assert det.status_code == 200, f"orders detail expected 200, got {det.status_code}: {det.text}"

    # --- pay 200 → Order ---
    path, method = "/api/v1/orders/{order_id}/pay", "post"
    pay = client.post(f"/api/v1/orders/{order_id}/pay", headers=headers)
    paid = assert_response_matches_schema(
        pilot_spec, path, method, pay, context=_ctx(method, path)
    )
    assert pay.status_code == 200, f"pay expected 200, got {pay.status_code}: {pay.text}"
    assert isinstance(paid, dict)

    # --- internal fulfill 200 → Order ---
    path, method = "/internal/v1/orders/{order_id}/fulfill", "post"
    ful = client.post(f"/internal/v1/orders/{order_id}/fulfill", headers=ih)
    fulfilled = assert_response_matches_schema(
        pilot_spec, path, method, ful, context=_ctx(method, path)
    )
    assert ful.status_code == 200, f"fulfill expected 200, got {ful.status_code}: {ful.text}"
    assert isinstance(fulfilled, dict)

    # --- aftersales/candidates 200 ---
    path, method = "/api/v1/aftersales/candidates", "post"
    cand = client.post(
        path,
        headers=headers,
        json={"text": "热水器漏水了要退货", "category_hint": "HOME_APPLIANCE_HEATER"},
    )
    assert_response_matches_schema(
        pilot_spec, path, method, cand, context=_ctx(method, path)
    )
    assert cand.status_code == 200, (
        f"aftersales/candidates expected 200, got {cand.status_code}: {cand.text}"
    )

    # --- aftersales/confirm 202 ---
    path, method = "/api/v1/aftersales/confirm", "post"
    conf = client.post(
        path,
        headers=headers,
        json={
            "order_id": order_id,
            "order_item_id": order_item_id,
            "buyer_reason_text": "热水器漏水了，要求全额退货退款",
            "evidence_urls": ["https://storage.example.invalid/evidence/e1.jpg"],
        },
    )
    confirm_body = assert_response_matches_schema(
        pilot_spec, path, method, conf, context=_ctx(method, path)
    )
    assert conf.status_code == 202, (
        f"aftersales/confirm expected 202, got {conf.status_code}: {conf.text}"
    )
    assert isinstance(confirm_body, dict)
    claim_no = confirm_body["claim_no"]

    # --- internal refunds/execute 200 → RefundStatus ---
    path, method = "/internal/v1/refunds/execute", "post"
    refund_key = f"RFK_CONTRACT_{uuid.uuid4().hex}"
    approved = int(paid.get("total_payable_cents") or order["total_payable_cents"])
    exe = client.post(
        path,
        headers=ih,
        json={
            "refund_idempotency_key": refund_key,
            "claim_no": claim_no,
            "order_id": order_id,
            "order_item_id": order_item_id,
            "approved_refund_cents": approved,
            "restock_policy": "DEFECTIVE_WAREHOUSE",
            "approval_mode": "SUPERVISOR",
            "approved_at": "2026-01-01T00:00:00Z",
        },
    )
    refund_status = assert_response_matches_schema(
        pilot_spec, path, method, exe, context=_ctx(method, path)
    )
    assert exe.status_code == 200, (
        f"refunds/execute expected 200, got {exe.status_code}: {exe.text}"
    )
    assert isinstance(refund_status, dict)

    # --- refunds/status SUCCESS ---
    path, method = "/internal/v1/refunds/status", "get"
    st_ok = client.get(
        path,
        headers=ih,
        params={"refund_idempotency_key": refund_key},
    )
    status_ok = assert_response_matches_schema(
        pilot_spec, path, method, st_ok, context=_ctx(method, path, "SUCCESS")
    )
    assert st_ok.status_code == 200, (
        f"refunds/status SUCCESS expected 200, got {st_ok.status_code}: {st_ok.text}"
    )
    assert isinstance(status_ok, dict)
    assert status_ok.get("execution_status") == "SUCCESS", (
        f"refunds/status expected execution_status=SUCCESS, got {status_ok!r}"
    )

    # --- refunds/status NOT_FOUND (仍为 200) ---
    missing_key = f"RFK_MISSING_{uuid.uuid4().hex}"
    st_nf = client.get(
        path,
        headers=ih,
        params={"refund_idempotency_key": missing_key},
    )
    status_nf = assert_response_matches_schema(
        pilot_spec, path, method, st_nf, context=_ctx(method, path, "NOT_FOUND")
    )
    assert st_nf.status_code == 200, (
        f"refunds/status NOT_FOUND expected 200, got {st_nf.status_code}: {st_nf.text}"
    )
    assert isinstance(status_nf, dict)
    assert status_nf.get("execution_status") == "NOT_FOUND", (
        f"refunds/status expected execution_status=NOT_FOUND, got {status_nf!r}"
    )


def test_contract_cancel(client: httpx.Client, pilot_spec):
    """独立 STOCK_LOCKED 订单走 cancel → Order。"""
    headers = bearer(USER_A)
    set_stock(SKU_STOCK_TEST, 10)

    q = client.post(
        "/api/v1/quotes",
        headers=headers,
        json={"items": [{"sku_id": SKU_STOCK_TEST, "qty": 1}]},
    )
    assert q.status_code == 201, q.text
    quote_id = q.json()["quote_id"]

    c = client.post(
        "/api/v1/orders/create",
        headers=headers,
        json={"idempotency_key": f"IDK_CANCEL_{uuid.uuid4().hex}", "quote_id": quote_id},
    )
    assert c.status_code == 201, c.text
    order_id = c.json()["order_id"]

    path, method = "/api/v1/orders/{order_id}/cancel", "post"
    r = client.post(f"/api/v1/orders/{order_id}/cancel", headers=headers)
    body = assert_response_matches_schema(
        pilot_spec, path, method, r, context=_ctx(method, path)
    )
    assert r.status_code == 200, f"cancel expected 200, got {r.status_code}: {r.text}"
    assert isinstance(body, dict)


def test_contract_error_401_missing_token(client: httpx.Client, pilot_spec, error_codes):
    path, method = "/api/v1/quotes", "post"
    r = client.post(path, json={"items": [{"sku_id": SKU_HEATER, "qty": 1}]})
    assert r.status_code == 401, f"missing token expected 401, got {r.status_code}: {r.text}"
    assert_error_body(
        pilot_spec,
        path,
        method,
        r,
        error_codes,
        context=_ctx(method, path, "401 missing token"),
        expected_code="UNAUTHORIZED",
    )


def test_contract_error_404_order_not_found(client: httpx.Client, pilot_spec, error_codes):
    headers = bearer(USER_A)
    path, method = "/api/v1/orders/{order_id}", "get"
    missing = f"ORD_NOT_EXIST_{uuid.uuid4().hex}"
    r = client.get(f"/api/v1/orders/{missing}", headers=headers)
    assert r.status_code == 404, f"missing order expected 404, got {r.status_code}: {r.text}"
    assert_error_body(
        pilot_spec,
        path,
        method,
        r,
        error_codes,
        context=_ctx(method, path, "404 not found"),
        expected_code="NOT_FOUND",
    )


def test_contract_error_422_insufficient_stock(client: httpx.Client, pilot_spec, error_codes):
    headers = bearer(USER_A)
    set_stock(SKU_STOCK_TEST, 10)
    q = client.post(
        "/api/v1/quotes",
        headers=headers,
        json={"items": [{"sku_id": SKU_STOCK_TEST, "qty": 20}]},
    )
    assert q.status_code == 201, f"quote for oversell setup failed: {q.status_code} {q.text}"
    quote_id = q.json()["quote_id"]

    path, method = "/api/v1/orders/create", "post"
    r = client.post(
        path,
        headers=headers,
        json={
            "idempotency_key": f"IDK_OOS_{uuid.uuid4().hex}",
            "quote_id": quote_id,
        },
    )
    assert r.status_code == 422, (
        f"insufficient stock expected 422, got {r.status_code}: {r.text}"
    )
    assert_error_body(
        pilot_spec,
        path,
        method,
        r,
        error_codes,
        context=_ctx(method, path, "422 insufficient stock"),
        expected_code="INSUFFICIENT_STOCK",
    )


def test_contract_error_409_expired_quote(client: httpx.Client, pilot_spec, error_codes):
    headers = bearer(USER_A)
    set_stock(SKU_STOCK_TEST, 10)
    q = client.post(
        "/api/v1/quotes",
        headers=headers,
        json={"items": [{"sku_id": SKU_STOCK_TEST, "qty": 1}]},
    )
    assert q.status_code == 201, q.text
    quote_id = q.json()["quote_id"]
    expire_quote(quote_id)

    path, method = "/api/v1/orders/create", "post"
    r = client.post(
        path,
        headers=headers,
        json={
            "idempotency_key": f"IDK_EXPIRED_{uuid.uuid4().hex}",
            "quote_id": quote_id,
        },
    )
    assert r.status_code == 409, (
        f"expired quote expected 409, got {r.status_code}: {r.text}"
    )
    assert_error_body(
        pilot_spec,
        path,
        method,
        r,
        error_codes,
        context=_ctx(method, path, "409 expired quote"),
        expected_code="QUOTE_EXPIRED_OR_CHANGED",
    )
