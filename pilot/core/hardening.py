"""生产环境启动自检：ECOMPILOT_ENV=prod 时，仍在使用开发默认密钥/口令或开着开发入口就拒绝启动。

报错只列出"哪一项不合格"，不回显任何值。"""
import os

from pilot.core.config import Settings

DEV_DEFAULTS = {
    "jwt_secret": "dev-secret-change-me-dev-secret-change-me",
    "internal_token": "dev-internal-token",
    "db_password": "ecompilot_root",
}


def production_problems(s: Settings, environ=os.environ) -> list[str]:
    problems = []
    for name, default in DEV_DEFAULTS.items():
        if getattr(s, name) == default:
            problems.append(f"{name} 仍是开发默认值")
    if len(s.jwt_secret) < 32:
        problems.append("jwt_secret 少于 32 个字符")
    if len(s.internal_token) < 16:
        problems.append("internal_token 少于 16 个字符")
    if environ.get("ECOMPILOT_DEV_TOKENS") == "1":
        problems.append("ECOMPILOT_DEV_TOKENS=1：开发令牌入口不能在生产启用")
    return problems


def enforce(s: Settings, environ=os.environ) -> None:
    if s.env.lower() not in ("prod", "production"):
        return
    problems = production_problems(s, environ)
    if problems:
        raise RuntimeError("拒绝以生产模式启动：" + "；".join(problems))
