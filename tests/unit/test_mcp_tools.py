from sqlalchemy import text

from pilot.core.db import get_engine
from pilot.mcp_tools.bundle_tool import suggest_bundle
from pilot.mcp_tools.image_fallback import resolve_image
from pilot.mcp_tools.inventory_tool import get_stock


def test_suggest_bundle_seed(db_name):
    # 5 shampoos cost 29500; the next tier is 500 cents away.
    # Cheapest addon costs 590: 30090 - 4000 - 800 = 25290.
    # Baseline: 29500 - 1000 - 800 = 27700, saving 2410.
    engine = get_engine()
    with engine.connect() as c:
        product = dict(c.execute(text(
            "SELECT sku_id, shop_id, catalog_category_id, unit_price_cents "
            "FROM products_sku WHERE sku_id='SKU_SHAMPOO_001'"
        )).mappings().one())
        coupons = [dict(r) for r in c.execute(text(
            "SELECT user_coupon_id, catalog_category_id, threshold_cents, discount_cents "
            "FROM user_coupons WHERE user_id='USR_A'"
        )).mappings()]
    product['qty'] = 5
    result = suggest_bundle(engine, 'SHOP_SELF_01', [product], coupons)
    assert result == {
        'addons': [dict(sku_id='SKU_ADDON_15', shop_id='SHOP_C',
                        catalog_category_id='DAILY_GENERAL', unit_price_cents=590, qty=1)],
        'payable_with_addon_cents': 25290, 'payable_without_addon_cents': 27700,
        'saving_cents': 2410,
    }
    assert suggest_bundle(engine, 'other_tenant', [product], coupons) is None
    with engine.begin() as c:
        c.execute(text("UPDATE promo_rules SET is_active=0 WHERE rule_id='PROMO_300_40'"))
    assert suggest_bundle(engine, 'SHOP_SELF_01', [product], coupons) is None


def test_get_stock(db_name):
    engine = get_engine()
    assert get_stock(engine, 'SKU_STOCK_TEST') == dict(sellable_stock=10, defective_stock=0, version=0)
    assert get_stock(engine, 'missing') is None
    assert get_stock(engine, 'SKU_STOCK_TEST') == dict(sellable_stock=10, defective_stock=0, version=0)


def test_image_fallback(db_name):
    engine = get_engine()
    assert resolve_image(engine, 'SKU_ADDON_01') == {
        'url': '/static/default/DAILY_GENERAL.png', 'source': 'category_default'}
    with engine.begin() as c:
        c.execute(text("UPDATE products_sku SET image_url=:url WHERE sku_id='SKU_ADDON_02'"),
                  {'url': '/gallery/toothbrush.png'})
        c.execute(text("UPDATE products_sku SET image_url='' WHERE sku_id='SKU_ADDON_01'"))
    assert resolve_image(engine, 'SKU_ADDON_01') == {
        'url': '/gallery/toothbrush.png', 'source': 'gallery'}
    assert resolve_image(engine, 'SKU_ADDON_02') == {
        'url': '/gallery/toothbrush.png', 'source': 'primary'}
    with engine.begin() as c:
        c.execute(text("UPDATE products_sku SET image_url=:url WHERE sku_id='SKU_ADDON_01'"),
                  {'url': '/primary/tissue.png'})
    assert resolve_image(engine, 'SKU_ADDON_01') == {
        'url': '/primary/tissue.png', 'source': 'primary'}
