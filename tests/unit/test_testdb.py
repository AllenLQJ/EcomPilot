from sqlalchemy import text

from pilot.core.db import get_engine


def test_seed_loaded(db_name):
    with get_engine().connect() as c:
        assert c.execute(text("select count(*) from products_sku where is_addon_pool=1")).scalar() == 15
        assert c.execute(text("select sellable_stock from products_sku where sku_id='SKU_HEATER_003'")).scalar() == 50
