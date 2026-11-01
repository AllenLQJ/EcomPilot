"""官方 A2A 协议网关（a2a-sdk 1.x，JSON-RPC）：把 Pilot 的五个代理按标准 A2A 对外暴露。

运行在独立环境 .venv-proto（SDK 需要比主栈更新的 pydantic/starlette，主栈不升级）。每个代理一个子路径：
  GET  /<name>/.well-known/agent-card.json     标准 Agent Card
  POST /<name>/                                JSON-RPC（message/send 等）
请求里的第一个 data part（JSON 对象）作为代理输入，转发给 Pilot 自带的 `POST /a2a/invoke`；
成功 → 任务 COMPLETED，输出放在名为 `output` 的 artifact（第 1 个 part 是 data，第 2 个 part 是原样 JSON 文本，整数金额在文本里不会变成浮点）；失败 → 任务 FAILED，状态消息含错误码。
Pilot 内部代理之间的互相调用仍是自定义 /a2a/invoke，没有改成官方协议。
"""
import json
import os

import httpx
from a2a.helpers import new_data_artifact, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

HOST = os.environ.get("A2A_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8090").rstrip("/")
AGENT_HOST = os.environ.get("PILOT_AGENT_HOST", "http://127.0.0.1")
TIMEOUT_S = float(os.environ.get("A2A_GATEWAY_TIMEOUT_S", "15"))

AGENTS = {   # name: (port, description, skills)；端口与 contracts/agents.md 一致
    "env_fit": (8011, "穿搭/环境适配建议", ["env_fit"]),
    "selection": (8012, "按需求从目录选品", ["selection"]),
    "pricing_order": (8013, "凑单与报价下单", ["pricing_order"]),
    "aftersales": (8014, "售后候选订单与申请", ["aftersales"]),
    "seeding_note": (8015, "种草笔记生成", ["seeding_note"]),
}


def whole_numbers(value):
    """A2A 的 data part 是 protobuf Struct，数字一律是 double；把整数值还原成 int（金额是整数分，不能变成 112000.0）。"""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {k: whole_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [whole_numbers(v) for v in value]
    return value


class InvokeExecutor(AgentExecutor):
    def __init__(self, name: str, base_url: str):
        self.name, self.base_url = name, base_url

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:     # SDK 要求先入队 Task，再发状态更新
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        await updater.start_work()
        payload = self._input(context)
        if payload is None:
            await updater.failed(new_text_message("INVALID_REQUEST: message needs a data part with a JSON object"))
            return
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(self.base_url + "/a2a/invoke", json={"trace_id": context.task_id, "input": payload})
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            await updater.failed(new_text_message(f"UPSTREAM_ERROR: {type(exc).__name__}"))
            return
        if r.status_code != 200 or body.get("status") != "ok":
            err = body.get("error") or {}
            await updater.failed(new_text_message(f"{err.get('code', 'UPSTREAM_ERROR')}: {err.get('message', '')}"[:300]))
            return
        # data part 是 protobuf Struct，数字在线路上是 112000.0；原样 JSON 文本另放一个 part，金额要精确时读它
        parts = [*new_data_artifact("output", body["output"], media_type="application/json").parts,
                 new_text_part(json.dumps(body["output"], ensure_ascii=False), media_type="application/json")]
        await updater.add_artifact(parts, name="output")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    @staticmethod
    def _input(context: RequestContext) -> dict | None:
        from google.protobuf.json_format import MessageToDict
        for part in context.message.parts:
            if part.WhichOneof("content") == "data":
                value = MessageToDict(part.data)
                return whole_numbers(value) if isinstance(value, dict) else None
        return None


def build_card(name: str, description: str, skills: list[str], public_url: str = HOST) -> pb.AgentCard:
    return pb.AgentCard(
        name=name, description=description, version="1.0.0",
        supported_interfaces=[pb.AgentInterface(url=f"{public_url}/{name}/", protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=pb.AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        skills=[pb.AgentSkill(id=s, name=s, description=description, tags=["ecompilot"]) for s in skills])


def build_app(agents: dict | None = None, public_url: str = HOST) -> Starlette:
    mounts = []
    for name, (port, description, skills) in (agents or AGENTS).items():
        base = f"{AGENT_HOST}:{port}" if isinstance(port, int) else str(port)
        card = build_card(name, description, skills, public_url)
        handler = DefaultRequestHandler(agent_executor=InvokeExecutor(name, base), task_store=InMemoryTaskStore(),
                                        agent_card=card)
        mounts.append(Mount(f"/{name}", routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, rpc_url="/")]))

    async def index(_):
        return JSONResponse({"agents": {n: f"{public_url}/{n}/.well-known/agent-card.json" for n in (agents or AGENTS)}})

    async def health(_):
        return JSONResponse({"status": "ok"})
    return Starlette(routes=[Route("/", index), Route("/health", health), *mounts])


app = build_app()
