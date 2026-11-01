from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ECOMPILOT_", env_file=".env", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = 3307
    db_user: str = "root"
    db_password: str = "ecompilot_root"
    db_name: str = "ecompilot"
    jwt_secret: str = "dev-secret-change-me-dev-secret-change-me"
    jwt_alg: str = "HS256"
    internal_token: str = "dev-internal-token"
    nlu_url: str = "http://127.0.0.1:8001"
    rag_url: str = "http://127.0.0.1:8002"
    ops_url: str = "http://127.0.0.1:8000"
    quote_ttl_seconds: int = 900
    pay_timeout_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
