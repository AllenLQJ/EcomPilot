"""A2A 调用客户端（契约见 contracts/agents.md）。"""
import os

import httpx


class A2AError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


def invoke(url: str, input: dict, trace_id: str = "-", timeout: float = 3.0) -> dict:
    """调用代理。ECOMPILOT_A2A_MODE=official（默认）走官方 A2A JSON-RPC；legacy 走原来的 /a2a/invoke。"""
    if os.environ.get("ECOMPILOT_A2A_MODE", "official").lower() != "legacy":
        return _invoke_official(url, input, trace_id, timeout)
    return _invoke_legacy(url, input, trace_id, timeout)


def _invoke_official(url: str, input: dict, trace_id: str, timeout: float) -> dict:
    from pilot.agents import a2a_official
    try:
        return a2a_official.invoke_official(url, input, trace_id, timeout)
    except a2a_official.OfficialCallError as e:
        raise A2AError(e.code, e.message) from e
    except httpx.TimeoutException as e:
        raise A2AError("UPSTREAM_TIMEOUT", type(e).__name__) from e
    except Exception as e:    # noqa: BLE001 — 传输层/协议层的任何失败都是 UPSTREAM_ERROR，不回显细节
        if "Timeout" in type(e).__name__:
            raise A2AError("UPSTREAM_TIMEOUT", type(e).__name__) from e
        raise A2AError("UPSTREAM_ERROR", type(e).__name__) from e


def _invoke_legacy(url: str, input: dict, trace_id: str, timeout: float) -> dict:
    try:
        r = httpx.post(url.rstrip("/") + "/a2a/invoke", json={"trace_id": trace_id, "input": input}, timeout=timeout)
    except httpx.TimeoutException as e:
        raise A2AError("UPSTREAM_TIMEOUT", str(e)) from e
    except httpx.HTTPError as e:
        raise A2AError("UPSTREAM_ERROR", str(e)) from e
    if r.status_code != 200:
        raise A2AError("UPSTREAM_ERROR", f"http {r.status_code}")
    body = r.json()
    if body.get("status") != "ok":
        err = body.get("error") or {}
        raise A2AError(err.get("code", "UPSTREAM_ERROR"), err.get("message", ""))
    return body["output"]
