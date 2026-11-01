"""A2A 调用客户端（契约见 contracts/agents.md）。"""
import httpx


class A2AError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


def invoke(url: str, input: dict, trace_id: str = "-", timeout: float = 3.0) -> dict:
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
