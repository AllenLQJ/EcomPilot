import os
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from protocols.mcp_server import mcp

ROOT = Path(__file__).resolve().parents[2]


def structured(result):
    return result.structured_content if getattr(result, "structured_content", None) is not None else result.content


async def test_lists_three_read_only_tools():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_stock", "suggest_bundle", "resolve_image"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
    assert tools["get_stock"].input_schema["required"] == ["sku_id"]


async def test_get_stock_found_and_missing(db_name):
    async with Client(mcp) as client:
        hit = structured(await client.call_tool("get_stock", {"sku_id": "SKU_HEATER_003"}))
        miss = structured(await client.call_tool("get_stock", {"sku_id": "NOPE"}))
    assert hit["found"] is True and hit["sellable_stock"] == 50
    assert miss == {"found": False}


async def test_resolve_image_has_fallback_and_missing_sku(db_name):
    async with Client(mcp) as client:
        hit = structured(await client.call_tool("resolve_image", {"sku_id": "SKU_HEATER_003"}))
        miss = structured(await client.call_tool("resolve_image", {"sku_id": "NOPE"}))
    assert hit["found"] is True and hit["source"] in {"primary", "gallery", "category_default"} and hit["url"]
    assert miss == {"found": False}


async def test_suggest_bundle_matches_in_process_result(db_name):
    from sqlalchemy import text
    from pilot.core.db import get_engine
    from pilot.mcp_tools.bundle_tool import suggest_bundle
    with get_engine().connect() as c:
        product = dict(c.execute(text("SELECT sku_id, shop_id, catalog_category_id, unit_price_cents "
                                      "FROM products_sku WHERE sku_id='SKU_SHAMPOO_001'")).mappings().one())
        coupons = [dict(r) for r in c.execute(text(
            "SELECT user_coupon_id, catalog_category_id, threshold_cents, discount_cents FROM user_coupons WHERE user_id='USR_A'")).mappings()]
    product["qty"] = 5
    expected = suggest_bundle(get_engine(), "SHOP_SELF_01", [product], coupons)
    async with Client(mcp) as client:
        got = structured(await client.call_tool("suggest_bundle", {"tenant_id": "SHOP_SELF_01", "lines": [product], "coupons": coupons}))
    assert got == expected and got["saving_cents"] == 2410


async def test_invalid_arguments_are_errors_not_crashes():
    async with Client(mcp) as client:
        result = await client.call_tool("get_stock", {"wrong": 1})
    assert result.is_error is True


async def test_real_stdio_transport_end_to_end(db_name):
    """起真实子进程走 stdio（继承测试库环境变量），验证官方客户端 <-> 服务端。"""
    params = StdioServerParameters(command=sys.executable, args=["-m", "protocols.mcp_server"], cwd=str(ROOT),
                                   env={**os.environ, "PYTHONPATH": str(ROOT)})
    async with Client(params) as client:
        hit = structured(await client.call_tool("get_stock", {"sku_id": "SKU_HEATER_003"}))
    assert hit["found"] is True
