"""MySQL 连接。库名由 ECOMPILOT_DB_NAME 决定，测试通过独立库隔离。"""
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

from pilot.core.config import get_settings

_engines: dict[str, Engine] = {}


def make_url(db_name: str | None = None) -> URL:
    s = get_settings()
    return URL.create("mysql+pymysql", s.db_user, s.db_password, s.db_host, s.db_port,
                      db_name if db_name is not None else s.db_name, {"charset": "utf8mb4"})


def get_engine() -> Engine:
    s = get_settings()
    if s.db_name not in _engines:
        _engines[s.db_name] = create_engine(make_url(), pool_size=30, max_overflow=30,
                                            pool_pre_ping=True, isolation_level="READ COMMITTED")
    return _engines[s.db_name]
