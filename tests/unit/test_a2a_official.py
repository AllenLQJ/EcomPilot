"""Pilot 代理的官方 A2A（a2a-sdk 0.3）：真实 HTTP 往返，覆盖成功、错误映射、超时、不可达、legacy 模式、整数保持。不需要数据库。"""
import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import HTTPException

from pilot.agents import a2a_client
from pilot.agents.a2a_client import A2AError
from pilot.agents.base import AgentError, make_agent_app


def handler(data, trace_id):
    mode = data.get("mode")
    if mode == "agent_error":
        raise AgentError("OUT_OF_STOCK", "没货了")
    if mode == "http_400":
        raise HTTPException(400, detail={"code": "INVALID_REQUEST", "message": "city 格式错误"})
    if mode == "http_str":
        raise HTTPException(400, detail="INVALID_REQUEST")
    if mode == "boom":
        raise RuntimeError("secret internal path /etc/passwd")
    if mode == "slow":
        time.sleep(2)
    return {"echo": data, "trace_id": trace_id, "cents": 112000}


@pytest.fixture(scope="module")
def agent_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = make_agent_app("demo", port, ["demo_skill"], handler)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_agent_card_is_standard_and_legacy_routes_still_exist(agent_url):
    card = httpx.get(agent_url + "/.well-known/agent-card.json").json()
    assert card["name"] == "demo" and card["skills"][0]["id"] == "demo_skill"
    assert card["url"] == agent_url + "/a2a/rpc" and card["preferredTransport"] == "JSONRPC"
    assert httpx.get(agent_url + "/.well-known/agent.json").json()["skills"] == ["demo_skill"]
    legacy = httpx.post(agent_url + "/a2a/invoke", json={"trace_id": "t", "input": {"a": 1}}).json()
    assert legacy["status"] == "ok" and legacy["output"]["cents"] == 112000
    assert httpx.get(agent_url + "/health").json() == {"status": "ok", "agent": "demo"}


def test_official_call_roundtrip_keeps_integers_and_trace_id(agent_url):
    out = a2a_client.invoke(agent_url, {"qty": 2, "unit_price_cents": 112000, "ratio": 0.98, "nested": [{"n": 3}]}, trace_id="trace-xyz")
    assert out["cents"] == 112000 and type(out["cents"]) is int
    assert out["echo"] == {"qty": 2, "unit_price_cents": 112000, "ratio": 0.98, "nested": [{"n": 3}]}
    assert type(out["echo"]["unit_price_cents"]) is int and out["trace_id"] == "trace-xyz"


@pytest.mark.parametrize("mode,code,message", [
    ("agent_error", "OUT_OF_STOCK", "没货了"),
    ("http_400", "INVALID_REQUEST", "city 格式错误"),
    ("http_str", "INVALID_REQUEST", ""),
])
def test_agent_failures_map_to_a2a_error_with_code(agent_url, mode, code, message):
    with pytest.raises(A2AError) as info:
        a2a_client.invoke(agent_url, {"mode": mode})
    assert info.value.code == code and message in info.value.message


def test_unexpected_exception_does_not_leak_details(agent_url):
    with pytest.raises(A2AError) as info:
        a2a_client.invoke(agent_url, {"mode": "boom"})
    assert info.value.code == "INTERNAL_ERROR" and "passwd" not in str(info.value) and "secret" not in str(info.value)


def test_timeout_and_unreachable_have_distinct_codes(agent_url):
    with pytest.raises(A2AError) as slow:
        a2a_client.invoke(agent_url, {"mode": "slow"}, timeout=0.5)
    assert slow.value.code == "UPSTREAM_TIMEOUT"
    with pytest.raises(A2AError) as down:
        a2a_client.invoke("http://127.0.0.1:9", {"x": 1}, timeout=1.0)
    assert down.value.code == "UPSTREAM_ERROR"


def test_legacy_mode_uses_the_old_endpoint(agent_url, monkeypatch):
    monkeypatch.setenv("ECOMPILOT_A2A_MODE", "legacy")
    monkeypatch.setattr(a2a_client, "_invoke_official", lambda *a: (_ for _ in ()).throw(AssertionError("must not use official")))
    assert a2a_client.invoke(agent_url, {"qty": 1})["cents"] == 112000


async def test_official_invoke_inside_a_running_event_loop_does_not_crash(agent_url):
    out = await asyncio.to_thread(a2a_client.invoke, agent_url, {"qty": 1})        # 常规：线程里没有循环
    assert out["cents"] == 112000
    out = a2a_client.invoke(agent_url, {"qty": 2})                                  # 当前线程有运行中的循环：换线程执行
    assert out["echo"]["qty"] == 2


def test_message_without_exactly_one_data_part_is_rejected(agent_url):
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import Message, Part, Role, TextPart
    from pilot.agents.a2a_official import _card

    async def go():
        async with httpx.AsyncClient() as http:
            client = ClientFactory(ClientConfig(streaming=False, httpx_client=http)).create(_card(agent_url))
            msg = Message(role=Role.user, message_id="m1", parts=[Part(root=TextPart(text="帮我选个热水器"))])
            return [e async for e in client.send_message(msg)]
    events = asyncio.run(go())
    task = events[-1][0] if isinstance(events[-1], tuple) else events[-1]
    assert task.status.state.value == "failed" and "INVALID_REQUEST" in task.status.message.parts[0].root.text
