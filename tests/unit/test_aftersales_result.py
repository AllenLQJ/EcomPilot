from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.aftersales.internal import router
from pilot.core.db import get_engine
from pilot.core.errors import install_error_handlers

H = {"X-Internal-Token": "dev-internal-token"}


def _setup(status="AFTER_SALE_OPEN", claim="SUBMITTED"):
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO orders (order_id,idempotency_key,quote_id,tenant_id,user_id,total_original_cents,total_discount_cents,"
                       "total_payable_cents,order_status,created_at,pay_deadline_at) VALUES ('o','k','q','t','u',100,0,100,:s,NOW(),NOW())"), {"s": status})
        c.execute(text("INSERT INTO aftersale_requests (claim_no,tenant_id,user_id,order_id,order_item_id,claim_status,created_at) "
                       "VALUES ('c','t','u','o','i',:s,NOW())"), {"s": claim})


def _client():
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


def test_rejected_restores_and_idempotent(db_name):
    _setup()
    c = _client()
    assert c.post("/internal/v1/aftersales/c/result", json={"result": "REJECTED"}, headers=H).status_code == 200
    assert c.post("/internal/v1/aftersales/c/result", json={"result": "REJECTED"}, headers=H).status_code == 200
    with get_engine().connect() as conn:
        assert conn.execute(text("select order_status from orders")).scalar() == "FULFILLED"
        assert conn.execute(text("select claim_status from aftersale_requests")).scalar() == "REJECTED"


def test_withdrawn_deletes_and_errors(db_name):
    _setup()
    c = _client()
    assert c.post("/internal/v1/aftersales/c/result", json={"result": "WITHDRAWN"}, headers=H).status_code == 200
    with get_engine().connect() as conn:
        assert conn.execute(text("select count(*) from aftersale_requests")).scalar() == 0
    assert c.post("/internal/v1/aftersales/c/result", json={"result": "WITHDRAWN"}, headers=H).status_code == 404
    assert c.post("/internal/v1/aftersales/c/result", json={"result": "X"}, headers=H).status_code == 400
    assert c.post("/internal/v1/aftersales/c/result", json={"result": "WITHDRAWN"}).status_code == 401


def test_completed_conflict(db_name):
    _setup(status="REFUNDED_FULL", claim="COMPLETED")
    assert _client().post("/internal/v1/aftersales/c/result", json={"result": "REJECTED"}, headers=H).status_code == 409
