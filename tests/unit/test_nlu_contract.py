"""Pilot 对 NLU 响应的依赖，固定为 EcomNLU 契约 v1.3 §3.7 的真实形状（含 Pilot 不用的额外字段与 null 提示）。"""
import httpx
import pytest

from pilot.agents import selection_agent
from pilot.aftersales import router as aftersales

NATIVE_RESPONSE = {
    "clauses": [
        {"text": "牛奶要一箱", "predicted_label": "蒙牛", "category_confidence": 0.91, "category_code": "DAIRY",
         "catalog_category_hint": "DAIRY_MILK", "brand_hint": "蒙牛", "item_type": "PRODUCT"},
        {"text": "苹果也来点", "predicted_label": "水果", "category_confidence": 0.95, "category_code": "FRUIT",
         "catalog_category_hint": None, "brand_hint": None, "item_type": "PRODUCT"},
        {"text": "随便看看", "predicted_label": "ABSTAIN", "category_confidence": 0.31, "category_code": None,
         "catalog_category_hint": None, "brand_hint": None, "item_type": "PRODUCT"}],
    "sentiment": "NEGATIVE", "sentiment_score": 0.97, "priority_boost": True, "scene_not_applicable": False,
    "model_name": "bert_base_chinese"}


@pytest.fixture
def native_nlu(monkeypatch):
    real_post, real_client = httpx.post, httpx.Client

    def handler(request):
        assert request.url.path == "/api/v1/nlu/analyze"
        return httpx.Response(200, json=NATIVE_RESPONSE)
    monkeypatch.setattr(selection_agent.httpx, "post",
                        lambda url, **kw: real_client(transport=httpx.MockTransport(handler)).post(url, **kw))
    monkeypatch.setattr(aftersales.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def test_selection_uses_hints_and_skips_null_and_abstain(native_nlu):
    categories, brands = selection_agent._hints("牛奶要一箱，苹果也来点，随便看看")
    assert categories == {"DAIRY_MILK"} and brands == {"蒙牛"}


def test_aftersales_signal_from_native_response(native_nlu):
    signal = aftersales._analyze("牛奶要一箱")
    assert signal == {"predicted_label": "蒙牛", "category_confidence": 0.91, "catalog_category_hint": "DAIRY_MILK",
                      "sentiment": "NEGATIVE", "sentiment_score": 0.97, "priority_boost": True}
