"""启动时的幂等补列：已有数据库（早于 contracts/schema.sql 的新列）不需要手工迁移。MySQL 8 的 ADD COLUMN 没有 IF NOT EXISTS，
所以先查 information_schema。新建的库（测试库、容器首次初始化）直接来自 schema.sql，这里什么都不做。"""
from sqlalchemy import text

COLUMNS = [
    ("aftersale_requests", "result_reason", "VARCHAR(200) NULL"),
]


def ensure_columns(engine) -> list[str]:
    added = []
    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            exists = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() "
                "AND table_name=:t AND column_name=:c"), {"t": table, "c": column}).scalar()
            if not exists:
                conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}"))
                added.append(f"{table}.{column}")
    return added
