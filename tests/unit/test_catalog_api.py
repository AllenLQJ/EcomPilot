import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from pilot.catalog import router
from pilot.core.config import get_settings
from pilot.core.errors import install_error_handlers


@pytest.fixture
def client(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE catalog_categories (catalog_category_id TEXT, parent_id TEXT, category_name TEXT, item_type TEXT)'))
        conn.execute(text("INSERT INTO catalog_categories VALUES ('BOOKS',NULL,'书籍','PHYSICAL')"))
        conn.execute(text('CREATE TABLE products_sku (sku_id TEXT, tenant_id TEXT, shop_id TEXT, catalog_category_id TEXT, brand_name TEXT, sku_name TEXT, unit_price_cents BIGINT, sellable_stock INTEGER, positive_review_ratio NUMERIC)'))
        conn.execute(text("INSERT INTO products_sku VALUES ('S','T','SHOP','BOOKS',NULL,'书',1234,8,0.98)"))
    monkeypatch.setattr(router, 'get_engine', lambda: engine)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router.router)
    with TestClient(app) as client:
        yield client
    engine.dispose()


def test_snapshots(client):
    headers = {'X-Internal-Token': get_settings().internal_token}
    result = client.get('/internal/v1/catalog/skus/S?tenant_id=T', headers=headers)
    assert result.status_code == 200
    assert result.json() == dict(sku_id='S', tenant_id='T', shop_id='SHOP', catalog_category_id='BOOKS',
                                 category_name='书籍', brand_name=None, sku_name='书', unit_price_cents=1234,
                                 sellable_stock=8, positive_review_ratio=0.98)
    assert type(result.json()['unit_price_cents']) is int
    assert type(result.json()['positive_review_ratio']) is float
    categories = client.get('/internal/v1/catalog/categories', headers=headers)
    assert categories.status_code == 200
    assert categories.json() == {'categories': [dict(catalog_category_id='BOOKS', parent_id=None, category_name='书籍', item_type='PHYSICAL')]}


@pytest.mark.parametrize('path,status,code', [
    ('/skus/S', 400, 'INVALID_REQUEST'), ('/skus/S?tenant_id=OTHER',404,'NOT_FOUND'),
    ('/skus/missing?tenant_id=T',404,'NOT_FOUND')])
def test_errors(client, path, status, code):
    response = client.get('/internal/v1/catalog'+path, headers={'X-Internal-Token': get_settings().internal_token})
    assert response.status_code == status
    assert response.json()['error']['code'] == code


@pytest.mark.parametrize('path', ['/categories', '/skus/S?tenant_id=T'])
@pytest.mark.parametrize('headers', [{}, {'X-Internal-Token': 'wrong'}])
def test_auth(client, path, headers):
    response = client.get('/internal/v1/catalog'+path, headers=headers)
    assert response.status_code == 401
    assert response.json()['error']['code'] == 'INTERNAL_AUTH_FAILED'
