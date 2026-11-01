"""官方 A2A（a2a-sdk 0.3，JSON-RPC）：服务端把代理的 handler 暴露为标准 A2A 代理；客户端用官方 Client 调用。

服务端：每个代理应用除了原有的 `/a2a/invoke` 之外，多挂两个路由
  GET  /.well-known/agent-card.json   标准 Agent Card
  POST /a2a/rpc                       JSON-RPC（message/send 等）
请求里的第一个 data part（JSON 对象）是代理输入，成功时输出放在名为 `output` 的 data artifact；失败 → 任务 failed，
状态消息是 "<CODE>: <message>"。此协议里的 JSON 数字不会变成浮点，整数分在两端保持整数。

客户端 `invoke_official` 是 `a2a_client.invoke` 的官方协议实现（`ECOMPILOT_A2A_MODE=official`，默认），
`legacy` 仍走原来的 `/a2a/invoke`。
"""
import asyncio
import os
import threading
import uuid
from typing import Callable

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentSkill, DataPart, Message, Part, Role, TaskState,
                       TransportProtocol, UnsupportedOperationError)
from a2a.utils import get_data_parts, new_agent_text_message, new_task
from a2a.utils.errors import ServerError

RPC_PATH = "/a2a/rpc"
CARD_PATH = "/.well-known/agent-card.json"


class HandlerExecutor(AgentExecutor):
    def __init__(self, handler: Callable[[dict, str], dict], agent_error, http_exception):
        self.handler, self.agent_error, self.http_exception = handler, agent_error, http_exception

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        parts = get_data_parts(context.message.parts)
        if len(parts) != 1 or not isinstance(parts[0], dict):
            await updater.failed(new_agent_text_message("INVALID_REQUEST: message needs exactly one data part with a JSON object",
                                                        task.context_id, task.id))
            return
        trace_id = str((context.message.metadata or {}).get("trace_id") or task.id)
        try:
            output = await asyncio.to_thread(self.handler, parts[0], trace_id)
        except self.agent_error as exc:
            await self._fail(updater, task, exc.code, exc.message)
            return
        except self.http_exception as exc:
            detail = exc.detail
            code = detail.get("code") if isinstance(detail, dict) else str(detail)
            await self._fail(updater, task, code or "INVALID_REQUEST", detail.get("message", "") if isinstance(detail, dict) else "")
            return
        except Exception:    # noqa: BLE001 — 细节只留在服务端日志，不回给调用方
            await self._fail(updater, task, "INTERNAL_ERROR", "agent failed")
            return
        await updater.add_artifact([Part(root=DataPart(data=output))], name="output")
        await updater.complete()

    @staticmethod
    async def _fail(updater, task, code, message):
        await updater.failed(new_agent_text_message(f"{code}: {message}"[:300], task.context_id, task.id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())


def build_card(name: str, port: int, skills: list[str]) -> AgentCard:
    base = os.environ.get("ECOMPILOT_AGENT_PUBLIC_URL", f"http://127.0.0.1:{port}").rstrip("/")
    return AgentCard(
        name=name, description=f"EcomPilot {name} agent", version="1.0.0", url=base + RPC_PATH,
        preferred_transport="JSONRPC", capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        skills=[AgentSkill(id=s, name=s, description=f"{name}: {s}", tags=["ecompilot"]) for s in skills])


def mount(app, name: str, port: int, skills: list[str], handler, agent_error, http_exception) -> None:
    card = build_card(name, port, skills)
    request_handler = DefaultRequestHandler(agent_executor=HandlerExecutor(handler, agent_error, http_exception),
                                            task_store=InMemoryTaskStore())
    A2AFastAPIApplication(agent_card=card, http_handler=request_handler).add_routes_to_app(
        app, agent_card_url=CARD_PATH, rpc_url=RPC_PATH)


# ---------------------------------------------------------------- 客户端
class OfficialCallError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


def _card(base_url: str) -> AgentCard:
    """调用方已经知道代理地址，不需要先 GET Agent Card：直接构造一张只含传输信息的静态卡片。"""
    return AgentCard(name="pilot-agent", description="EcomPilot agent", version="1.0.0",
                     url=base_url.rstrip("/") + RPC_PATH, preferred_transport="JSONRPC",
                     capabilities=AgentCapabilities(streaming=False), default_input_modes=["application/json"],
                     default_output_modes=["application/json"], skills=[])


async def _invoke(url: str, data: dict, trace_id: str, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as http:
        client = ClientFactory(ClientConfig(streaming=False, httpx_client=http,
                                            supported_transports=[TransportProtocol.jsonrpc])).create(_card(url))
        message = Message(role=Role.user, message_id=uuid.uuid4().hex, parts=[Part(root=DataPart(data=data))],
                          metadata={"trace_id": trace_id})
        final = None
        async for event in client.send_message(message):
            final = event[0] if isinstance(event, tuple) else event
    if final is None or not hasattr(final, "status"):
        raise OfficialCallError("UPSTREAM_ERROR", "no task in response")
    state = final.status.state
    if state == TaskState.completed:
        for artifact in final.artifacts or []:
            for output in get_data_parts(artifact.parts):
                if isinstance(output, dict):
                    return output
        raise OfficialCallError("UPSTREAM_ERROR", "completed task has no data output")
    text = ""
    if final.status.message and final.status.message.parts:
        text = "".join(getattr(p.root, "text", "") for p in final.status.message.parts)
    code, _, message_text = text.partition(": ")
    raise OfficialCallError(code if code.isupper() and " " not in code else "UPSTREAM_ERROR", message_text or text)


def invoke_official(url: str, data: dict, trace_id: str = "-", timeout: float = 3.0) -> dict:
    """同步入口。调用方线程里通常没有事件循环（FastAPI 的线程池、worker 线程）；万一有，就换一个线程去跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_invoke(url, data, trace_id, timeout))
    box: dict = {}

    def runner():
        try:
            box["value"] = asyncio.run(_invoke(url, data, trace_id, timeout))
        except BaseException as exc:    # noqa: BLE001 — 原样带回调用线程
            box["error"] = exc
    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
