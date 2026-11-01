"""T1-F 专用辅助（非 pytest 自动 conftest；由本任务测试显式 import）。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import text

from pilot.core.auth import issue_token
from pilot.core.db import get_engine
from pilot.core.testdb import test_database

ROOT = Path(__file__).resolve().parents[2]
F_INTERNAL_TOKEN = "dev-internal-token"
F_TENANT_ID = "SHOP_SELF_01"
F_USER_A = "USR_A"
F_USER_B = "USR_B"
F_HEATER_SKU = "SKU_HEATER_003"
F_HEATER_PAYABLE_CENTS = 112000


@dataclass(frozen=True)
class FStack:
    db_name: str
    pilot_base_url: str
    ops_base_url: str
    nlu_base_url: str
    internal_token: str = F_INTERNAL_TOKEN


def f_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def f_wait_ready(base_url: str, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        for path in ("/health", "/openapi.json"):
            try:
                r = httpx.get(f"{base_url}{path}", timeout=1.5)
                if r.status_code < 500:
                    return
            except Exception as exc:  # noqa: BLE001 — 探活重试
                last = exc
        time.sleep(0.15)
    raise RuntimeError(f"service not ready at {base_url}: {last}")


def f_stop_procs(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)


def f_bearer(user_id: str, *, tenant_id: str = F_TENANT_ID, ttl: int = 3600) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id, tenant_id=tenant_id, ttl=ttl)}"}


def f_sql_scalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def f_sql_one(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).mappings().first()


def f_count_aftersale_requests() -> int:
    return int(f_sql_scalar("SELECT COUNT(*) FROM aftersale_requests") or 0)


def f_order_status(order_id: str) -> str:
    return str(f_sql_scalar("SELECT order_status FROM orders WHERE order_id = :oid", {"oid": order_id}))


def f_defective_stock(sku_id: str = F_HEATER_SKU) -> int:
    return int(
        f_sql_scalar("SELECT defective_stock FROM products_sku WHERE sku_id = :sid", {"sid": sku_id}) or 0
    )


def f_refund_rows_for_claim(claim_no: str) -> list[dict]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT refund_execution_id, refund_idempotency_key, claim_no, "
                "order_id, refunded_cents, execution_status, order_status_after "
                "FROM refund_executions WHERE claim_no = :c"
            ),
            {"c": claim_no},
        ).mappings().all()
        return [dict(r) for r in rows]


def f_assert_approve_http_ok(status_code: int) -> None:
    """桩 approve 响应体不做强假设；允许 200/202/504，或其它非 5xx。"""
    assert status_code in (200, 202, 504) or status_code < 500, status_code


def f_place_heater_order(
    stack: FStack,
    user_id: str,
    *,
    pay: bool = True,
    fulfill: bool = True,
    cancel: bool = False,
    sku_id: str = F_HEATER_SKU,
) -> dict:
    """走 quotes → create → [pay] → [fulfill]；cancel=True 时在 STOCK_LOCKED 取消。"""
    headers = f_bearer(user_id)
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        q = client.post("/api/v1/quotes", headers=headers, json={"items": [{"sku_id": sku_id, "qty": 1}]})
        assert q.status_code == 201, q.text
        quote = q.json()
        assert quote["total_payable_cents"] == F_HEATER_PAYABLE_CENTS

        c = client.post(
            "/api/v1/orders/create",
            headers=headers,
            json={"idempotency_key": f"IDK_{uuid.uuid4().hex}", "quote_id": quote["quote_id"]},
        )
        assert c.status_code in (200, 201), c.text
        order = c.json()
        order_id = order["order_id"]
        item_id = order["items"][0]["order_item_id"]

        if cancel:
            r = client.post(f"/api/v1/orders/{order_id}/cancel", headers=headers)
            assert r.status_code == 200, r.text
            return {
                "order_id": order_id,
                "order_item_id": item_id,
                "order_status": r.json().get("order_status", f_order_status(order_id)),
                "total_payable_cents": order["total_payable_cents"],
            }

        if pay:
            r = client.post(f"/api/v1/orders/{order_id}/pay", headers=headers)
            assert r.status_code == 200, r.text
            order = r.json()

        if fulfill:
            assert pay, "fulfill requires pay=True"
            r = client.post(
                f"/internal/v1/orders/{order_id}/fulfill",
                headers={"X-Internal-Token": stack.internal_token},
            )
            assert r.status_code == 200, r.text
            order = r.json()

        return {
            "order_id": order_id,
            "order_item_id": item_id,
            "order_status": order.get("order_status", f_order_status(order_id)),
            "total_payable_cents": order.get("total_payable_cents", F_HEATER_PAYABLE_CENTS),
        }


def f_confirm_aftersale(
    stack: FStack,
    user_id: str,
    order_id: str,
    order_item_id: str,
    *,
    extra_body: dict | None = None,
    reason: str = "热水器漏水了，要求全额退货退款",
) -> httpx.Response:
    body = {
        "order_id": order_id,
        "order_item_id": order_item_id,
        "buyer_reason_text": reason,
        "evidence_urls": ["https://storage.example.invalid/evidence/e1.jpg"],
    }
    if extra_body:
        body.update(extra_body)
    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        return client.post("/api/v1/aftersales/confirm", headers=f_bearer(user_id), json=body)


@contextmanager
def f_live_stack_cm():
    """独立测试库 + Pilot / Ops 桩 / NLU 桩 三进程。"""
    with test_database(with_seed=True) as db_name:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM promo_rules"))

        pilot_port, ops_port, nlu_port = f_free_port(), f_free_port(), f_free_port()
        pilot_url = f"http://127.0.0.1:{pilot_port}"
        ops_url = f"http://127.0.0.1:{ops_port}"
        nlu_url = f"http://127.0.0.1:{nlu_port}"

        base_env = os.environ.copy()
        base_env["PYTHONPATH"] = (
            str(ROOT) + (os.pathsep + base_env["PYTHONPATH"] if base_env.get("PYTHONPATH") else "")
        )

        env_pilot = base_env.copy()
        env_pilot["ECOMPILOT_DB_NAME"] = db_name
        env_pilot["ECOMPILOT_OPS_URL"] = ops_url
        env_pilot["ECOMPILOT_NLU_URL"] = nlu_url
        env_pilot["ECOMPILOT_INTERNAL_TOKEN"] = F_INTERNAL_TOKEN

        env_ops = base_env.copy()
        env_ops["PILOT_BASE_URL"] = pilot_url
        env_ops["PILOT_INTERNAL_TOKEN"] = F_INTERNAL_TOKEN

        env_nlu = base_env.copy()

        procs: list[subprocess.Popen] = []
        try:
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "stubs.nlu_stub:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(nlu_port),
                    ],
                    cwd=str(ROOT),
                    env=env_nlu,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "stubs.ops_stub:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(ops_port),
                    ],
                    cwd=str(ROOT),
                    env=env_ops,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "pilot.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(pilot_port),
                    ],
                    cwd=str(ROOT),
                    env=env_pilot,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            f_wait_ready(nlu_url)
            f_wait_ready(ops_url)
            f_wait_ready(pilot_url)
            yield FStack(
                db_name=db_name,
                pilot_base_url=pilot_url,
                ops_base_url=ops_url,
                nlu_base_url=nlu_url,
            )
        finally:
            f_stop_procs(procs)
