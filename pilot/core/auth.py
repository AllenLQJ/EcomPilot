"""JWT 身份依赖。身份只来自 Token，请求体中的 user_id/tenant_id 一律忽略。"""
import time
from dataclasses import dataclass

import jwt
from fastapi import Header

from pilot.core.config import get_settings
from pilot.core.errors import ApiError


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant_id: str
    role: str = "buyer"


def issue_token(user_id: str, tenant_id: str = "SHOP_SELF_01", role: str = "buyer", ttl: int = 3600) -> str:
    s = get_settings()
    now = int(time.time())
    return jwt.encode({"sub": user_id, "tenant_id": tenant_id, "role": role, "iat": now, "exp": now + ttl},
                      s.jwt_secret, algorithm=s.jwt_alg)


def current_identity(authorization: str | None = Header(default=None)) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token")
    s = get_settings()
    try:
        p = jwt.decode(authorization[7:].strip(), s.jwt_secret, algorithms=[s.jwt_alg])
    except jwt.PyJWTError:
        raise ApiError(401, "UNAUTHORIZED", "invalid or expired token")
    if not p.get("sub") or not p.get("tenant_id"):
        raise ApiError(401, "UNAUTHORIZED", "token missing identity")
    return Identity(p["sub"], p["tenant_id"], p.get("role", "buyer"))


def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != get_settings().internal_token:
        raise ApiError(401, "INTERNAL_AUTH_FAILED", "bad internal token")
