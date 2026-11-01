"""协议层测试在 .venv-proto 里运行（见 requirements-protocols.txt），不依赖数据库。"""
import socket
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """app 可以稍后设置（先知道端口才能生成指向自己的 Agent Card）。"""
    def __init__(self, app=None):
        self.port = free_port()
        self.app = app
        self.server = uvicorn.Server(uvicorn.Config(self, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    def __enter__(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def downstream():
    """假的 Pilot 代理：/a2a/invoke 按输入里的 mode 返回成功/业务错误/500。"""
    seen = []

    async def invoke(request: Request):
        body = await request.json()
        seen.append(body)
        mode = body["input"].get("mode")
        if mode == "boom":
            return JSONResponse({"detail": "x"}, status_code=500)
        if mode == "error":
            return JSONResponse({"status": "error", "error": {"code": "INVALID_INPUT", "message": "bad needs"}})
        return JSONResponse({"status": "ok", "output": {"echo": body["input"], "trace_id": body["trace_id"]}})

    with Server(Starlette(routes=[Route("/a2a/invoke", invoke, methods=["POST"])])) as srv:
        srv.seen = seen
        yield srv
