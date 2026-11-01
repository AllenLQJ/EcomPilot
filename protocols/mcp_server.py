"""官方 MCP 服务（mcp SDK 2.x）：把 Pilot 的三个只读工具按标准 MCP 对外暴露。

运行在独立环境 .venv-proto（SDK 需要比主栈更新的 pydantic/starlette）。工具实现复用 pilot.mcp_tools，
直连 Pilot 的 MySQL（读取，不写）。Pilot 主进程内部仍直接调用这些函数，没有走 MCP。

  stdio:            python -m protocols.mcp_server
  streamable-http:  python -m protocols.mcp_server --transport streamable-http --port 8091
streamable-http 没有认证，默认只绑定 127.0.0.1；对外暴露前必须放到带认证的网关后面。
"""
import argparse
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from pilot.core.db import get_engine
from pilot.mcp_tools import bundle_tool, image_fallback, inventory_tool

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

mcp = MCPServer("ecompilot-tools", instructions="EcomPilot 商品库存、凑单建议与图片兜底（只读）。金额一律为整数分。")


@mcp.tool(annotations=READ_ONLY)
def get_stock(sku_id: str) -> dict[str, Any]:
    """查询 SKU 的可售库存、残次库存与版本号；SKU 不存在时 found=false。"""
    row = inventory_tool.get_stock(get_engine(), sku_id)
    return {"found": False} if row is None else {"found": True, **row}


@mcp.tool(annotations=READ_ONLY)
def suggest_bundle(tenant_id: str, lines: list[dict[str, Any]], coupons: list[dict[str, Any]]) -> dict[str, Any]:
    """按租户的满减规则与凑单池，建议最省钱的凑单商品；lines 每项含 sku_id/shop_id/catalog_category_id/unit_price_cents/qty。"""
    return bundle_tool.suggest_bundle(get_engine(), tenant_id, lines, coupons)


@mcp.tool(annotations=READ_ONLY)
def resolve_image(sku_id: str) -> dict[str, Any]:
    """解析商品图：主图 → 同品类图库 → 品类默认图；SKU 不存在时 found=false。"""
    from sqlalchemy.exc import NoResultFound
    try:
        return {"found": True, **image_fallback.resolve_image(get_engine(), sku_id)}
    except NoResultFound:
        return {"found": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
