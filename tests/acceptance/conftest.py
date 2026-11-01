"""AC-TX 验收测试基础设施：独立测试库 + 真实 uvicorn HTTP。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from pilot.core.auth import issue_token
from pilot.core.config import get_settings
from pilot.core.db import get_engine
from pilot.core.testdb import test_database

ROOT = Path(__file__).resolve().parents[2]
SKU_STOCK_TEST = "SKU_STOCK_TEST"
DEFAULT_JWT_SECRET = "dev-secret-change-me-dev-secret-change-me"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_health(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as e:  # noqa: BLE001 — 启动期连接拒绝属预期
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server /health not ready within {timeout}s (last_err={last_err!r})")


def auth(user_id: str) -> dict[str, str]:
    """返回 Authorization: Bearer <jwt> 头。"""
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


def make_quote(
    client: httpx.Client,
    headers: dict[str, str],
    items: list[dict],
    coupon_ids: list[str] | None = None,
) -> dict:
    body: dict = {"items": items}
    if coupon_ids is not None:
        body["coupon_ids"] = coupon_ids
    r = client.post("/api/v1/quotes", json=body, headers=headers)
    assert r.status_code == 201, (
        f"POST /api/v1/quotes expected 201, got {r.status_code}: {r.text}"
    )
    data = r.json()
    assert "quote_id" in data, f"quote response missing quote_id: {data}"
    return data


def clear_promo_rules() -> None:
    """清空满减规则，使实付 = 单价 × 数量，便于断言。"""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM promo_rules"))


def set_stock(sku_id: str, sellable_stock: int, *, reset_version: bool = True) -> None:
    if reset_version:
        sql = text(
            "UPDATE products_sku SET sellable_stock = :stock, version = 0 WHERE sku_id = :sku"
        )
    else:
        sql = text("UPDATE products_sku SET sellable_stock = :stock WHERE sku_id = :sku")
    with get_engine().begin() as conn:
        result = conn.execute(sql, {"stock": sellable_stock, "sku": sku_id})
        assert result.rowcount == 1, f"set_stock: sku {sku_id!r} not found"


def get_stock(sku_id: str) -> int:
    with get_engine().connect() as conn:
        val = conn.execute(
            text("SELECT sellable_stock FROM products_sku WHERE sku_id = :sku"),
            {"sku": sku_id},
        ).scalar()
    assert val is not None, f"get_stock: sku {sku_id!r} not found"
    return int(val)


def count_orders_by_idempotency_key(user_id: str, idempotency_key: str) -> int:
    with get_engine().connect() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM orders "
                "WHERE user_id = :uid AND idempotency_key = :k"
            ),
            {"uid": user_id, "k": idempotency_key},
        ).scalar()
    return int(n or 0)


def clear_orders() -> None:
    """删除全部订单及明细（FK：先 items 后 orders）。"""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM order_items"))
        conn.execute(text("DELETE FROM orders"))


def expire_quote(quote_id: str) -> None:
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                "UPDATE price_quotes SET expires_at = '2000-01-01 00:00:00' "
                "WHERE quote_id = :qid"
            ),
            {"qid": quote_id},
        )
        assert result.rowcount == 1, f"expire_quote: quote {quote_id!r} not found"


def get_order_status(order_id: str) -> str:
    with get_engine().connect() as conn:
        status = conn.execute(
            text("SELECT order_status FROM orders WHERE order_id = :oid"),
            {"oid": order_id},
        ).scalar()
    assert status is not None, f"order {order_id!r} not found"
    return str(status)


def get_coupon_status(user_coupon_id: str) -> str:
    with get_engine().connect() as conn:
        status = conn.execute(
            text("SELECT status FROM user_coupons WHERE user_coupon_id = :cid"),
            {"cid": user_coupon_id},
        ).scalar()
    assert status is not None, f"coupon {user_coupon_id!r} not found"
    return str(status)


def error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code")
    return None


@pytest.fixture()
def tx():
    """将 helper 注入测试，避免 import tests.acceptance.conftest（无 __init__.py）。"""
    return SimpleNamespace(
        SKU_STOCK_TEST=SKU_STOCK_TEST,
        auth=auth,
        make_quote=make_quote,
        clear_promo_rules=clear_promo_rules,
        set_stock=set_stock,
        get_stock=get_stock,
        count_orders_by_idempotency_key=count_orders_by_idempotency_key,
        clear_orders=clear_orders,
        expire_quote=expire_quote,
        get_order_status=get_order_status,
        get_coupon_status=get_coupon_status,
        error_code=error_code,
    )


@pytest.fixture()
def server(tmp_path):
    """function 级：独立库 + uvicorn 子进程，yield base_url。"""
    with test_database(with_seed=True) as db_name:
        port = _free_port()
        env = os.environ.copy()
        env["ECOMPILOT_DB_NAME"] = db_name
        env.setdefault("ECOMPILOT_JWT_SECRET", DEFAULT_JWT_SECRET)
        # 保证父进程 issue_token 与子进程验签使用同一密钥
        os.environ.setdefault("ECOMPILOT_JWT_SECRET", env["ECOMPILOT_JWT_SECRET"])
        get_settings.cache_clear()

        pythonpath = str(ROOT)
        if env.get("PYTHONPATH"):
            pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
        env["PYTHONPATH"] = pythonpath

        log_path = tmp_path / "uvicorn.log"
        log_f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pilot.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            try:
                _wait_health(base_url, timeout=20.0)
            except RuntimeError:
                log_f.flush()
                tail = log_path.read_text(encoding="utf-8")[-4000:]
                raise RuntimeError(
                    f"uvicorn not healthy at {base_url}; log tail:\n{tail}"
                ) from None
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            log_f.close()


@pytest.fixture()
def client(server: str):
    with httpx.Client(base_url=server, timeout=30.0) as c:
        yield c
