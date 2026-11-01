import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.core.auth import issue_token
from pilot.core.db import get_engine


@pytest.fixture()
def client(db_name):
    from pilot.main import app
    with TestClient(app) as c:
        yield c


def H(user="USR_A"):
    return {"Authorization": f"Bearer {issue_token(user)}"}


def sql(_q, **p):
    with get_engine().begin() as c:
        r = c.execute(text(_q), p)
        return r.all() if r.returns_rows else None


def quote(client, items, user="USR_A", coupons=None):
    body = {"items": items}
    if coupons is not None:
        body["coupon_ids"] = coupons
    r = client.post("/api/v1/quotes", json=body, headers=H(user))
    assert r.status_code == 201, r.text
    return r.json()


def create(client, q, key, user="USR_A"):
    return client.post("/api/v1/orders/create", json={"idempotency_key": key, "quote_id": q["quote_id"]}, headers=H(user))


def stock(sku):
    return sql("select sellable_stock from products_sku where sku_id=:s", s=sku)[0][0]


def test_quote_math_and_no_identity_from_body(client):
    sql("delete from promo_rules")
    q = quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 3}])
    assert q["total_original_cents"] == 3000 and q["total_payable_cents"] == 3000
    r = client.post("/api/v1/quotes", json={"items": [{"sku_id": "SKU_STOCK_TEST", "qty": 1}], "user_id": "USR_B"},
                    headers=H("USR_A"))
    assert r.status_code == 201
    assert sql("select user_id from price_quotes where quote_id=:q", q=r.json()["quote_id"])[0][0] == "USR_A"


def test_create_idempotent_and_stock_once(client):
    sql("delete from promo_rules")
    q = quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 2}])
    with ThreadPoolExecutor(5) as ex:
        rs = list(ex.map(lambda _: create(client, q, "K1"), range(5)))
    assert sorted(r.status_code for r in rs) == [200, 200, 200, 200, 201]
    assert len({r.json()["order_id"] for r in rs}) == 1
    assert stock("SKU_STOCK_TEST") == 8
    assert create(client, quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 1}]), "K1").status_code == 409


def test_insufficient_vs_conflict(client):
    sql("delete from promo_rules")
    qa, qb = (quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 2}], u) for u in ("USR_A", "USR_B"))
    bar = threading.Barrier(2)

    def go(u, q):
        bar.wait()
        return create(client, q, "K-" + u, u)
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(go, ("USR_A", "USR_B"), (qa, qb)))
    assert [r.status_code for r in rs] == [201, 201], [r.text for r in rs]
    assert stock("SKU_STOCK_TEST") == 6
    r = create(client, quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 20}], "USR_B"), "K-big", "USR_B")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert stock("SKU_STOCK_TEST") == 6


def test_expired_quote_and_price_change(client):
    sql("delete from promo_rules")
    q = quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 1}])
    sql("update price_quotes set expires_at='2000-01-01 00:00:00' where quote_id=:q", q=q["quote_id"])
    r = create(client, q, "KE")
    assert r.status_code == 409 and r.json()["error"]["code"] == "QUOTE_EXPIRED_OR_CHANGED"
    assert r.json()["error"]["details"]["quote"]["quote_id"] != q["quote_id"]
    q2 = quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 1}])
    sql("update products_sku set unit_price_cents=1200 where sku_id='SKU_STOCK_TEST'")
    assert create(client, q2, "KP").json()["error"]["code"] == "QUOTE_EXPIRED_OR_CHANGED"
    assert stock("SKU_STOCK_TEST") == 10


def test_pay_cancel_idempotent_and_coupon_release(client):
    sql("delete from promo_rules")
    q = quote(client, [{"sku_id": "SKU_SHAMPOO_001", "qty": 1}], coupons=["CPN_A_001"])
    assert q["coupon_ids"] == ["CPN_A_001"] and q["total_discount_cents"] == 800
    o = create(client, q, "KC").json()
    assert o["order_status"] == "STOCK_LOCKED" and o["total_payable_cents"] == 5100
    assert sql("select status from user_coupons where user_coupon_id='CPN_A_001'")[0][0] == "LOCKED"
    base = stock("SKU_SHAMPOO_001")
    with ThreadPoolExecutor(3) as ex:
        rs = list(ex.map(lambda _: client.post(f"/api/v1/orders/{o['order_id']}/cancel", headers=H()), range(3)))
    assert [r.status_code for r in rs] == [200, 200, 200]
    assert stock("SKU_SHAMPOO_001") == base + 1
    assert sql("select status from user_coupons where user_coupon_id='CPN_A_001'")[0][0] == "UNUSED"
    assert client.post(f"/api/v1/orders/{o['order_id']}/pay", headers=H()).status_code == 409


def test_pay_fulfill_flow_and_cancel_after_pay(client):
    sql("delete from promo_rules")
    o = create(client, quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 1}]), "KF").json()
    assert client.post(f"/api/v1/orders/{o['order_id']}/pay", headers=H("USR_B")).status_code == 404
    p = client.post(f"/api/v1/orders/{o['order_id']}/pay", headers=H())
    assert p.json()["order_status"] == "PAID_SIMULATED"
    assert client.post(f"/api/v1/orders/{o['order_id']}/pay", headers=H()).status_code == 200
    assert client.post(f"/internal/v1/orders/{o['order_id']}/fulfill").status_code == 401
    f = client.post(f"/internal/v1/orders/{o['order_id']}/fulfill", headers={"X-Internal-Token": "dev-internal-token"})
    assert f.json()["order_status"] == "FULFILLED"
    assert client.post(f"/api/v1/orders/{o['order_id']}/cancel", headers=H()).status_code == 409
    o2 = create(client, quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 1}]), "KF2").json()
    client.post(f"/api/v1/orders/{o2['order_id']}/pay", headers=H())
    c = client.post(f"/api/v1/orders/{o2['order_id']}/cancel", headers=H())
    assert c.json()["order_status"] == "REFUNDED_FULL" and c.json()["refunded_cents"] == 1000
    assert client.post(f"/api/v1/orders/{o2['order_id']}/cancel", headers=H()).status_code == 200


def test_unpaid_timeout_expires_once(client):
    from pilot.orders import service
    sql("delete from promo_rules")
    o = create(client, quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 4}]), "KT").json()
    sql("update orders set pay_deadline_at='2000-01-01 00:00:00' where order_id=:o", o=o["order_id"])
    assert client.post(f"/api/v1/orders/{o['order_id']}/pay", headers=H()).status_code == 409
    assert service.expire_unpaid_orders(get_engine()) == 0
    assert stock("SKU_STOCK_TEST") == 10
    assert client.get(f"/api/v1/orders/{o['order_id']}", headers=H()).json()["order_status"] == "CANCELLED"


def test_auth_and_ownership(client):
    assert client.post("/api/v1/quotes", json={"items": [{"sku_id": "SKU_STOCK_TEST", "qty": 1}]}).status_code == 401
    o = create(client, quote(client, [{"sku_id": "SKU_STOCK_TEST", "qty": 1}]), "KO").json()
    assert client.get(f"/api/v1/orders/{o['order_id']}", headers=H("USR_B")).status_code == 404
    assert client.get("/api/v1/orders", headers=H("USR_B")).json()["orders"] == []


def test_addons_confirm(client):
    sql("update promo_rules set is_active=0 where rule_id<>'PROMO_100_10'")
    q = quote(client, [{"sku_id": "SKU_SHAMPOO_001", "qty": 1}, {"sku_id": "SKU_MILK_001", "qty": 1}], "USR_C")
    assert q["total_original_cents"] == 10400
    # 满 100 元(10000)已达标 → 下一档 gap 不存在于已停用规则
    q = quote(client, [{"sku_id": "SKU_SHAMPOO_001", "qty": 1}, {"sku_id": "SKU_ADDON_01", "qty": 8}], "USR_C")
    assert q["total_original_cents"] == 5900 + 7920 and q["promo_rule_id"] == "PROMO_100_10"
    r = client.post(f"/api/v1/quotes/{q['quote_id']}/confirm-addons", json={"addon_sku_ids": ["SKU_NOPE"]}, headers=H("USR_C"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_ADDON_SELECTION"
