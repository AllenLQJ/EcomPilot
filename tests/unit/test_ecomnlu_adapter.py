import httpx
import pytest
from fastapi.testclient import TestClient

from adapters import ecomnlu_adapter as ad

client = TestClient(ad.app)


def _upstream(monkeypatch, handler):
    real = httpx.Client

    def make(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)
    monkeypatch.setattr(ad.httpx, "Client", make)


def _pred(label, confidence, sentiment=None):
    return {"category": {"label": label, "confidence": confidence}, "top_k": [{"label": label, "probability": confidence}],
            "sentiment": sentiment, "elapsed_ms": 1.0, "model_name": "test"}


def _by_text(table):
    def handler(request):
        review = httpx.Response(200, content=request.content).json()["review"]
        for key, value in table.items():
            if key in review:
                return httpx.Response(200, json=value)
        return httpx.Response(200, json=_pred("手机", 0.2))
    return handler


def test_single_clause_maps_category_brand_and_negative_sentiment(monkeypatch):
    _upstream(monkeypatch, _by_text({"热水器": _pred("热水器", 0.93, {"label": "负向", "confidence": 0.97})}))
    body = client.post("/api/v1/nlu/analyze", json={"text": "热水器漏水了", "scene_type": "aftersales"}).json()
    assert body["clauses"] == [dict(text="热水器漏水了", predicted_label="热水器", category_confidence=0.93,
                                    catalog_category_hint="HOME_APPLIANCE_HEATER", brand_hint=None, item_type="PRODUCT")]
    assert (body["sentiment"], body["sentiment_score"], body["priority_boost"]) == ("NEGATIVE", 0.97, True)
    assert body["scene_not_applicable"] is False


def test_multi_clause_each_classified_and_brand_service_types(monkeypatch):
    _upstream(monkeypatch, _by_text({"整体": _pred("蒙牛", 0.5, {"label": "正向", "confidence": 0.8}),   # 整句命中它，取情感
                                     "牛奶": _pred("蒙牛", 0.9), "酒店": _pred("酒店", 0.88)}))
    body = client.post("/api/v1/nlu/analyze", json={"text": "牛奶要一箱，酒店订一晚，整体不错", "scene_type": "shopping"}).json()
    labels = [(c["predicted_label"], c["catalog_category_hint"], c["brand_hint"], c["item_type"]) for c in body["clauses"]]
    assert labels[0] == ("蒙牛", "DAIRY_MILK", "蒙牛", "PRODUCT")
    assert labels[1] == ("酒店", "TRAVEL_HOTEL", None, "SERVICE")
    assert body["clauses"][2]["predicted_label"] == "ABSTAIN"       # 0.2 < 阈值
    assert body["sentiment"] == "POSITIVE" and body["priority_boost"] is False   # shopping 场景不加急


def test_low_confidence_abstains_without_hint(monkeypatch):
    _upstream(monkeypatch, lambda req: httpx.Response(200, json=_pred("平板", 0.64)))
    clause = client.post("/api/v1/nlu/analyze", json={"text": "随便看看"}).json()["clauses"][0]
    assert clause["predicted_label"] == "ABSTAIN" and clause["catalog_category_hint"] is None


def test_category_without_catalog_id_has_no_hint(monkeypatch):
    _upstream(monkeypatch, lambda req: httpx.Response(200, json=_pred("水果", 0.95)))
    clause = client.post("/api/v1/nlu/analyze", json={"text": "苹果很甜"}).json()["clauses"][0]
    assert clause["predicted_label"] == "水果" and clause["catalog_category_hint"] is None


def test_missing_sentiment_model_is_neutral_not_guessed(monkeypatch):
    _upstream(monkeypatch, lambda req: httpx.Response(200, json=_pred("手机", 0.9, None)))
    body = client.post("/api/v1/nlu/analyze", json={"text": "手机发热", "scene_type": "AFTER_SALE_FEEDBACK"}).json()
    assert (body["sentiment"], body["sentiment_score"], body["priority_boost"]) == ("NEUTRAL", 0.0, False)


@pytest.mark.parametrize("scene", ["PDF_BODY", "CS_APOLOGY"])
def test_not_applicable_scenes_do_not_call_upstream(monkeypatch, scene):
    _upstream(monkeypatch, lambda req: (_ for _ in ()).throw(AssertionError("must not call upstream")))
    body = client.post("/api/v1/nlu/analyze", json={"text": "致歉", "scene_type": scene}).json()
    assert body["scene_not_applicable"] is True and body["clauses"] == []


@pytest.mark.parametrize("handler", [
    lambda req: httpx.Response(503, json={"status": "degraded"}),
    lambda req: httpx.Response(200, json={"oops": 1}),
    lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused")),
])
def test_upstream_failure_is_502_so_pilot_degrades(monkeypatch, handler):
    _upstream(monkeypatch, handler)
    r = client.post("/api/v1/nlu/analyze", json={"text": "热水器漏水"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_pilot_signal_parses_adapter_output(monkeypatch):
    """适配器输出必须能被 pilot.aftersales.router._analyze 直接消费（含 sentiment 枚举）。"""
    from pilot.aftersales import router as pilot_router
    _upstream(monkeypatch, _by_text({"热水器": _pred("热水器", 0.93, {"label": "负向", "confidence": 0.97})}))
    out = client.post("/api/v1/nlu/analyze", json={"text": "热水器漏水了", "scene_type": "aftersales"}).json()

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return out

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return FakeResp()
    monkeypatch.setattr(pilot_router.httpx, "Client", FakeClient)
    signal = pilot_router._analyze("热水器漏水了")
    assert signal["catalog_category_hint"] == "HOME_APPLIANCE_HEATER" and signal["priority_boost"] is True
