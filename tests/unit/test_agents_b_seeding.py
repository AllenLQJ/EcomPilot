from copy import deepcopy
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.agents import env_fit_agent as env
from pilot.agents import seeding_note_agent as seed
from pilot.agents.a2a_client import A2AError


REQUEST = {"auth_token": "jwt-original", "tenant_id": "SHOP_SELF_01", "city": "杭州",
           "needs": [{"text": "洗发水"}, {"text": "牛奶", "budget_cents": 2000}]}
CANDIDATES = [
    {"need_index": 1, "sku_id": "milk", "sku_name": "牛奶", "unit_price_cents": 1250,
     "reviews": ["奶香浓郁，这是实际返回的评论。"]},
    {"need_index": 0, "sku_id": "shampoo", "sku_name": "洗发水", "unit_price_cents": 5900,
     "reviews": ["洗后清爽，这是另一条真实返回的评论。", "第二条评论"]},
    {"need_index": 0, "sku_id": "alternative", "sku_name": "备选", "unit_price_cents": 6000,
     "reviews": ["不应选中"]},
]
QUOTE = {"quote_id": "Q-1", "total_payable_cents": 7125, "suggested_addons": []}


@pytest.fixture
def downstream(monkeypatch):
    calls = []
    failures = {}
    quote = deepcopy(QUOTE)
    barrier = Barrier(2, timeout=3)

    def invoke(url, input, trace_id, timeout=3.0):
        calls.append((url, input, trace_id))
        if url.endswith(":8011") or url.endswith(":8012"):
            barrier.wait()  # Sequential upstream calls would fail this test.
        if url in failures:
            raise A2AError(failures[url])
        if url.endswith(":8011"):
            return env.handle(input, trace_id)
        if url.endswith(":8012"):
            return {"candidates": deepcopy(CANDIDATES)}
        assert url.endswith(":8013")
        assert len(calls) == 3
        return {"quote": quote}

    for variable in ("ENV_FIT", "SELECTION", "PRICING"):
        monkeypatch.delenv(f"ECOMPILOT_AGENT_{variable}_URL", raising=False)
    monkeypatch.setattr(seed.a2a_client, "invoke", invoke)
    monkeypatch.setattr(seed, "get_engine", lambda: "engine")
    monkeypatch.setattr(seed, "resolve_image", lambda engine, sku_id: {
        "url": f"/images/{sku_id}.png", "source": "primary"})
    return calls, failures, quote


def generate():
    response = TestClient(seed.app).post("/a2a/invoke", json={"trace_id": "trace-test", "input": REQUEST})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    return response.json()["output"]["note"]


@pytest.mark.parametrize("city,temp,humidity", [("杭州", 18, 75), ("北京", 8, 30), ("广州", 26, 80), ("成都", 20, 50)])
def test_env_data(city, temp, humidity):
    client = TestClient(env.app)
    response = client.post("/a2a/invoke", json={"input": {"city": city, "days": 2}})
    assert response.status_code == 200
    output = response.json()["output"]
    assert output == {"city": city, "temperature_c": temp, "humidity_pct": humidity,
                      "advice": env.handle({"city": city}, "-")["advice"]}
    assert all(output["advice"].values())


@pytest.mark.parametrize("module,name,port", [(env, "env_fit", 8011), (seed, "seeding_note", 8015)])
def test_agent_protocol(module, name, port):
    client = TestClient(module.app)
    assert client.get("/health").json() == {"status": "ok", "agent": name}
    assert client.get("/.well-known/agent.json").json() == {"name": name, "port": port, "skills": [name]}
    assert client.post("/a2a/invoke", json={"input": {}}).status_code == 400
    assert client.post("/a2a/invoke", json={}).status_code == 400


@pytest.mark.parametrize("data", [{"city": 5}, {"city": "杭州", "days": True}, {"city": "杭州", "days": 0}])
def test_env_invalid(data):
    assert TestClient(env.app).post("/a2a/invoke", json={"input": data}).status_code == 400


def test_all_success(downstream):
    calls, _, _ = downstream
    note = generate()
    assert note["degraded"] == []
    assert note["quote_id"] == "Q-1"
    assert note["savings_formula"] is None
    assert note["title"] in note["body"]
    assert note["evidence"]["city_advice"]["clothing"] in note["body"]
    assert note["evidence"]["picks"] == [
        {"sku_id": "shampoo", "review": CANDIDATES[1]["reviews"][0]},
        {"sku_id": "milk", "review": CANDIDATES[0]["reviews"][0]},
    ]
    for pick in note["evidence"]["picks"]:
        assert pick["review"] in note["body"]
    assert "不应选中" not in note["body"]
    pricing = next(call for call in calls if call[0].endswith(":8013"))
    assert pricing[1] == {"auth_token": "jwt-original", "items": [
        {"sku_id": "shampoo", "qty": 1}, {"sku_id": "milk", "qty": 1}]}
    assert all(call[2] == "trace-test" for call in calls)
    assert next(call[1] for call in calls if call[0].endswith(":8012")) == {
        "tenant_id": REQUEST["tenant_id"], "needs": REQUEST["needs"]}


def test_selection_timeout(downstream):
    calls, failures, _ = downstream
    failures["http://127.0.0.1:8012"] = "UPSTREAM_TIMEOUT"
    note = generate()
    assert note["degraded"] == ["selection"]
    assert note["images"] == note["evidence"]["picks"] == []
    assert note["quote_id"] is note["savings_formula"] is None
    assert note["evidence"]["city_advice"]
    assert len(calls) == 2


def test_pricing_failure_still_ok(downstream):
    _, failures, _ = downstream
    failures["http://127.0.0.1:8013"] = "UPSTREAM_ERROR"
    note = generate()
    assert note["degraded"] == ["pricing_order"]
    assert note["quote_id"] is note["savings_formula"] is None
    assert len(note["images"]) == len(note["evidence"]["picks"]) == 2


def test_env_failure_still_ok(downstream):
    _, failures, _ = downstream
    failures["http://127.0.0.1:8011"] = "UPSTREAM_ERROR"
    note = generate()
    assert note["degraded"] == ["env_fit"]
    assert note["evidence"]["city_advice"] is None
    assert note["quote_id"] == "Q-1"


def test_savings_formula(downstream):
    _, _, quote = downstream
    quote["suggested_addons"] = [
        {"sku_name": name, "payable_with_addon_cents": 6624, "saving_cents": 501}
        for name in ("纸巾", "牙刷")]
    note = generate()
    assert note["savings_formula"] == "加购 纸巾、牙刷 后实付 ¥71.25 → ¥66.24，省 ¥5.01"
    assert note["savings_formula"] in note["body"]


def test_runtime_urls(monkeypatch):
    seen = []
    monkeypatch.setattr(seed.a2a_client, "invoke", lambda url, data, trace: seen.append(url) or {})
    for suffix, port in [("ENV_FIT", 8011), ("SELECTION", 8012), ("PRICING", 8013)]:
        for host in ("first", "second"):
            monkeypatch.setenv(f"ECOMPILOT_AGENT_{suffix}_URL", f"http://{host}:{port}")
            seed._invoke(suffix, port, {}, "t")
            assert seen[-1] == f"http://{host}:{port}"


@pytest.mark.parametrize("source", ["primary", "gallery", "category_default"])
def test_image_source_preserved(downstream, monkeypatch, source):
    monkeypatch.setattr(seed, "resolve_image", lambda engine, sku_id: {"url": "/fallback.png", "source": source})
    assert generate()["images"] == [
        {"sku_id": sku_id, "url": "/fallback.png", "source": source} for sku_id in ("shampoo", "milk")]


def test_image_database_fallback(db_name, monkeypatch):
    from pilot.core.db import get_engine
    from pilot.mcp_tools.image_fallback import resolve_image

    monkeypatch.setattr(seed, "get_engine", get_engine)
    monkeypatch.setattr(seed, "resolve_image", resolve_image)

    def invoke(url, input, trace_id):
        if url.endswith(":8011"):
            return env.handle(input, trace_id)
        if url.endswith(":8012"):
            return {"candidates": [{"need_index": 0, "sku_id": "SKU_ADDON_01", "sku_name": "纸巾",
                                    "unit_price_cents": 590, "reviews": ["纸巾柔软"]}]}
        return {"quote": QUOTE}

    monkeypatch.setattr(seed.a2a_client, "invoke", invoke)
    for suffix in ("ENV_FIT", "SELECTION", "PRICING"):
        monkeypatch.delenv(f"ECOMPILOT_AGENT_{suffix}_URL", raising=False)
    assert generate()["images"][0] == {"sku_id": "SKU_ADDON_01", "url": "/static/default/DAILY_GENERAL.png",
                                        "source": "category_default"}
    with get_engine().begin() as connection:
        connection.execute(text("UPDATE products_sku SET image_url='/gallery.png' WHERE sku_id='SKU_ADDON_02'"))
    assert generate()["images"][0]["source"] == "gallery"
    with get_engine().begin() as connection:
        connection.execute(text("UPDATE products_sku SET image_url='/primary.png' WHERE sku_id='SKU_ADDON_01'"))
    assert generate()["images"][0]["source"] == "primary"
