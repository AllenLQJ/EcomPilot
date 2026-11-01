"""Process-local label cache, keyed by engine and label; exceptions are not cached."""
from functools import lru_cache
from sqlalchemy import text


@lru_cache(maxsize=1024)
def _resolve(engine, label):
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT catalog_category_id, brand_hint, item_type FROM nlu_label_to_catalog_map "
            "WHERE nlu_label=:label"), {"label": label}).mappings().first()
    return dict(row) if row is not None else None


def resolve_label(engine, label) -> dict | None:
    result = _resolve(engine, label)
    return dict(result) if result is not None else None


resolve_label.cache_clear = _resolve.cache_clear
