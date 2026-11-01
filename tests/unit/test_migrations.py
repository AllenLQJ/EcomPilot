from sqlalchemy import text

from pilot.core import migrations
from pilot.core.db import get_engine


def columns():
    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns "
                                                "WHERE table_schema=DATABASE() AND table_name='aftersale_requests'"))}


def test_ensure_columns_adds_missing_column_once_and_is_idempotent(db_name):
    assert "result_reason" in columns()                                  # 新库直接来自 schema.sql
    assert migrations.ensure_columns(get_engine()) == []
    with get_engine().begin() as conn:                                   # 模拟早于该列的旧库
        conn.execute(text("ALTER TABLE aftersale_requests DROP COLUMN result_reason"))
    assert "result_reason" not in columns()
    assert migrations.ensure_columns(get_engine()) == ["aftersale_requests.result_reason"]
    assert "result_reason" in columns()
    assert migrations.ensure_columns(get_engine()) == []
