"""T2-E 契约一致性辅助（非 pytest 自动 conftest；由 test_contract_*.py 显式 import）。"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import text

from pilot.core.auth import issue_token
from pilot.core.db import get_engine
from pilot.core.testdb import test_database

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
OPENAPI_DIR = CONTRACTS / "openapi"
ERRORS_MD = CONTRACTS / "errors.md"

INTERNAL_TOKEN = "dev-internal-token"
TENANT_ID = "SHOP_SELF_01"
USER_A = "USR_A"
SKU_HEATER = "SKU_HEATER_003"
SKU_STOCK_TEST = "SKU_STOCK_TEST"
DEFAULT_JWT_SECRET = "dev-secret-change-me-dev-secret-change-me"

_SKIP_SCHEMA_KEYS = frozenset(
    {
        "description",
        "example",
        "examples",
        "title",
        "xml",
        "externalDocs",
        "deprecated",
        "readOnly",
        "writeOnly",
        "discriminator",
        "nullable",
    }
)


@dataclass(frozen=True)
class PilotStack:
    db_name: str
    pilot_base_url: str
    ops_base_url: str
    nlu_base_url: str
    internal_token: str = INTERNAL_TOKEN


@dataclass(frozen=True)
class StubsStack:
    nlu_base_url: str
    rag_base_url: str
    ops_base_url: str


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 45.0) -> None:
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


def bearer(user_id: str = USER_A, *, tenant_id: str = TENANT_ID, ttl: int = 3600) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id, tenant_id=tenant_id, ttl=ttl)}"}


def internal_headers(token: str = INTERNAL_TOKEN) -> dict[str, str]:
    return {"X-Internal-Token": token}


def load_openapi(name: str) -> dict[str, Any]:
    path = OPENAPI_DIR / name
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise RuntimeError(f"OpenAPI file is not a mapping: {path}")
    return doc


def load_error_codes(path: Path = ERRORS_MD) -> set[str]:
    """从 contracts/errors.md 表格提取 error.code 集合。"""
    codes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        http_cell, code_cell = cells[0], cells[1]
        if http_cell in {"HTTP", "---"} or set(http_cell) <= {"-"}:
            continue
        if not re.fullmatch(r"\d{3}", http_cell):
            continue
        if not code_cell or code_cell == "code":
            continue
        codes.add(code_cell)
    if not codes:
        raise RuntimeError(f"no error codes parsed from {path}")
    return codes


def _resolve_pointer(root: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref (only local JSON Pointer): {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"$ref {ref!r} unresolved at {part!r}")
        node = node[part]
    return node


def openapi_to_json_schema(
    node: Any,
    root: dict[str, Any],
    *,
    _visiting: set[str] | None = None,
) -> Any:
    """OpenAPI 3.0 schema → Draft 2020-12 可校验 schema（nullable / 去非校验关键字 / 内联 $ref）。"""
    if not isinstance(node, dict):
        return node

    visiting = _visiting if _visiting is not None else set()

    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str):
            raise TypeError(f"$ref must be string, got {ref!r}")
        if ref in visiting:
            return True
        visiting.add(ref)
        try:
            return openapi_to_json_schema(_resolve_pointer(root, ref), root, _visiting=visiting)
        finally:
            visiting.discard(ref)

    nullable = bool(node.get("nullable"))
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SKIP_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                pk: openapi_to_json_schema(pv, root, _visiting=visiting) for pk, pv in value.items()
            }
        elif key == "items":
            out[key] = openapi_to_json_schema(value, root, _visiting=visiting)
        elif key in ("allOf", "oneOf", "anyOf") and isinstance(value, list):
            out[key] = [openapi_to_json_schema(item, root, _visiting=visiting) for item in value]
        elif key == "additionalProperties" and isinstance(value, dict):
            out[key] = openapi_to_json_schema(value, root, _visiting=visiting)
        elif key == "not":
            out[key] = openapi_to_json_schema(value, root, _visiting=visiting)
        else:
            out[key] = value

    if not nullable:
        return out

    typ = out.get("type")
    if isinstance(typ, str):
        out["type"] = [typ, "null"]
        return out
    if isinstance(typ, list):
        if "null" not in typ:
            out["type"] = [*typ, "null"]
        return out
    return {"anyOf": [out, {"type": "null"}]}


def operation(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    paths = spec.get("paths") or {}
    if path not in paths:
        raise KeyError(f"path {path!r} not in OpenAPI")
    item = paths[path]
    m = method.lower()
    if m not in item:
        raise KeyError(f"{method.upper()} {path} not in OpenAPI")
    return item[m]


def declared_status_codes(spec: dict[str, Any], path: str, method: str) -> set[str]:
    return {str(k) for k in (operation(spec, path, method).get("responses") or {})}


def resolve_response_object(
    spec: dict[str, Any], path: str, method: str, status_code: int | str
) -> dict[str, Any] | None:
    responses = operation(spec, path, method).get("responses") or {}
    key = str(status_code)
    raw = responses.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict) and "$ref" in raw:
        resolved = _resolve_pointer(spec, raw["$ref"])
        if not isinstance(resolved, dict):
            raise TypeError(f"response $ref {raw['$ref']!r} is not an object")
        return resolved
    if not isinstance(raw, dict):
        raise TypeError(f"response for {method.upper()} {path} {key} is not an object")
    return raw


def response_json_schema(
    spec: dict[str, Any], path: str, method: str, status_code: int | str
) -> dict[str, Any] | None:
    resp = resolve_response_object(spec, path, method, status_code)
    if resp is None:
        return None
    content = resp.get("content") or {}
    media = content.get("application/json")
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    if schema is None:
        return None
    converted = openapi_to_json_schema(schema, spec)
    if not isinstance(converted, dict):
        raise TypeError(f"converted schema for {method.upper()} {path} {status_code} is not object")
    return converted


def format_schema_errors(errors: list[Any], *, limit: int = 8) -> str:
    lines: list[str] = []
    for err in errors[:limit]:
        loc = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "(root)"
        lines.append(f"  - at {loc}: {err.message}")
    if len(errors) > limit:
        lines.append(f"  ... and {len(errors) - limit} more")
    return "\n".join(lines)


def assert_status_in_openapi(
    spec: dict[str, Any],
    path: str,
    method: str,
    status_code: int,
    *,
    context: str = "",
) -> None:
    declared = declared_status_codes(spec, path, method)
    code = str(status_code)
    prefix = f"{context}: " if context else ""
    assert code in declared, (
        f"{prefix}{method.upper()} {path} returned HTTP {status_code}, "
        f"but OpenAPI only declares responses {sorted(declared)}"
    )


def assert_response_matches_schema(
    spec: dict[str, Any],
    path: str,
    method: str,
    response: httpx.Response,
    *,
    context: str = "",
    require_json_schema: bool = True,
) -> Any | None:
    """校验状态码在 OpenAPI 声明内；若有 application/json schema 则做 Draft202012 形状校验。"""
    assert_status_in_openapi(
        spec, path, method, response.status_code, context=context or f"{method.upper()} {path}"
    )
    schema = response_json_schema(spec, path, method, response.status_code)
    prefix = context or f"{method.upper()} {path} → {response.status_code}"
    if schema is None:
        if require_json_schema:
            raise AssertionError(
                f"{prefix}: OpenAPI has no application/json schema for this status, "
                f"but a JSON body schema was required; body={response.text[:500]!r}"
            )
        return None
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{prefix}: expected JSON body matching schema, got parse error {exc!r}; "
            f"text={response.text[:500]!r}"
        ) from exc

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{prefix}: response failed OpenAPI schema validation "
        f"({len(errors)} error(s)):\n{format_schema_errors(errors)}\n"
        f"body={body!r}"
    )
    return body


def assert_error_body(
    spec: dict[str, Any],
    path: str,
    method: str,
    response: httpx.Response,
    error_codes: set[str],
    *,
    context: str = "",
    expected_code: str | None = None,
    check_declared_status: bool | None = None,
) -> dict[str, Any]:
    """校验 Error schema + errors.md code；若该状态码在 path 上有声明则同时断言声明覆盖。

    check_declared_status:
      None  — 仅当 OpenAPI 声明了该 status 时才断言（401 等常未写入 responses）
      True  — 强制要求 status 在声明内
      False — 跳过声明检查，只做 Error schema
    """
    prefix = context or f"{method.upper()} {path} → {response.status_code}"
    declared = declared_status_codes(spec, path, method)
    status_key = str(response.status_code)
    if check_declared_status is True:
        assert_status_in_openapi(spec, path, method, response.status_code, context=prefix)
        body = assert_response_matches_schema(
            spec, path, method, response, context=prefix, require_json_schema=True
        )
    elif check_declared_status is False or status_key not in declared:
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{prefix}: expected JSON Error body, parse error {exc!r}; "
                f"text={response.text[:500]!r}"
            ) from exc
        schema = openapi_to_json_schema({"$ref": "#/components/schemas/Error"}, spec)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
        assert not errors, (
            f"{prefix}: Error schema validation failed ({len(errors)} error(s)):\n"
            f"{format_schema_errors(errors)}\nbody={body!r}"
        )
    else:
        body = assert_response_matches_schema(
            spec, path, method, response, context=prefix, require_json_schema=True
        )

    assert isinstance(body, dict), f"{prefix}: error body must be object, got {body!r}"
    err = body.get("error")
    assert isinstance(err, dict), f"{prefix}: missing error object in {body!r}"
    code = err.get("code")
    assert isinstance(code, str) and code, f"{prefix}: error.code missing in {body!r}"
    assert code in error_codes, (
        f"{prefix}: error.code={code!r} not in contracts/errors.md ({sorted(error_codes)})"
    )
    if expected_code is not None:
        assert code == expected_code, (
            f"{prefix}: expected error.code={expected_code!r}, got {code!r}; body={body!r}"
        )
    return body


def sql_exec(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def set_stock(sku_id: str, sellable_stock: int) -> None:
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                "UPDATE products_sku SET sellable_stock = :stock, version = 0 "
                "WHERE sku_id = :sku"
            ),
            {"stock": sellable_stock, "sku": sku_id},
        )
        assert result.rowcount == 1, f"set_stock: sku {sku_id!r} not found"


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


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("ECOMPILOT_JWT_SECRET", DEFAULT_JWT_SECRET)
    os.environ.setdefault("ECOMPILOT_JWT_SECRET", env["ECOMPILOT_JWT_SECRET"])
    pythonpath = str(ROOT)
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    return env


def _popen_uvicorn(module_app: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module_app,
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


@contextmanager
def pilot_live_stack_cm():
    """独立测试库 + Pilot / Ops 桩 / NLU 桩；清空 promo_rules；ECOMPILOT_DEV_TOKENS=1。"""
    with test_database(with_seed=True) as db_name:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM promo_rules"))

        pilot_port, ops_port, nlu_port = free_port(), free_port(), free_port()
        pilot_url = f"http://127.0.0.1:{pilot_port}"
        ops_url = f"http://127.0.0.1:{ops_port}"
        nlu_url = f"http://127.0.0.1:{nlu_port}"

        base_env = _base_env()

        env_pilot = base_env.copy()
        env_pilot["ECOMPILOT_DB_NAME"] = db_name
        env_pilot["ECOMPILOT_OPS_URL"] = ops_url
        env_pilot["ECOMPILOT_NLU_URL"] = nlu_url
        env_pilot["ECOMPILOT_INTERNAL_TOKEN"] = INTERNAL_TOKEN
        env_pilot["ECOMPILOT_DEV_TOKENS"] = "1"

        env_ops = base_env.copy()
        env_ops["PILOT_BASE_URL"] = pilot_url
        env_ops["PILOT_INTERNAL_TOKEN"] = INTERNAL_TOKEN

        env_nlu = base_env.copy()

        procs: list[subprocess.Popen] = []
        try:
            procs.append(_popen_uvicorn("stubs.nlu_stub:app", nlu_port, env_nlu))
            procs.append(_popen_uvicorn("stubs.ops_stub:app", ops_port, env_ops))
            procs.append(_popen_uvicorn("pilot.main:app", pilot_port, env_pilot))
            wait_ready(nlu_url)
            wait_ready(ops_url)
            wait_ready(pilot_url)
            yield PilotStack(
                db_name=db_name,
                pilot_base_url=pilot_url,
                ops_base_url=ops_url,
                nlu_base_url=nlu_url,
            )
        finally:
            stop_procs(procs)


@contextmanager
def stubs_live_stack_cm():
    """NLU / RAG / Ops 三桩进程（无 Pilot DB）。"""
    nlu_port, rag_port, ops_port = free_port(), free_port(), free_port()
    nlu_url = f"http://127.0.0.1:{nlu_port}"
    rag_url = f"http://127.0.0.1:{rag_port}"
    ops_url = f"http://127.0.0.1:{ops_port}"

    base_env = _base_env()
    env_ops = base_env.copy()
    env_ops["PILOT_BASE_URL"] = "http://127.0.0.1:9"
    env_ops["PILOT_INTERNAL_TOKEN"] = INTERNAL_TOKEN

    procs: list[subprocess.Popen] = []
    try:
        procs.append(_popen_uvicorn("stubs.nlu_stub:app", nlu_port, base_env.copy()))
        procs.append(_popen_uvicorn("stubs.rag_stub:app", rag_port, base_env.copy()))
        procs.append(_popen_uvicorn("stubs.ops_stub:app", ops_port, env_ops))
        wait_ready(nlu_url)
        wait_ready(rag_url)
        wait_ready(ops_url)
        yield StubsStack(nlu_base_url=nlu_url, rag_base_url=rag_url, ops_base_url=ops_url)
    finally:
        stop_procs(procs)
