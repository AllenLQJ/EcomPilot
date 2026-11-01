from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.core.config import get_settings
from pilot.core.db import get_engine
from pilot.core.errors import install_error_handlers
from pilot.refunds.router import router
from pilot.refunds.reconcile import reconcile_items


@pytest.fixture
def api(db_name):
    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("""INSERT INTO orders
            (order_id,idempotency_key,quote_id,tenant_id,user_id,total_original_cents,
             total_discount_cents,total_payable_cents,order_status,created_at,pay_deadline_at)
            VALUES ('o','idem','q','t','u',2000,0,2000,'FULFILLED',NOW(),NOW())"""))
        for index in (1, 2):
            c.execute(text("""INSERT INTO order_items
                (order_item_id,order_id,sku_id,shop_id,catalog_category_id,sku_name,qty,
                 unit_price_cents,original_cents,discount_share_cents,payable_cents)
                VALUES (:id,'o','SKU_STOCK_TEST','SHOP_A','DAILY_GENERAL','test',2,500,1000,0,1000)"""),
                      {'id': f'i{index}'})
        c.execute(text("""INSERT INTO aftersale_requests
            (claim_no,tenant_id,user_id,order_id,order_item_id,claim_status,created_at)
            VALUES ('claim','t','u','o','i1','SUBMITTED',NOW())"""))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    with TestClient(app, headers={'X-Internal-Token': get_settings().internal_token}) as client:
        yield client


def payload(**changes):
    return dict(refund_idempotency_key='r', claim_no='claim', order_id='o',
                approved_refund_cents=1000, restock_policy='NO_RESTOCK') | changes


def execute(api, **changes):
    return api.post('/internal/v1/refunds/execute', json=payload(**changes))


def scalar(sql):
    with get_engine().connect() as c:
        return c.execute(text(sql)).scalar_one()


def test_first_replay_status(api):
    first = execute(api, order_item_id='i1').json()
    assert first['execution_status'] == 'SUCCESS'
    assert first['order_status_after'] == 'REFUNDED_PARTIAL'
    assert first['idempotent_replayed'] is False
    replay = execute(api, order_item_id='i1').json()
    assert replay == first | {'idempotent_replayed': True}
    assert scalar("SELECT refunded_cents FROM orders WHERE order_id='o'") == 1000
    assert scalar("SELECT claim_status FROM aftersale_requests WHERE claim_no='claim'") == 'COMPLETED'
    assert api.get('/internal/v1/refunds/status', params={'refund_idempotency_key': 'r'}).json() == first
    missing = api.get('/internal/v1/refunds/status', params={'refund_idempotency_key': 'missing'})
    assert missing.status_code == 200
    assert missing.json()['execution_status'] == 'NOT_FOUND'
    assert missing.json()['refunded_cents'] == 0


def test_concurrent_same_key(api):
    barrier = Barrier(10)
    def call(_):
        barrier.wait()
        return execute(api, approved_refund_cents=2000, restock_policy='DEFECTIVE_WAREHOUSE')
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(call, range(10)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(not r.json()['idempotent_replayed'] for r in responses) == 1
    assert len({r.json()['refund_execution_id'] for r in responses}) == 1
    assert scalar('SELECT COUNT(*) FROM refund_executions') == 1
    assert scalar("SELECT refunded_cents FROM orders WHERE order_id='o'") == 2000
    assert scalar("SELECT SUM(refunded_cents) FROM order_items WHERE order_id='o'") == 2000
    assert scalar("SELECT defective_stock FROM products_sku WHERE sku_id='SKU_STOCK_TEST'") == 4


@pytest.mark.parametrize('changes,status,code', [
    ({'approved_refund_cents': 2001},422,'REFUND_EXCEEDS_LIMIT'),
    ({'approved_refund_cents': 1001,'order_item_id':'i1'},422,'REFUND_EXCEEDS_LIMIT'),
    ({'order_item_id':'other'},404,'NOT_FOUND'),
    ({'order_id':'other'},404,'NOT_FOUND'),
])
def test_reject(api, changes, status, code):
    response = execute(api, **changes)
    assert response.status_code == status
    assert response.json()['error']['code'] == code
    assert scalar('SELECT COUNT(*) FROM refund_executions') == 0
    assert scalar("SELECT refunded_cents FROM orders WHERE order_id='o'") == 0


def test_state_auth_and_integer(api):
    with get_engine().begin() as c:
        c.execute(text("UPDATE orders SET order_status='CREATED' WHERE order_id='o'"))
    assert execute(api).json()['error']['code'] == 'INVALID_ORDER_STATE'
    assert api.post('/internal/v1/refunds/execute',json=payload(),headers={'X-Internal-Token':'bad'}).status_code == 401
    assert execute(api, approved_refund_cents=1.5).json()['error']['code'] == 'INVALID_REQUEST'


@pytest.mark.parametrize('policy,column,expected,version', [
    ('DEFECTIVE_WAREHOUSE','defective_stock',2,0),
    ('SELLABLE_WAREHOUSE','sellable_stock',12,1),
    ('NO_RESTOCK','sellable_stock',10,0),
])
def test_partial_completion_inventory(api, policy, column, expected, version):
    assert execute(api, order_item_id='i1',approved_refund_cents=400,restock_policy=policy).status_code == 200
    assert scalar("SELECT defective_stock FROM products_sku WHERE sku_id='SKU_STOCK_TEST'") == 0
    second = execute(api, refund_idempotency_key='r2',order_item_id='i1',approved_refund_cents=600,restock_policy=policy)
    assert second.status_code == 200
    assert second.json()['order_status_after'] == 'REFUNDED_PARTIAL'
    assert scalar(f"SELECT {column} FROM products_sku WHERE sku_id='SKU_STOCK_TEST'") == expected
    assert scalar("SELECT version FROM products_sku WHERE sku_id='SKU_STOCK_TEST'") == version
    final = execute(api, refund_idempotency_key='r3')
    assert final.json()['order_status_after'] == 'REFUNDED_FULL'
    assert execute(api, refund_idempotency_key='r3').json()['idempotent_replayed'] is True


def test_reconcile_outcomes():
    posts = []
    def handler(request):
        assert request.headers['X-Internal-Token'] == 'secret'
        if request.method == 'POST':
            posts.append(request)
            return httpx.Response(200,json={'execution_status':'SUCCESS'})
        key = request.url.params['refund_idempotency_key']
        if key == 'timeout':
            raise httpx.ReadTimeout('timeout',request=request)
        if key == 'error':
            return httpx.Response(503)
        if key == 'malformed':
            return httpx.Response(200,text='invalid')
        return httpx.Response(200,json={'execution_status':{'done':'SUCCESS','missing':'NOT_FOUND','failed':'FAILED'}[key]})
    keys = ['done','missing','failed','timeout','error','malformed']
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = reconcile_items([{'refund_idempotency_key':k,'execute_payload':payload(refund_idempotency_key=k)} for k in keys],
                                  'http://pilot/','secret',client)
        assert not client.is_closed
    assert [r['outcome'] for r in results] == ['RECONCILED','REPLAYED'] + ['RETRY_LATER'] * 4
    assert [r['execution_status'] for r in results] == ['SUCCESS','SUCCESS','FAILED',None,None,None]
    assert len(posts) == 1


@pytest.mark.parametrize('response_kind', ['failed', 'http_error', 'timeout'])
def test_reconcile_execute_failure(response_kind):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'execution_status': 'NOT_FOUND'})
        if response_kind == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        if response_kind == 'http_error':
            return httpx.Response(422)
        return httpx.Response(200, json={'execution_status': 'FAILED'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = reconcile_items([{'refund_idempotency_key': 'r', 'execute_payload': payload()}],
                                 'http://pilot', 'secret', client)
    assert result[0]['outcome'] == 'RETRY_LATER'
