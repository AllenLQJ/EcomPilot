"""T2-D 全链路 e2e：独立库 + 9 进程起栈（仅 tests/e2e/ 使用）。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from sqlalchemy import text

from pilot.core.db import get_engine
from pilot.core.testdb import test_database

ROOT = Path(__file__).resolve().parents[2]
INTERNAL_TOKEN = "dev-internal-token"
TENANT_ID = "SHOP_SELF_01"
HEATER_SKU = "SKU_HEATER_003"
HEATER_PAYABLE_CENTS = 112000
HEALTH_TIMEOUT_S = 30.0

AGENT_MODULES = (
    ("env_fit", "pilot.agents.env_fit_agent"),
    ("selection", "pilot.agents.selection_agent"),
    ("pricing", "pilot.agents.pricing_order_agent"),
    ("aftersales", "pilot.agents.aftersales_agent"),
    ("seeding", "pilot.agents.seeding_note_agent"),
)


@dataclass
class E2EStack:
    db_name: str
    pilot_base_url: str
    ops_base_url: str
    nlu_base_url: str
    rag_base_url: str
    agent_urls: dict[str, str]
    internal_token: str = INTERNAL_TOKEN
    selection_proc: subprocess.Popen | None = field(default=None, repr=False)
    _procs: list[subprocess.Popen] = field(default_factory=list, repr=False)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_health(base_url: str, timeout: float = HEALTH_TIMEOUT_S) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.5)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 — 启动期连接拒绝属预期
            last = exc
        time.sleep(0.15)
    raise RuntimeError(f"/health not ready at {base_url} within {timeout}s: {last}")


def stop_procs(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)


def parse_sse(text_body: str) -> list[dict[str, Any]]:
    """按空行分块解析 SSE，返回 [{event, data}, ...]；data 尽量 JSON 解码。"""
    events: list[dict[str, Any]] = []
    # 兼容 \r\n；按空行分块
    normalized = text_body.replace("\r\n", "\n")
    for block in normalized.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines and event_name == "message":
            continue
        raw = "\n".join(data_lines)
        parsed: Any = raw
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
        events.append({"event": event_name, "data": parsed})
    return events


def sse_cards(events: list[dict[str, Any]], card_type: str | None = None) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("event") != "card":
            continue
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        if card_type is None or data.get("type") == card_type:
            cards.append(data)
    return cards


def sse_tokens_text(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for ev in events:
        if ev.get("event") != "token":
            continue
        data = ev.get("data")
        if isinstance(data, dict) and "text" in data:
            parts.append(str(data["text"]))
        elif isinstance(data, str):
            parts.append(data)
    return "".join(parts)


def sse_errors(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("event") == "error" and isinstance(ev.get("data"), dict):
            out.append(ev["data"])
    return out


def sql_scalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def sql_all(sql: str, params: dict | None = None) -> list[dict]:
    with get_engine().connect() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
        return [dict(r) for r in rows]


def order_status(order_id: str) -> str:
    val = sql_scalar("SELECT order_status FROM orders WHERE order_id = :oid", {"oid": order_id})
    assert val is not None, f"order {order_id!r} not found in DB"
    return str(val)


def count_aftersale_requests() -> int:
    return int(sql_scalar("SELECT COUNT(*) FROM aftersale_requests") or 0)


def refund_rows_for_claim(claim_no: str) -> list[dict]:
    return sql_all(
        "SELECT refund_execution_id, refund_idempotency_key, claim_no, "
        "order_id, refunded_cents, execution_status, order_status_after "
        "FROM refund_executions WHERE claim_no = :c",
        {"c": claim_no},
    )


def _base_env(db_name: str, urls: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["ECOMPILOT_DB_NAME"] = db_name
    env["ECOMPILOT_DEV_TOKENS"] = "1"
    env["ECOMPILOT_INTERNAL_TOKEN"] = INTERNAL_TOKEN
    env["ECOMPILOT_OPS_URL"] = urls["ops"]
    env["ECOMPILOT_NLU_URL"] = urls["nlu"]
    env["ECOMPILOT_RAG_URL"] = urls["rag"]
    env["ECOMPILOT_PILOT_URL"] = urls["pilot"]
    env["ECOMPILOT_AGENT_ENV_FIT_URL"] = urls["env_fit"]
    env["ECOMPILOT_AGENT_SELECTION_URL"] = urls["selection"]
    env["ECOMPILOT_AGENT_PRICING_URL"] = urls["pricing"]
    env["ECOMPILOT_AGENT_AFTERSALES_URL"] = urls["aftersales"]
    env["ECOMPILOT_AGENT_SEEDING_URL"] = urls["seeding"]
    return env


def _popen_uvicorn(module: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _allocate_urls() -> tuple[dict[str, int], dict[str, str]]:
    ports = {
        "nlu": free_port(),
        "rag": free_port(),
        "ops": free_port(),
        "env_fit": free_port(),
        "selection": free_port(),
        "pricing": free_port(),
        "aftersales": free_port(),
        "seeding": free_port(),
        "pilot": free_port(),
    }
    urls = {k: f"http://127.0.0.1:{p}" for k, p in ports.items()}
    return ports, urls


def start_e2e_stack(db_name: str) -> E2EStack:
    ports, urls = _allocate_urls()
    base = _base_env(db_name, urls)

    env_ops = base.copy()
    env_ops["PILOT_BASE_URL"] = urls["pilot"]
    env_ops["PILOT_INTERNAL_TOKEN"] = INTERNAL_TOKEN

    env_stub = base.copy()  # nlu/rag 不强制依赖 Pilot

    procs: list[subprocess.Popen] = []
    selection_proc: subprocess.Popen | None = None

    procs.append(_popen_uvicorn("stubs.nlu_stub:app", ports["nlu"], env_stub))
    procs.append(_popen_uvicorn("stubs.rag_stub:app", ports["rag"], env_stub))
    procs.append(_popen_uvicorn("stubs.ops_stub:app", ports["ops"], env_ops))

    for key, module in AGENT_MODULES:
        proc = _popen_uvicorn(f"{module}:app", ports[key], base)
        procs.append(proc)
        if key == "selection":
            selection_proc = proc

    procs.append(_popen_uvicorn("pilot.main:app", ports["pilot"], base))

    try:
        wait_health(urls["nlu"])
        wait_health(urls["rag"])
        wait_health(urls["ops"])
        for key, _ in AGENT_MODULES:
            wait_health(urls[key])
        wait_health(urls["pilot"])
    except Exception:
        stop_procs(procs)
        raise

    return E2EStack(
        db_name=db_name,
        pilot_base_url=urls["pilot"],
        ops_base_url=urls["ops"],
        nlu_base_url=urls["nlu"],
        rag_base_url=urls["rag"],
        agent_urls={k: urls[k] for k, _ in AGENT_MODULES},
        selection_proc=selection_proc,
        _procs=procs,
    )


def chat_stream(
    stack: E2EStack,
    token: str,
    *,
    session_id: str,
    message: str,
    confirmed_order_id: str | None = None,
    confirmed_order_item_id: str | None = None,
    extra_body: dict | None = None,
    timeout: float = 60.0,
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    body: dict[str, Any] = {"session_id": session_id, "message": message}
    if confirmed_order_id is not None:
        body["confirmed_order_id"] = confirmed_order_id
    if confirmed_order_item_id is not None:
        body["confirmed_order_item_id"] = confirmed_order_item_id
    if extra_body:
        body.update(extra_body)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=stack.pilot_base_url, timeout=timeout) as client:
        r = client.post("/api/v1/chat/stream", headers=headers, json=body)
        events = parse_sse(r.text) if r.status_code == 200 else []
        return r, events


def issue_dev_token(stack: E2EStack, user_id: str, tenant_id: str = TENANT_ID) -> str:
    with httpx.Client(base_url=stack.pilot_base_url, timeout=15.0) as client:
        r = client.post("/api/v1/dev/token", json={"user_id": user_id, "tenant_id": tenant_id})
        assert r.status_code == 200, f"POST /api/v1/dev/token failed: {r.status_code} {r.text}"
        body = r.json()
        token = body.get("token") or body.get("access_token")
        assert token, f"dev/token response missing token: {body}"
        return str(token)


def unique_user_id(prefix: str = "USR_E2E") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def e2e_stack() -> Iterator[E2EStack]:
    """module 级：独立库 + 清空 promo_rules + 9 个 uvicorn 子进程。"""
    with test_database(with_seed=True) as db_name:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM promo_rules"))
        stack = start_e2e_stack(db_name)
        try:
            yield stack
        finally:
            stop_procs(stack._procs)


@pytest.fixture
def e2e_stack_killable_selection() -> Iterator[E2EStack]:
    """function 级独立栈：selection 子进程可单独 terminate，不影响 module 栈。"""
    with test_database(with_seed=True) as db_name:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM promo_rules"))
        stack = start_e2e_stack(db_name)
        try:
            yield stack
        finally:
            stop_procs(stack._procs)
