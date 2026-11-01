"""Read-only inventory lookup."""
from sqlalchemy import text


def get_stock(engine, sku_id):
    with engine.connect() as connection:
        row = connection.execute(text(
            'SELECT sellable_stock, defective_stock, version FROM products_sku WHERE sku_id=:sku_id'
        ), {'sku_id': sku_id}).mappings().first()
    return dict(row) if row is not None else None
