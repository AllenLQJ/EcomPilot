import httpx
import pytest
from sqlalchemy import create_engine, text

from pilot.catalog.labels import resolve_label
from pilot.agents import selection_agent
from pilot.aftersales import router as aftersales


@pytest.fixture
def engine():
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE nlu_label_to_catalog_map (nlu_label TEXT, catalog_category_id TEXT, brand_hint TEXT, item_type TEXT)'))
        conn.execute(text("INSERT INTO nlu_label_to_catalog_map VALUES ('蒙牛','DAIRY_MILK','蒙牛','PHYSICAL')"))
    resolve_label.cache_clear()
    yield engine
    resolve_label.cache_clear()
    engine.dispose()


def test_cache_and_copy(engine):
    row = resolve_label(engine, '蒙牛')
    row['brand_hint'] = 'mutated'
    with engine.begin() as conn:
        conn.execute(text("UPDATE nlu_label_to_catalog_map SET brand_hint='new'"))
    assert resolve_label(engine, '蒙牛')['brand_hint'] == '蒙牛'
    resolve_label.cache_clear()
    assert resolve_label(engine, '蒙牛')['brand_hint'] == 'new'
    assert resolve_label(engine, 'missing') is None


@pytest.mark.parametrize('mode', ['mapped', 'nlu_wins', 'missing', 'abstain', 'error'])
def test_hints(monkeypatch, engine, mode):
    if mode == 'error':
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE nlu_label_to_catalog_map'))
    nlu_hints = mode in ('nlu_wins', 'missing', 'abstain')
    clause = dict(predicted_label={'missing': '未知', 'abstain': 'ABSTAIN'}.get(mode, '蒙牛'),
                  catalog_category_hint='NLU_CAT' if nlu_hints else None, brand_hint='NLU_BRAND' if nlu_hints else None,
                  category_confidence=0.9)
    def transport(request):
        return httpx.Response(200, json=dict(clauses=[clause], sentiment='NEGATIVE', sentiment_score=0.9, priority_boost=True))
    original_client = httpx.Client
    with original_client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(httpx, 'post', client.post)
        monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(transport)))
        monkeypatch.setattr(selection_agent, 'get_engine', lambda: engine)
        monkeypatch.setattr(aftersales, 'get_engine', lambda: engine)
        categories, brands = selection_agent._hints('牛奶')
        signal = aftersales._analyze('牛奶')
    # mapped: NLU 没给提示 -> 映射表补；nlu_wins: NLU 给了就以 NLU 为准；error: 读表失败只是少一个来源
    expected = {'mapped': ({'DAIRY_MILK'}, {'蒙牛'}), 'nlu_wins': ({'NLU_CAT'}, {'NLU_BRAND'}),
                'missing': ({'NLU_CAT'}, {'NLU_BRAND'}), 'abstain': (set(), set()), 'error': (set(), set())}[mode]
    assert (categories, brands) == expected
    assert signal['catalog_category_hint'] == next(iter(expected[0]), None)
    assert 'brand_hint' not in signal      # Signal 契约里没有品牌字段，品牌提示只用于选品
