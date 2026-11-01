import importlib
import json
from datetime import datetime, timedelta
from hashlib import sha1

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.core.auth import issue_token
from pilot.core.config import get_settings
from pilot.core.db import get_engine
from pilot.core.errors import install_error_handlers

module = importlib.import_module("pilot.aftersales.router")


@pytest.fixture
def setup(db_name, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "nlu_url", "http://nlu.test")
    monkeypatch.setattr(settings, "ops_url", "http://ops.test")
    calls = []
    faults = {"ops": None, "nlu": None}
    signal = {"clauses": [
        {"predicted_label": "low", "category_confidence": .2},
        {"predicted_label": "热水器", "category_confidence": .98, "catalog_category_hint": "heater"}],
        "sentiment": "NEGATIVE", "sentiment_score": .96, "priority_boost": True}
    real_client = httpx.Client

    def respond(request):
        service = request.url.host.split('.')[0]
        calls.append((service, json.loads(request.content), request.extensions["timeout"]))
        if faults[service] == "timeout":
            raise httpx.ReadTimeout("injected", request=request)
        if faults[service] == "malformed":
            return httpx.Response(200, json={"clauses": None})
        return httpx.Response(503 if faults[service] == "error" else 202 if service == "ops" else 200,
                              json=signal if service == "nlu" else {})

    monkeypatch.setattr(module.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    app = FastAPI()
    app.include_router(module.router)
    install_error_handlers(app)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + issue_token("buyer", "tenant")
    yield client, calls, faults
    client.close()


def insert(order="one", status="FULFILLED", user="buyer", tenant="tenant", category="heater", age=1):
    now = datetime.now()
    with get_engine().begin() as conn:
        conn.execute(text("""INSERT INTO orders
            (order_id,idempotency_key,quote_id,tenant_id,user_id,total_original_cents,
             total_discount_cents,total_payable_cents,order_status,created_at,fulfilled_at,pay_deadline_at)
            VALUES (:id,:id,'quote',:tenant,:user,12000,1000,11000,:status,:created,:fulfilled,:now)"""),
            {"id": order, "tenant": tenant, "user": user, "status": status,
             "created": now - timedelta(days=age), "fulfilled": now - timedelta(days=5, hours=2), "now": now})
        conn.execute(text("""INSERT INTO order_items
            (order_item_id,order_id,sku_id,shop_id,catalog_category_id,sku_name,qty,
             unit_price_cents,original_cents,discount_share_cents,payable_cents,refunded_cents)
            VALUES (:item,:id,'sku','shop',:category,'商品',2,6000,12000,1000,11000,123)"""),
            {"item": order + "-item", "id": order, "category": category})


def confirm(client, order="one", item=None):
    return client.post("/api/v1/aftersales/confirm", json={
        "order_id": order, "order_item_id": item or order + "-item",
        "buyer_reason_text": "漏水", "evidence_urls": ["https://evidence.test/1"],
        "user_id": "forged", "tenant_id": "forged"})


def snapshot():
    with get_engine().connect() as conn:
        tables = [row[0] for row in conn.execute(text("SHOW TABLES"))]
        return {table: conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one() for table in tables}


def test_candidates_scope_sort_and_readonly(setup):
    client, calls, _ = setup
    insert("old", age=4)
    insert("new", status="PAID_SIMULATED", category="milk", age=1)
    insert("other", user="other")
    insert("other-tenant", tenant="other")
    insert("closed", status="CANCELLED")
    before = snapshot()
    plain = client.post("/api/v1/aftersales/candidates", json={}).json()
    assert [o["order_id"] for o in plain["candidates"]] == ["new", "old"]
    response = client.post("/api/v1/aftersales/candidates", json={"text": "漏水", "user_id": "other", "tenant_id": "other"})
    assert response.status_code == 200
    data = response.json()
    assert [o["order_id"] for o in data["candidates"]] == ["old", "new"]
    assert [o["category_match"] for o in data["candidates"]] == [True, False]
    assert data["candidates"][0]["items"][0]["payable_cents"] == 11000
    assert data["nlu_signal"]["predicted_label"] == "热水器"
    assert calls[0][2]["read"] == 2
    override = client.post("/api/v1/aftersales/candidates", json={"text": "漏水", "category_hint": "milk"}).json()
    assert override["candidates"][0]["order_id"] == "new"
    assert snapshot() == before


@pytest.mark.parametrize("fault", ["timeout", "error", "malformed"])
def test_nlu_failure_degrades(setup, fault):
    client, _, faults = setup
    insert()
    faults["nlu"] = fault
    before = snapshot()
    result = client.post("/api/v1/aftersales/candidates", json={"text": "reason"})
    assert result.status_code == 200
    assert result.json()["nlu_signal"] is None
    assert snapshot() == before


@pytest.mark.parametrize("kind", ["other", "missing", "tenant", "item"])
def test_confirm_not_found(setup, kind):
    client, calls, _ = setup
    insert(user="other" if kind == "other" else "buyer", tenant="other" if kind == "tenant" else "tenant")
    if kind == "item":
        insert("different")
    result = confirm(client, "missing" if kind == "missing" else "one", "different-item" if kind == "item" else None)
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "NOT_FOUND"
    assert calls == []


@pytest.mark.parametrize("status", ["PAID_SIMULATED", "CANCELLED", "REFUNDED_FULL"])
def test_confirm_invalid_state(setup, status):
    client, calls, _ = setup
    insert(status=status)
    result = confirm(client)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "INVALID_ORDER_STATE"
    assert calls == []


def test_success_payload_and_duplicate(setup):
    client, calls, _ = setup
    insert()
    result = confirm(client)
    claim = "CLM_" + sha1(b"one:one-item").hexdigest()[:12].upper()
    assert result.status_code == 202
    assert result.json() == {"claim_no": claim, "claim_status": "SUBMITTED", "order_status": "AFTER_SALE_OPEN"}
    payload = next(body for service, body, _ in calls if service == "ops")
    assert payload == {
        "claim_no": claim, "tenant_id": "tenant", "user_id": "buyer", "order_id": "one",
        "order_item_id": "one-item", "sku_id": "sku", "catalog_category_id": "heater",
        "item_payable_cents": 11000, "max_refundable_cents": 10877, "days_since_delivery": 5,
        "claim_type": "QUALITY_REFUND", "buyer_reason_text": "漏水", "evidence_urls": ["https://evidence.test/1"],
        "nlu_signal": {"predicted_label": "热水器", "category_confidence": .98, "sentiment": "NEGATIVE",
                       "sentiment_score": .96, "priority_boost": True}}
    assert calls[-1][2]["read"] == 3
    assert confirm(client).status_code == 409
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT claim_status FROM aftersale_requests")).scalar_one() == "SUBMITTED"
        # Exercise the item uniqueness guard independently of the order state guard.
        conn.execute(text("UPDATE orders SET order_status='FULFILLED'"))
    assert confirm(client).status_code == 409


@pytest.mark.parametrize("fault,status,code", [("error", 502, "UPSTREAM_ERROR"), ("timeout", 504, "UPSTREAM_TIMEOUT")])
def test_ops_compensation_and_safe_retry(setup, fault, status, code):
    client, calls, faults = setup
    insert()
    faults.update(ops=fault, nlu="error")
    result = confirm(client)
    assert result.status_code == status
    assert result.json()["error"]["code"] == code
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT order_status FROM orders WHERE order_id='one'")).scalar_one() == "FULFILLED"
        assert conn.execute(text("SELECT COUNT(*) FROM aftersale_requests")).scalar_one() == 0
    first = calls[-1][1]
    assert first["nlu_signal"] == {"predicted_label": "ABSTAIN", "category_confidence": 0,
                                   "sentiment": "NEUTRAL", "sentiment_score": 0, "priority_boost": False}
    faults["ops"] = None
    assert confirm(client).status_code == 202
    assert calls[-1][1]["claim_no"] == first["claim_no"]


def test_candidates_without_body_and_auth(setup):
    client, calls, _ = setup
    insert()
    assert client.post("/api/v1/aftersales/candidates").status_code == 200
    assert calls == []
    client.headers.pop("Authorization")
    assert client.post("/api/v1/aftersales/candidates").status_code == 401
