"""Internal read-only catalog snapshots."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from pilot.core.auth import require_internal
from pilot.core.db import get_engine
from pilot.core.errors import ApiError

router = APIRouter(prefix="/internal/v1/catalog", dependencies=[Depends(require_internal)])


@router.get("/skus/{sku_id}")
def sku_snapshot(sku_id: str, tenant_id: str | None = None):
    if not tenant_id:
        raise ApiError(400, "INVALID_REQUEST", "tenant_id is required")
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT p.sku_id, p.tenant_id, p.shop_id, p.catalog_category_id, c.category_name, "
            "p.brand_name, p.sku_name, p.unit_price_cents, p.sellable_stock, p.positive_review_ratio "
            "FROM products_sku p JOIN catalog_categories c "
            "ON c.catalog_category_id=p.catalog_category_id "
            "WHERE p.sku_id=:sku AND p.tenant_id=:tenant"),
            {"sku": sku_id, "tenant": tenant_id}).mappings().first()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "sku not found")
    result = dict(row)
    result["unit_price_cents"] = int(result["unit_price_cents"])
    result["positive_review_ratio"] = float(result["positive_review_ratio"])
    return result


@router.get("/categories")
def categories():
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT catalog_category_id, parent_id, category_name, item_type "
            "FROM catalog_categories ORDER BY catalog_category_id")).mappings()
        return {"categories": [dict(row) for row in rows]}
