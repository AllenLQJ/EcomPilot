import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.aftersales import router as module
from pilot.core.auth import issue_token
from pilot.core.config import get_settings
from pilot.core.db import get_engine
from pilot.core.errors import install_error_handlers


@pytest.fixture
def setup(db_name, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ops_url", "http://ops.test")
    calls, behaviour = [], {"status": {"claim_status": "NEED_MORE_EVIDENCE", "needs_evidence": True},
                            "evidence": (202, {"claim_no": "CLM1", "status": "SUBMITTED"}), "fault": None}
    real_client = httpx.Client

    def respond(request):
        calls.append((request.method, request.url.path, request.headers.get("x-internal-token"),
                      json.loads(request.content) if request.content else None))
        if behaviour["fault"] == "timeout":
            raise httpx.ReadTimeout("injected", request=request)
        if behaviour["fault"] == "down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"claim_no": "CLM1", "audit_round": 1, "evidence_count": 1, **behaviour["status"]})
        code, body = behaviour["evidence"]
        return httpx.Response(code, json=body)

    monkeypatch.setattr(module.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    now = datetime.now()
    with get_engine().begin() as conn:
        conn.execute(text("""INSERT INTO orders (order_id,idempotency_key,quote_id,tenant_id,user_id,total_original_cents,
            total_discount_cents,total_payable_cents,order_status,created_at,fulfilled_at,pay_deadline_at)
            VALUES ('O1','O1','q','tenant','buyer',100,0,100,'AFTER_SALE_OPEN',:n,:n,:n)"""), {"n": now - timedelta(days=1)})
        conn.execute(text("""INSERT INTO aftersale_requests (claim_no,tenant_id,user_id,order_id,order_item_id,claim_status,
            buyer_reason_text,created_at) VALUES ('CLM1','tenant','buyer','O1','O1-item','SUBMITTED','漏水',:n)"""), {"n": now})
    app = FastAPI()
    app.include_router(module.router)
    install_error_handlers(app)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + issue_token("buyer", "tenant")
    yield client, calls, behaviour
    client.close()


def other(client, user="stranger", tenant="tenant"):
    return {"Authorization": "Bearer " + issue_token(user, tenant)}


def test_status_shows_ops_state_and_needs_evidence(setup):
    client, calls, _ = setup
    r = client.get("/api/v1/aftersales/CLM1")
    assert r.status_code == 200
    assert r.json() == {"claim_no": "CLM1", "order_id": "O1", "order_status": "AFTER_SALE_OPEN",
                        "claim_status": "NEED_MORE_EVIDENCE", "needs_evidence": True, "ops_available": True,
                        "result_reason": None}
    assert calls[0][:3] == ("GET", "/internal/v1/claims/CLM1/status", get_settings().internal_token)


def test_status_falls_back_to_local_view_when_ops_down(setup):
    client, _, behaviour = setup
    behaviour["fault"] = "down"
    body = client.get("/api/v1/aftersales/CLM1").json()
    assert body["claim_status"] == "SUBMITTED" and body["ops_available"] is False and body["needs_evidence"] is False


@pytest.mark.parametrize("user,tenant", [("stranger", "tenant"), ("buyer", "other-tenant")])
def test_foreign_or_unknown_claim_is_404_and_ops_untouched(setup, user, tenant):
    client, calls, _ = setup
    headers = other(client, user, tenant)
    assert client.get("/api/v1/aftersales/CLM1", headers=headers).status_code == 404
    assert client.post("/api/v1/aftersales/CLM1/evidence", headers=headers, json={"evidence_urls": ["https://e.test/1"]}).status_code == 404
    assert client.get("/api/v1/aftersales/NOPE").status_code == 404
    assert calls == []


def test_evidence_forwarded_to_ops_with_internal_token(setup):
    client, calls, _ = setup
    r = client.post("/api/v1/aftersales/CLM1/evidence", json={"evidence_urls": ["https://e.test/a.jpg", "https://e.test/b.jpg"],
                                                              "user_id": "forged"})
    assert r.status_code == 202 and r.json() == {"claim_no": "CLM1", "claim_status": "SUBMITTED"}
    method, path, token, body = calls[0]
    assert (method, path, token) == ("POST", "/internal/v1/claims/CLM1/evidence", get_settings().internal_token)
    assert body == {"evidence_urls": ["https://e.test/a.jpg", "https://e.test/b.jpg"]}


@pytest.mark.parametrize("ops,fault,status,code", [
    ((409, {}), None, 409, "INVALID_ORDER_STATE"),
    ((404, {}), None, 404, "NOT_FOUND"),
    ((500, {}), None, 502, "UPSTREAM_ERROR"),
    ((202, {}), "timeout", 504, "UPSTREAM_TIMEOUT"),
    ((202, {}), "down", 502, "UPSTREAM_ERROR"),
])
def test_ops_errors_are_mapped(setup, ops, fault, status, code):
    client, _, behaviour = setup
    behaviour["evidence"], behaviour["fault"] = ops, fault
    r = client.post("/api/v1/aftersales/CLM1/evidence", json={"evidence_urls": ["https://e.test/1"]})
    assert r.status_code == status and r.json()["error"]["code"] == code


@pytest.mark.parametrize("urls", [[], ["ftp://e.test/1"], ["javascript:alert(1)"], ["https://e.test/" + "x" * 1100],
                                  ["https://e.test/%d" % i for i in range(11)]])
def test_invalid_evidence_rejected_without_calling_ops(setup, urls):
    client, calls, _ = setup
    r = client.post("/api/v1/aftersales/CLM1/evidence", json={"evidence_urls": urls})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST"
    assert calls == []
