"""Read tenant promotions and addon pool for the pure bundle calculator."""
from sqlalchemy import text

from pilot.bundle.bundle_math_tools import suggest_addons


def suggest_bundle(engine, tenant_id, lines, coupons):
    with engine.connect() as connection:
        rules = [dict(row) for row in connection.execute(text(
            'SELECT rule_id, threshold_cents, discount_cents FROM promo_rules '
            'WHERE tenant_id=:tenant_id AND is_active=1 ORDER BY threshold_cents, rule_id'
        ), {'tenant_id': tenant_id}).mappings()]
        pool = [dict(row) for row in connection.execute(text(
            'SELECT sku_id, shop_id, catalog_category_id, unit_price_cents, '
            'sellable_stock, positive_review_ratio FROM products_sku '
            'WHERE tenant_id=:tenant_id AND is_addon_pool=1'
        ), {'tenant_id': tenant_id}).mappings()]
    return suggest_addons(lines, coupons, rules, pool)
