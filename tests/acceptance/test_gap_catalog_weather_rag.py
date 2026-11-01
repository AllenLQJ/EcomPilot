"""C-PILOT 缺口验收：商品快照、NLU 标签映射、离线天气、选品 RAG 召回。

只依赖 contracts + 本目录辅助；不读 pilot/ 实现细节。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from pilot.core.db import get_engine
from pilot.core.testdb import test_database as _test_database_factory

_CF_PATH = Path(__file__).resolve().parent / "conftest_f.py"
_CF_NAME = "tests_acceptance_conftest_f"
if _CF_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CF_NAME, _CF_PATH)
    _CF = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CF_NAME] = _CF
    _SPEC.loader.exec_module(_CF)
else:
    _CF = sys.modules[_CF_NAME]

f_free_port = _CF.f_free_port
f_live_stack_cm = _CF.f_live_stack_cm
f_sql_one = _CF.f_sql_one
f_sql_scalar = _CF.f_sql_scalar
f_stop_procs = _CF.f_stop_procs
f_wait_ready = _CF.f_wait_ready
F_INTERNAL_TOKEN = _CF.F_INTERNAL_TOKEN
F_TENANT_ID = _CF.F_TENANT_ID
F_HEATER_SKU = _CF.F_HEATER_SKU
ROOT = _CF.ROOT

SKU_TABLET = "SKU_TABLET_001"
TABLET_PRICE_CENTS = 239900
NEED_NO_KEYWORD = "想买点好东西送人"
RAG_TOKEN = "rag-token"

# EcomNLU 十标签 → 期望 catalog_category_id（与品类树扩容一致）
NLU_LABEL_TO_CATEGORY = {
    "书籍": "BOOKS",
    "平板": "DIGITAL_TABLET",
    "手机": "DIGITAL_PHONE",
    "水果": "FRESH_FRUIT",
    "洗发水": "PERSONAL_SHAMPOO",
    "热水器": "HOME_APPLIANCE_HEATER",
    "蒙牛": "DAIRY_MILK",
    "衣服": "APPAREL",
    "计算机": "DIGITAL_COMPUTER",
    "酒店": "TRAVEL_HOTEL",
}

REQUIRED_CATEGORY_IDS = {
    "HOME_APPLIANCE_HEATER",
    "BOOKS",
    "DIGITAL_PHONE",
    "FRESH_FRUIT",
    "APPAREL",
    "DIGITAL_COMPUTER",
}


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code")
    return None


def _base_env(db_name: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if db_name is not None:
        env["ECOMPILOT_DB_NAME"] = db_name
    return env


def _popen_uvicorn(module: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@contextmanager
def _env_fit_process() -> Iterator[str]:
    """起环境适配代理；不设置 ECOMPILOT_WEATHER → 默认离线表。"""
    port = f_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = _base_env()
    env.pop("ECOMPILOT_WEATHER", None)
    proc = _popen_uvicorn("pilot.agents.env_fit_agent:app", port, env)
    try:
        f_wait_ready(base_url)
        yield base_url
    finally:
        f_stop_procs([proc])


class _FakeRagState:
    def __init__(self) -> None:
        self.mode: str = "ok"  # ok | http500 | non_json | skus_not_array
        self.requests: list[dict[str, Any]] = []


@contextmanager
def _fake_rag_server() -> Iterator[tuple[str, _FakeRagState]]:
    """线程内 uvicorn 假 RAG：POST /api/v1/rag/search。"""
    state = _FakeRagState()
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/rag/search")
    async def rag_search(request: Request) -> Any:
        body = await request.json()
        state.requests.append(
            {
                "headers": {
                    "X-Internal-Token": request.headers.get("X-Internal-Token"),
                },
                "body": body,
            }
        )
        if state.mode == "http500":
            return Response(content="boom", status_code=500, media_type="text/plain")
        if state.mode == "non_json":
            return Response(content="not-json<<<", status_code=200, media_type="text/plain")
        if state.mode == "skus_not_array":
            return {
                "hits": [
                    {
                        "chunk_id": "c1",
                        "text": "x",
                        "rule_source": "r",
                        "is_demo_rule": False,
                        "score": 0.9,
                        "skus": "SKU_TABLET_001",
                    }
                ]
            }
        return {
            "hits": [
                {
                    "chunk_id": "c1",
                    "text": "x",
                    "rule_source": "r",
                    "is_demo_rule": False,
                    "score": 0.9,
                    "skus": [SKU_TABLET],
                }
            ]
        }

    port = f_free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline_ok = False
        with httpx.Client(timeout=1.5) as probe:
            for _ in range(200):
                try:
                    if probe.get(f"{base_url}/health").status_code < 500:
                        deadline_ok = True
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.05)
        assert deadline_ok, f"fake RAG not ready at {base_url}"
        yield base_url, state
    finally:
        server.should_exit = True
        thread.join(timeout=8)


@contextmanager
def _selection_agent(
    db_name: str,
    *,
    rag_url: str,
    rag_token: str | None,
) -> Iterator[str]:
    """起选品代理；NLU 指向不可达地址以强制降级。"""
    port = f_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = _base_env(db_name)
    env["ECOMPILOT_RAG_URL"] = rag_url
    env["ECOMPILOT_NLU_URL"] = "http://127.0.0.1:1"
    if rag_token is None:
        env["ECOMPILOT_RAG_INTERNAL_TOKEN"] = ""
    else:
        env["ECOMPILOT_RAG_INTERNAL_TOKEN"] = rag_token
    proc = _popen_uvicorn("pilot.agents.selection_agent:app", port, env)
    try:
        f_wait_ready(base_url)
        yield base_url
    finally:
        f_stop_procs([proc])


def _invoke_selection(base_url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{base_url}/a2a/invoke",
        json={"trace_id": "t-rag", "input": payload},
        timeout=30.0,
    )


def _candidate_sku_ids(body: dict[str, Any]) -> list[str]:
    cands = (body.get("output") or {}).get("candidates") or []
    return [c.get("sku_id") for c in cands if isinstance(c, dict)]


def _set_tablet_stock(stock: int) -> None:
    with get_engine().begin() as conn:
        result = conn.execute(
            text("UPDATE products_sku SET sellable_stock = :s WHERE sku_id = :sid"),
            {"s": stock, "sid": SKU_TABLET},
        )
        assert result.rowcount == 1, f"failed to update {SKU_TABLET} stock"


# ---------------------------------------------------------------------------
# 1) 商品快照只读接口（Pilot 主 API + 独立测试库）
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_stack():
    with f_live_stack_cm() as stack:
        yield stack


def test_catalog_sku_snapshot_ok(catalog_stack):
    stack = catalog_stack
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(
            f"/internal/v1/catalog/skus/{F_HEATER_SKU}",
            params={"tenant_id": F_TENANT_ID},
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
        )
    assert r.status_code == 200, f"SKU snapshot expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    for key in (
        "sku_id",
        "tenant_id",
        "shop_id",
        "catalog_category_id",
        "category_name",
        "brand_name",
        "sku_name",
        "unit_price_cents",
        "sellable_stock",
        "positive_review_ratio",
    ):
        assert key in body, f"SKU snapshot missing field {key!r}: {body!r}"
    assert body["sku_id"] == F_HEATER_SKU, body
    assert body["tenant_id"] == F_TENANT_ID, body
    assert body["shop_id"] == "SHOP_A", body
    assert body["catalog_category_id"] == "HOME_APPLIANCE_HEATER", body
    assert body["category_name"] == "热水器", body
    assert body["brand_name"] == "海尔", body
    assert body["sku_name"] == "海尔 60L 电热水器", body
    assert body["unit_price_cents"] == 112000, body
    assert isinstance(body["unit_price_cents"], int) and not isinstance(
        body["unit_price_cents"], bool
    ), f"unit_price_cents must be int, got {type(body['unit_price_cents']).__name__}: {body['unit_price_cents']!r}"
    assert isinstance(body["sellable_stock"], int) and not isinstance(
        body["sellable_stock"], bool
    ), f"sellable_stock must be int, got {type(body['sellable_stock']).__name__}"
    assert body["sellable_stock"] == 50, body
    assert isinstance(
        body["positive_review_ratio"], float
    ), f"positive_review_ratio must be float, got {type(body['positive_review_ratio']).__name__}: {body['positive_review_ratio']!r}"
    assert body["positive_review_ratio"] == pytest.approx(0.98, abs=1e-3), body


def test_catalog_sku_wrong_tenant_404(catalog_stack):
    stack = catalog_stack
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(
            f"/internal/v1/catalog/skus/{F_HEATER_SKU}",
            params={"tenant_id": "OTHER"},
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
        )
    assert r.status_code == 404, f"wrong tenant expected 404, got {r.status_code}: {r.text}"
    assert _error_code(r) == "NOT_FOUND", f"expected NOT_FOUND, got {r.text}"


def test_catalog_sku_missing_sku_404(catalog_stack):
    stack = catalog_stack
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(
            "/internal/v1/catalog/skus/SKU_DOES_NOT_EXIST",
            params={"tenant_id": F_TENANT_ID},
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
        )
    assert r.status_code == 404, f"missing SKU expected 404, got {r.status_code}: {r.text}"
    assert _error_code(r) == "NOT_FOUND", f"expected NOT_FOUND, got {r.text}"


def test_catalog_sku_missing_tenant_400(catalog_stack):
    stack = catalog_stack
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(
            f"/internal/v1/catalog/skus/{F_HEATER_SKU}",
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
        )
    assert r.status_code == 400, f"missing tenant_id expected 400, got {r.status_code}: {r.text}"
    assert _error_code(r) == "INVALID_REQUEST", f"expected INVALID_REQUEST, got {r.text}"


def test_catalog_sku_missing_token_401(catalog_stack):
    stack = catalog_stack
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(
            f"/internal/v1/catalog/skus/{F_HEATER_SKU}",
            params={"tenant_id": F_TENANT_ID},
        )
    assert r.status_code == 401, f"missing token expected 401, got {r.status_code}: {r.text}"


def test_catalog_categories_list(catalog_stack):
    stack = catalog_stack
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(
            "/internal/v1/catalog/categories",
            headers={"X-Internal-Token": F_INTERNAL_TOKEN},
        )
    assert r.status_code == 200, f"categories expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert "categories" in body and isinstance(body["categories"], list), body
    assert body["categories"], "categories must be non-empty"
    ids: set[str] = set()
    for item in body["categories"]:
        for key in ("catalog_category_id", "parent_id", "category_name", "item_type"):
            assert key in item, f"category item missing {key!r}: {item!r}"
        ids.add(item["catalog_category_id"])
    missing = REQUIRED_CATEGORY_IDS - ids
    assert not missing, f"categories missing required ids {sorted(missing)}; got {sorted(ids)}"


# ---------------------------------------------------------------------------
# 2) NLU 标签映射表（直查 MySQL）
# ---------------------------------------------------------------------------


def test_nlu_label_to_catalog_map_ten_labels(db_name):
    """十个 EcomNLU 标签均存在，且 catalog_category_id 落在真实品类上。"""
    _ = db_name
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.nlu_label, m.label_kind, m.catalog_category_id, "
                "m.brand_hint, m.item_type, c.catalog_category_id AS cat_exists "
                "FROM nlu_label_to_catalog_map m "
                "LEFT JOIN catalog_categories c "
                "ON c.catalog_category_id = m.catalog_category_id"
            )
        ).mappings().all()
    by_label = {r["nlu_label"]: dict(r) for r in rows}
    missing_labels = sorted(set(NLU_LABEL_TO_CATEGORY) - set(by_label))
    assert not missing_labels, (
        f"nlu_label_to_catalog_map missing labels {missing_labels}; "
        f"present={sorted(by_label)}"
    )
    for label, expected_cat in NLU_LABEL_TO_CATEGORY.items():
        row = by_label[label]
        assert row["cat_exists"] is not None, (
            f"label {label!r} maps to {row['catalog_category_id']!r} "
            f"which is absent from catalog_categories"
        )
        assert row["catalog_category_id"] == expected_cat, (
            f"label {label!r}: expected catalog_category_id={expected_cat!r}, "
            f"got {row['catalog_category_id']!r}"
        )
    mengniu = by_label["蒙牛"]
    assert mengniu["catalog_category_id"] == "DAIRY_MILK", mengniu
    hotel = by_label["酒店"]
    assert hotel["catalog_category_id"] == "TRAVEL_HOTEL", hotel
    # 列名以 schema 为准，确认关键列可读
    assert f_sql_one(
        "SELECT nlu_label, label_kind, catalog_category_id, brand_hint, item_type "
        "FROM nlu_label_to_catalog_map WHERE nlu_label = :l",
        {"l": "蒙牛"},
    ) is not None
    assert f_sql_scalar(
        "SELECT COUNT(*) FROM nlu_label_to_catalog_map WHERE nlu_label IN "
        "('书籍','平板','手机','水果','洗发水','热水器','蒙牛','衣服','计算机','酒店')"
    ) == 10


# ---------------------------------------------------------------------------
# 3) 环境适配代理天气（离线默认，起独立进程）
# ---------------------------------------------------------------------------


def test_env_fit_weather_hangzhou_offline_no_source():
    with _env_fit_process() as base_url:
        r = httpx.post(
            f"{base_url}/a2a/invoke",
            json={"trace_id": "t", "input": {"city": "杭州"}},
            timeout=15.0,
        )
    assert r.status_code == 200, f"env_fit hangzhou expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("status") == "ok", body
    out = body.get("output") or {}
    assert out.get("temperature_c") == 18, out
    assert out.get("humidity_pct") == 75, out
    assert "source" not in out, f"offline weather must not expose source key, got {out!r}"


def test_env_fit_weather_unknown_city_defaults():
    with _env_fit_process() as base_url:
        r = httpx.post(
            f"{base_url}/a2a/invoke",
            json={"trace_id": "t", "input": {"city": "火星基地"}},
            timeout=15.0,
        )
    assert r.status_code == 200, f"env_fit unknown city expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("status") == "ok", body
    out = body.get("output") or {}
    assert out.get("temperature_c") == 20, out
    assert out.get("humidity_pct") == 50, out
    assert "source" not in out, f"offline weather must not expose source key, got {out!r}"


def test_env_fit_empty_city_rejected():
    """空 city：HTTP 400，或 200 且 status 非 ok（与 test_agents_a2a 非法入参风格一致）。"""
    with _env_fit_process() as base_url:
        r = httpx.post(
            f"{base_url}/a2a/invoke",
            json={"trace_id": "t", "input": {"city": ""}},
            timeout=15.0,
        )
    if r.status_code == 400:
        return
    assert r.status_code == 200, (
        f"empty city expected HTTP 400 or status!=ok body, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body.get("status") != "ok", f"empty city must not be ok, got {body!r}"


# ---------------------------------------------------------------------------
# 4) 选品代理 EcomRAG 召回（假 RAG + 选品进程）
# ---------------------------------------------------------------------------


def test_selection_rag_recall_with_token():
    with _test_database_factory(with_seed=True) as db_name:
        with _fake_rag_server() as (rag_url, rag_state):
            with _selection_agent(db_name, rag_url=rag_url, rag_token=RAG_TOKEN) as sel_url:
                r = _invoke_selection(
                    sel_url,
                    {"tenant_id": F_TENANT_ID, "needs": [{"text": NEED_NO_KEYWORD}]},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    skus = _candidate_sku_ids(body)
    assert SKU_TABLET in skus, f"RAG hit SKU missing from candidates {skus}; body={body!r}"
    assert len(rag_state.requests) >= 1, "configured RAG token must call fake RAG"
    last = rag_state.requests[-1]
    assert last["headers"]["X-Internal-Token"] == RAG_TOKEN, last
    query = (last.get("body") or {}).get("query")
    assert isinstance(query, str) and NEED_NO_KEYWORD in query, (
        f"RAG body.query must contain need text, got {last.get('body')!r}"
    )
    hit = next(c for c in body["output"]["candidates"] if c.get("sku_id") == SKU_TABLET)
    assert hit["unit_price_cents"] == TABLET_PRICE_CENTS, hit
    assert isinstance(hit["unit_price_cents"], int) and not isinstance(
        hit["unit_price_cents"], bool
    ), hit


def test_selection_rag_skipped_without_token():
    with _test_database_factory(with_seed=True) as db_name:
        with _fake_rag_server() as (rag_url, rag_state):
            with _selection_agent(db_name, rag_url=rag_url, rag_token=None) as sel_url:
                r = _invoke_selection(
                    sel_url,
                    {"tenant_id": F_TENANT_ID, "needs": [{"text": NEED_NO_KEYWORD}]},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert len(rag_state.requests) == 0, (
        f"empty RAG token must not call RAG, got {len(rag_state.requests)} requests: "
        f"{rag_state.requests!r}"
    )
    skus = _candidate_sku_ids(body)
    assert SKU_TABLET not in skus, (
        f"without RAG, no-keyword need must not include {SKU_TABLET}, got {skus}"
    )


@pytest.mark.parametrize("mode", ["http500", "non_json", "skus_not_array"])
def test_selection_rag_failures_degrade(mode: str):
    with _test_database_factory(with_seed=True) as db_name:
        with _fake_rag_server() as (rag_url, rag_state):
            rag_state.mode = mode
            with _selection_agent(db_name, rag_url=rag_url, rag_token=RAG_TOKEN) as sel_url:
                r = _invoke_selection(
                    sel_url,
                    {"tenant_id": F_TENANT_ID, "needs": [{"text": NEED_NO_KEYWORD}]},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", (
        f"RAG failure mode={mode!r} must still return status=ok, got {body!r}"
    )
    skus = _candidate_sku_ids(body)
    assert SKU_TABLET not in skus, (
        f"RAG failure mode={mode!r} must match no-RAG candidates "
        f"(no {SKU_TABLET}), got {skus}; body={body!r}"
    )


def test_selection_rag_excludes_zero_stock():
    with _test_database_factory(with_seed=True) as db_name:
        _set_tablet_stock(0)
        with _fake_rag_server() as (rag_url, rag_state):
            with _selection_agent(db_name, rag_url=rag_url, rag_token=RAG_TOKEN) as sel_url:
                r = _invoke_selection(
                    sel_url,
                    {"tenant_id": F_TENANT_ID, "needs": [{"text": NEED_NO_KEYWORD}]},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert len(rag_state.requests) >= 1, "RAG should still be called"
    skus = _candidate_sku_ids(body)
    assert SKU_TABLET not in skus, (
        f"zero-stock RAG hit must not enter candidates, got {skus}"
    )


def test_selection_rag_excludes_over_budget():
    with _test_database_factory(with_seed=True) as db_name:
        with _fake_rag_server() as (rag_url, rag_state):
            with _selection_agent(db_name, rag_url=rag_url, rag_token=RAG_TOKEN) as sel_url:
                r = _invoke_selection(
                    sel_url,
                    {
                        "tenant_id": F_TENANT_ID,
                        "needs": [
                            {
                                "text": NEED_NO_KEYWORD,
                                "budget_cents": TABLET_PRICE_CENTS - 1,
                            }
                        ],
                    },
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert len(rag_state.requests) >= 1, "RAG should still be called"
    skus = _candidate_sku_ids(body)
    assert SKU_TABLET not in skus, (
        f"over-budget RAG hit must not enter candidates, got {skus}; body={body!r}"
    )


def test_selection_rag_price_from_db_not_stub():
    """命中 SKU 的单价以 DB 为准（239900 int），不信任 RAG 侧编造价。"""
    with _test_database_factory(with_seed=True) as db_name:
        with _fake_rag_server() as (rag_url, _rag_state):
            with _selection_agent(db_name, rag_url=rag_url, rag_token=RAG_TOKEN) as sel_url:
                r = _invoke_selection(
                    sel_url,
                    {"tenant_id": F_TENANT_ID, "needs": [{"text": NEED_NO_KEYWORD}]},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    hit = next(
        (c for c in body["output"]["candidates"] if c.get("sku_id") == SKU_TABLET),
        None,
    )
    assert hit is not None, f"expected {SKU_TABLET} in candidates: {body!r}"
    assert hit["unit_price_cents"] == TABLET_PRICE_CENTS, hit
    assert type(hit["unit_price_cents"]) is int, (
        f"unit_price_cents must be int, got {type(hit['unit_price_cents']).__name__}"
    )
    # 避免误把 JSON 数字当 float 比较通过
    assert json.loads(json.dumps(hit["unit_price_cents"])) == TABLET_PRICE_CENTS
