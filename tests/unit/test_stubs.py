from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from threading import Barrier, Lock

import httpx
import pytest
from fastapi.testclient import TestClient

from stubs import nlu_stub, ops_stub, rag_stub


@pytest.mark.parametrize("app", [nlu_stub.app, rag_stub.app, ops_stub.app])
def test_health(app):
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def analyze(text, **kwargs):
    return TestClient(nlu_stub.app).post("/api/v1/nlu/analyze", json={"text": text, **kwargs}).json()


def test_nlu_negative_and_abstain():
    result = analyze("热水器漏水")
    assert result == dict(clauses=[dict(text="热水器漏水", predicted_label="热水器",
        category_confidence=0.98, catalog_category_hint="HOME_APPLIANCE_HEATER",
        brand_hint=None, item_type="PRODUCT")], sentiment="NEGATIVE",
        sentiment_score=0.96, priority_boost=True, scene_not_applicable=False)
    result = analyze("今天天气晴朗")
    assert result["clauses"][0]["predicted_label"] == "ABSTAIN"
    assert result["clauses"][0]["category_confidence"] == 0.30
    assert result["clauses"][0]["catalog_category_hint"] is None
    assert (result["sentiment"], result["sentiment_score"], result["priority_boost"]) == ("POSITIVE", 0.60, False)


@pytest.mark.parametrize("scene", ["PDF_BODY", "CS_APOLOGY"])
def test_nlu_scene_guard(scene):
    assert analyze("热水器漏水", scene_type=scene) == dict(clauses=[], sentiment="NEUTRAL",
        sentiment_score=0, priority_boost=False, scene_not_applicable=True)


@pytest.mark.parametrize("word", ["漏水", "打不着火", "坑", "退货", "退款", "坏了", "差评"])
def test_nlu_negative_words_and_scene_priority(word):
    result = analyze(word, scene_type="SHOPPING_QUERY")
    assert result["sentiment"] == "NEGATIVE"
    assert result["sentiment_score"] == 0.96
    assert result["priority_boost"] is False


def test_nlu_all_clause_separators():
    clauses = analyze("热水器，平板。洗发水！牛奶？蒙牛；酒店和书以及未知顺便平板还要酒店！")["clauses"]
    assert [c["predicted_label"] for c in clauses] == ["热水器", "平板", "洗发水", "蒙牛", "蒙牛", "酒店", "书籍", "ABSTAIN", "平板", "酒店"]
    assert [c["catalog_category_hint"] for c in clauses[:7]] == ["HOME_APPLIANCE_HEATER", "DIGITAL_TABLET", "PERSONAL_SHAMPOO", "DAIRY_MILK", "DAIRY_MILK", "TRAVEL_HOTEL", "BOOKS"]
    assert clauses[3]["brand_hint"] == "蒙牛"
    assert clauses[5]["item_type"] == "SERVICE"


def test_rag_demo_and_truncation():
    client = TestClient(rag_stub.app)
    def search(**kwargs):
        return client.post("/api/v1/rag/search", json={"query": "退货", **kwargs}).json()["hits"]
    hits = search(catalog_category_id="HOME_APPLIANCE_HEATER")
    assert hits[0]["score"] == 0.9
    assert hits[0]["rule_source"] == "热水器整机保修与漏水退换细则"
    assert all(h["is_demo_rule"] for h in hits)
    assert hits == sorted(hits, key=lambda h: h["score"], reverse=True)
    assert search(catalog_category_id="HOME_APPLIANCE_HEATER", top_k=1) == hits[:1]
    assert search(top_k=0) == []
    assert "7 天" in search(catalog_category_id="BOOKS")[0]["text"]


@pytest.fixture
def ops(monkeypatch):
    with ops_stub.state_lock:
        ops_stub.claims.clear()
        ops_stub.pending.clear()
        ops_stub.fault_mode = "none"
    calls = []
    lock = Lock()
    real_client = httpx.Client
    def handle(request):
        with lock:
            calls.append(request)
            count = len(calls)
        return httpx.Response(200, json={"execution_status": "SUCCESS", "call": count})
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(ops_stub.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    # TestClient uses its own Client subclass, unaffected by the factory replacement.
    client = TestClient(ops_stub.app)
    claim = dict(claim_no="CLM_1", tenant_id="T", user_id="U", order_id="ORD_1",
                 order_item_id="ITEM_1", sku_id="SKU_1", max_refundable_cents=12345)
    assert client.post("/internal/v1/claims/submit", json=claim).status_code == 202
    return client, claim, calls


def test_claim_idempotency_and_fault(ops):
    client, claim, _ = ops
    response = client.post("/internal/v1/claims/submit", json={**claim, "order_id": "OTHER"})
    assert response.status_code == 202
    assert response.json() == {"claim_no": "CLM_1", "status": "SUBMITTED"}
    assert client.get("/stub/admin/claims").json() == {"items": [claim]}
    client.post("/stub/admin/fault", json={"mode": "submit_fail"})
    assert client.post("/internal/v1/claims/submit", json=claim).status_code == 503
    assert client.post("/stub/admin/fault", json={"mode": "invalid"}).status_code == 422
    client.post("/stub/admin/fault", json={"mode": "none"})
    assert client.post("/internal/v1/claims/submit", json=claim).status_code == 202


@pytest.mark.parametrize("mode,count", [("none", 1), ("duplicate_callback", 2), ("refund_callback_timeout", 1)])
def test_approve_modes(ops, monkeypatch, mode, count):
    client, claim, calls = ops
    monkeypatch.setenv("PILOT_BASE_URL", "http://fake-pilot/runtime/")
    monkeypatch.setenv("PILOT_INTERNAL_TOKEN", "runtime-token")
    client.post("/stub/admin/fault", json={"mode": mode})
    result = client.post("/stub/admin/approve", json={"claim_no": claim["claim_no"]})
    assert len(calls) == count
    assert str(calls[0].url) == "http://fake-pilot/runtime/internal/v1/refunds/execute"
    assert calls[0].headers["X-Internal-Token"] == "runtime-token"
    payload = json.loads(calls[0].content)
    datetime.fromisoformat(payload.pop("approved_at"))
    assert payload == dict(refund_idempotency_key="REFUND_CLM_1_V1", claim_no="CLM_1",
        order_id="ORD_1", order_item_id="ITEM_1", approved_refund_cents=12345,
        restock_policy="DEFECTIVE_WAREHOUSE", approval_mode="SUPERVISOR_CONFIRMED", supervisor_user_id="SUP_007")
    items = client.get("/stub/admin/pending").json()["items"]
    if mode == "refund_callback_timeout":
        assert result.status_code == 504
        assert result.json() == {"detail": "callback read timeout (injected)"}
        assert items == [{"refund_idempotency_key": "REFUND_CLM_1_V1", "execute_payload": json.loads(calls[0].content)}]
    else:
        assert result.status_code == 200
        assert result.json() == {"status_code": 200, "response": {"execution_status": "SUCCESS", "call": count}}
        assert items == []
    if mode == "duplicate_callback":
        assert calls[0].content == calls[1].content


def test_concurrent_timeouts_each_call_pilot_but_deduplicate_pending(ops):
    client, _, calls = ops
    client.post("/stub/admin/fault", json={"mode": "refund_callback_timeout"})
    barrier = Barrier(4)
    def approve(_):
        barrier.wait(timeout=5)
        return client.post("/stub/admin/approve", json={"claim_no": "CLM_1"}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(approve, range(4))) == [504] * 4
    assert len(calls) == 4
    assert len(client.get("/stub/admin/pending").json()["items"]) == 1
    for _ in range(2):
        assert client.post("/stub/admin/reconcile_done", json={"refund_idempotency_key": "REFUND_CLM_1_V1"}).status_code == 200
    assert client.get("/stub/admin/pending").json() == {"items": []}


def test_approve_override_and_missing_claim(ops, monkeypatch):
    client, _, calls = ops
    monkeypatch.delenv("PILOT_INTERNAL_TOKEN", raising=False)
    assert client.post("/stub/admin/approve", json={"claim_no": "missing"}).status_code == 404
    assert calls == []
    assert client.post("/stub/admin/approve", json={"claim_no": "CLM_1", "approved_refund_cents": 100}).status_code == 200
    assert json.loads(calls[0].content)["approved_refund_cents"] == 100
    assert calls[0].headers["X-Internal-Token"] == "dev-internal-token"
    assert client.post("/stub/admin/approve", json={"claim_no": "CLM_1", "approved_refund_cents": 1.5}).status_code == 422
