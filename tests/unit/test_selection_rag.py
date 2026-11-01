import json

import httpx
import pytest
from sqlalchemy import create_engine, text

from pilot.agents import selection_agent as agent
from pilot.core.config import Settings


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE catalog_categories (catalog_category_id TEXT, category_name TEXT)'))
        conn.execute(text("INSERT INTO catalog_categories VALUES ('C','类别')"))
        conn.execute(text('CREATE TABLE products_sku (sku_id TEXT, sku_name TEXT, shop_id TEXT, catalog_category_id TEXT, unit_price_cents INTEGER, positive_review_ratio NUMERIC, sellable_stock INTEGER, brand_name TEXT, tenant_id TEXT, is_addon_pool INTEGER)'))
        for sku, name, ratio, price, stock, addon, tenant in [
            ('A','关键词商品',.99,100,10,0,'T'), ('B','关键词商品',.8,100,10,0,'T'),
            ('R','语义商品',.9,150,10,0,'T'), ('EXP','语义商品',.99,999,10,0,'T'),
            ('EMPTY','语义商品',.99,100,0,0,'T'), ('ADD','语义商品',.99,100,10,1,'T'),
            ('OTHER','语义商品',.99,100,10,0,'OTHER')]:
            conn.execute(text("INSERT INTO products_sku VALUES (:sku,:name,'SHOP','C',:price,:ratio,:stock,'品牌',:tenant,:addon)"),
                         dict(sku=sku,name=name,price=price,ratio=ratio,stock=stock,tenant=tenant,addon=addon))
    monkeypatch.setattr(agent, 'get_engine', lambda: engine)
    monkeypatch.setattr(agent, '_hints', lambda need: ({'C'}, {'品牌'}))
    settings = Settings(rag_url='http://rag', rag_internal_token='secret', rag_timeout_s=0.7)
    monkeypatch.setattr(agent, 'get_settings', lambda: settings)
    yield settings
    engine.dispose()


def select():
    return agent.handle({'tenant_id':'T','needs':[{'text':'关键词', 'budget_cents':200}]}, '')


def test_rag_weight_recall_and_filters(setup, monkeypatch):
    def transport(request):
        assert str(request.url) == 'http://rag/api/v1/rag/search'
        assert request.headers['X-Internal-Token'] == 'secret'
        assert request.extensions['timeout']['read'] == 0.7
        assert json.loads(request.content) == {'query':'关键词','top_k':5}
        return httpx.Response(200, json={'hits':[{'skus':['B','R','EXP','EMPTY','ADD','OTHER','R']}, {'skus':None}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(httpx, 'post', client.post)
        rows = select()['candidates']
    assert [row['sku_id'] for row in rows] == ['R','B','A']
    assert rows[0]['unit_price_cents'] == 150 and rows[0]['sellable_stock'] == 10


def test_empty_token_no_request(setup, monkeypatch):
    setup.rag_internal_token = ''
    def transport(request):
        pytest.fail('no RAG request allowed')
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(httpx,'post',client.post)
        assert [r['sku_id'] for r in select()['candidates']] == ['A','B']


@pytest.mark.parametrize('failure', ['timeout','status','json',[],{}, {'hits':{}}, {'hits':[None]},
    {'hits':[{'skus':'R'}]}, {'hits':[{'skus':['R']},{'skus':'bad'}]}, {'hits':[{'skus':[{}]}]},
    {'hits':[{'skus':None}]}])
def test_failures_equal_baseline(setup, monkeypatch, failure):
    setup.rag_internal_token = ''
    baseline = select()
    setup.rag_internal_token = 'secret'
    def transport(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('slow')
        if failure == 'status':
            return httpx.Response(500)
        if failure == 'json':
            return httpx.Response(200, text='invalid')
        return httpx.Response(200,json=failure)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(httpx,'post',client.post)
        assert select() == baseline
