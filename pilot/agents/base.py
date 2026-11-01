"""代理应用工厂：统一 /health、agent card、/a2a/invoke 协议。"""
from typing import Callable

from fastapi import FastAPI
from pydantic import BaseModel


class InvokeIn(BaseModel):
    trace_id: str = "-"
    input: dict


class AgentError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code, self.message = code, message or code


def make_agent_app(name: str, port: int, skills: list[str], handler: Callable[[dict, str], dict]) -> FastAPI:
    """handler(input, trace_id) -> output dict；抛 AgentError 转为 status=error。"""
    app = FastAPI(title=name)

    @app.get("/health")
    def health():
        return {"status": "ok", "agent": name}

    @app.get("/.well-known/agent.json")
    def card():
        return {"name": name, "port": port, "skills": skills}

    @app.post("/a2a/invoke")
    def invoke(body: InvokeIn):
        try:
            return {"status": "ok", "output": handler(body.input, body.trace_id)}
        except AgentError as e:
            return {"status": "error", "error": {"code": e.code, "message": e.message}}

    return app
