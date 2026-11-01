"""Resolve primary, category gallery, then category default images."""
from sqlalchemy import text


def resolve_image(engine, sku_id):
    with engine.connect() as connection:
        product = connection.execute(text(
            'SELECT catalog_category_id, image_url FROM products_sku WHERE sku_id=:sku_id'
        ), {'sku_id': sku_id}).mappings().one()
        if product['image_url']:
            return {'url': product['image_url'], 'source': 'primary'}
        category = product['catalog_category_id']
        image = connection.execute(text(
            "SELECT image_url FROM products_sku WHERE catalog_category_id=:category "
            "AND image_url IS NOT NULL AND image_url<>'' ORDER BY sku_id LIMIT 1"
        ), {'category': category}).scalar()
    if image:
        return {'url': image, 'source': 'gallery'}
    return {'url': f'/static/default/{category}.png', 'source': 'category_default'}
