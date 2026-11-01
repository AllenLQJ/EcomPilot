"""A2A contract checks; Pilot and NLU calls never require live services."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.agents import aftersales_agent, pricing_order_agent, selection_agent
from pilot.core.db import get_engine


AGENTS = [(selection_agent, "selection", 8012),
          (pricing_order_agent, "pricing_order", 8013),
          (aftersales_agent, "aftersales", 8014)]


def invoke(module, data):
    return TestClient(module.app).post("/a2a/invoke", json={"trace_id": "trace-test", "input": data})


@pytest.mark.parametrize("module,name,port", AGENTS)
def test_discovery(module, name, port):
    client = TestClient(module.app)
    assert client.get("/health").json() == {"status": "ok", "agent": name}
    card = client.get("/.well-known/agent.json").json()
    assert card["name"] == name and card["port"] == port and card["skills"]
    assert invoke(module, {}).status_code == 400


def test_pricing_and_addons(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(201, json={"quote_id": "Q2", "total_payable_cents": 5100})
    monkeypatch.setattr(httpx, "post", post)
    for base in ["http://first", "http://second/"]:
        monkeypatch.setenv("ECOMPILOT_PILOT_URL", base)
        result = invoke(pricing_order_agent, {"auth_token": "raw-jwt", "items": [{"sku_id": "SKU", "qty": 1}],
                                             "coupon_ids": ["CPN"], "user_id": "spoof", "tenant_id": "spoof"})
        assert result.json()["output"]["quote"]["total_payable_cents"] == 5100
        assert calls[-1][0] == base.rstrip("/") + "/api/v1/quotes"
        assert calls[-1][1]["json"] == {"items": [{"sku_id": "SKU", "qty": 1}], "coupon_ids": ["CPN"]}
        assert calls[-1][1]["headers"] == {"Authorization": "Bearer raw-jwt"}
    invoke(pricing_order_agent, {"auth_token": "jwt", "quote_id": "Q1", "addon_sku_ids": ["ADDON"]})
    assert calls[-1][0].endswith("/quotes/Q1/confirm-addons")
    assert calls[-1][1]["json"] == {"addon_sku_ids": ["ADDON"]}


def test_aftersales_candidates_never_confirm(monkeypatch):
    def post(url, **kwargs):
        assert url == "http://pilot/api/v1/aftersales/candidates"
        assert kwargs["json"] == {"text": "坏了", "category_hint": "DIGITAL_TABLET"}
        assert kwargs["headers"] == {"Authorization": "Bearer jwt"}
        return httpx.Response(200, json={"candidates": [{"order_id": "O"}], "nlu_signal": None})
    monkeypatch.setenv("ECOMPILOT_PILOT_URL", "http://pilot")
    monkeypatch.setattr(httpx, "post", post)
    result = invoke(aftersales_agent, {"auth_token": "jwt", "action": "candidates", "text": "坏了",
                                     "category_hint": "DIGITAL_TABLET", "order_id": "O", "order_item_id": "I",
                                     "user_id": "spoof", "tenant_id": "spoof"}).json()
    assert result["output"] == {"candidates": [{"order_id": "O"}], "nlu_signal": None}


@pytest.mark.parametrize("ids", [{}, {"order_id": "O"}, {"order_item_id": "I"},
                                 {"order_id": " ", "order_item_id": "I"}])
def test_confirm_requires_both_ids(monkeypatch, ids):
    def post(*args, **kwargs):
        pytest.fail("incomplete confirmation must not contact Pilot")
    monkeypatch.setattr(httpx, "post", post)
    result = invoke(aftersales_agent, {"auth_token": "jwt", "action": "confirm", **ids}).json()
    assert result["status"] == "error" and result["error"]["code"] == "INVALID_REQUEST"


def test_confirm(monkeypatch):
    def post(url, **kwargs):
        assert url.endswith("/aftersales/confirm")
        assert kwargs["json"] == {"order_id": "O", "order_item_id": "I", "buyer_reason_text": "坏了", "evidence_urls": ["https://evidence"]}
        assert kwargs["headers"] == {"Authorization": "Bearer jwt"}
        return httpx.Response(202, json={"claim_no": "C", "claim_status": "SUBMITTED"})
    monkeypatch.setattr(httpx, "post", post)
    assert invoke(aftersales_agent, {"auth_token": "jwt", "action": "confirm", "order_id": "O", "order_item_id": "I",
                                  "buyer_reason_text": "坏了", "evidence_urls": ["https://evidence"]}).json()["output"]["claim_no"] == "C"


@pytest.mark.parametrize("module,data", [
    (pricing_order_agent, {"auth_token": "jwt", "items": [{"sku_id": "S", "qty": 1}]}),
    (aftersales_agent, {"auth_token": "jwt", "action": "candidates"}),
    (aftersales_agent, {"auth_token": "jwt", "action": "confirm", "order_id": "O", "order_item_id": "I"})])
@pytest.mark.parametrize("status,code", [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND"),
                                       (409, "QUOTE_EXPIRED_OR_CHANGED"), (422, "INVALID_ADDON_SELECTION"), (502, "UPSTREAM_ERROR")])
def test_error_translation(monkeypatch, module, data, status, code):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(status, json={"error": {"code": code, "message": "message"}}))
    assert invoke(module, data).json() == {"status": "error", "error": {"code": code, "message": "message"}}


@pytest.mark.parametrize("module,data", [(pricing_order_agent, {"auth_token": "jwt", "items": [{"sku_id": "S", "qty": 1}]}),
                                        (aftersales_agent, {"auth_token": "jwt", "action": "candidates"})])
@pytest.mark.parametrize("exception,code", [(httpx.ConnectError("offline"), "UPSTREAM_ERROR"),
                                          (httpx.ReadTimeout("slow"), "UPSTREAM_TIMEOUT")])
def test_transport_errors(monkeypatch, module, data, exception, code):
    def post(*a, **kw):
        raise exception
    monkeypatch.setattr(httpx, "post", post)
    assert invoke(module, data).json()["error"]["code"] == code


@pytest.mark.parametrize("unavailable", [True, False])
def test_selection_seed(db_name, monkeypatch, unavailable):
    def post(url, **kwargs):
        assert url.endswith("/api/v1/nlu/analyze") and kwargs["timeout"] == 2.0
        if unavailable:
            raise httpx.ConnectError("NLU offline")
        # An unrelated hint must never exclude the actual keyword match.
        return httpx.Response(200, json={"clauses": [{"predicted_label": "平板", "catalog_category_hint": "DIGITAL_TABLET"}]})
    monkeypatch.setattr(httpx, "post", post)
    result = invoke(selection_agent, {"tenant_id": "SHOP_SELF_01", "needs": [{"text": "洗发水"}, {"text": "平板"}]}).json()
    assert result["status"] == "ok"
    rows = result["output"]["candidates"]
    assert [(r["need_index"], r["sku_id"]) for r in rows] == [(0, "SKU_SHAMPOO_001"), (1, "SKU_TABLET_001")]
    assert all(isinstance(r["unit_price_cents"], int) and r["sku_name"] in r["reviews"][0] for r in rows)
    assert invoke(selection_agent, {"tenant_id": "other", "needs": [{"text": "洗发水"}]}).json()["output"]["candidates"] == []


def test_selection_filters_and_top_three(db_name, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={"clauses": [{"predicted_label": "ABSTAIN", "catalog_category_hint": "DIGITAL_TABLET"}]}))
    with get_engine().begin() as conn:
        for index in range(5):
            conn.execute(text("INSERT INTO products_sku (sku_id, tenant_id, shop_id, catalog_category_id, sku_name, unit_price_cents, sellable_stock, positive_review_ratio, is_addon_pool) VALUES (:id, 'SHOP_SELF_01', 'SHOP_C', 'PERSONAL_SHAMPOO', '洗发水测试', 100, :stock, :ratio, :addon)"),
                         {"id": f"TEST_{index}", "stock": 0 if index == 0 else 10, "ratio": .999 if index < 2 else .99 - index / 100, "addon": index == 1})
    rows = invoke(selection_agent, {"tenant_id": "SHOP_SELF_01", "needs": [{"text": "洗发水", "budget_cents": 100}]}).json()["output"]["candidates"]
    assert [r["sku_id"] for r in rows] == ["TEST_2", "TEST_3", "TEST_4"]


@pytest.mark.parametrize("module,name,port", AGENTS)
def test_invalid_envelope(module, name, port):
    response = TestClient(module.app).post("/a2a/invoke", json={"input": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("module,data", [
    (pricing_order_agent, {"auth_token": "jwt", "items": [{"sku_id": "S", "qty": 1}]}),
    (aftersales_agent, {"auth_token": "jwt", "action": "candidates"})])
@pytest.mark.parametrize("status,body", [(502, None), (503, {"error": None}), (200, [])])
def test_malformed_upstream(monkeypatch, module, data, status, body):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(status, json=body) if body is not None
                        else httpx.Response(status, text="bad gateway"))
    assert invoke(module, data).json()["error"]["code"] == "UPSTREAM_ERROR"
