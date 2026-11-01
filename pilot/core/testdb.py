"""测试库工厂：每次创建 ecompilot_test_<uuid8>，导入 schema+seed，结束删除。"""
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text

from pilot.core import config, db

ROOT = Path(__file__).resolve().parents[2]


def _split(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def create_test_db(with_seed: bool = True) -> str:
    name = f"ecompilot_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(db.make_url(""), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
    admin.dispose()
    eng = create_engine(db.make_url(name))
    files = ["contracts/schema.sql"] + (["contracts/fixtures/seed.sql"] if with_seed else [])
    with eng.begin() as c:
        for f in files:
            for stmt in _split((ROOT / f).read_text(encoding="utf-8")):
                c.exec_driver_sql(stmt.replace("%", "%%"))
    eng.dispose()
    return name


def drop_test_db(name: str) -> None:
    assert name.startswith("ecompilot_test_")
    db._engines.pop(name, None)
    admin = create_engine(db.make_url(""), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
    admin.dispose()


@contextmanager
def test_database(with_seed: bool = True):
    name = create_test_db(with_seed)
    old = os.environ.get("ECOMPILOT_DB_NAME")
    os.environ["ECOMPILOT_DB_NAME"] = name
    config.get_settings.cache_clear()
    try:
        yield name
    finally:
        if old is None:
            os.environ.pop("ECOMPILOT_DB_NAME", None)
        else:
            os.environ["ECOMPILOT_DB_NAME"] = old
        config.get_settings.cache_clear()
        eng = db._engines.pop(name, None)
        if eng:
            eng.dispose()
        drop_test_db(name)
