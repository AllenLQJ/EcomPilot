import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pilot.core import hardening, observability
from pilot.core.config import Settings

GOOD = dict(jwt_secret="x" * 40, internal_token="t" * 24, db_password="not-the-default")


def test_dev_mode_never_blocks():
    hardening.enforce(Settings(env="dev"), environ={})


def test_prod_with_defaults_is_refused_without_echoing_values():
    with pytest.raises(RuntimeError) as info:
        hardening.enforce(Settings(env="prod"), environ={})
    msg = str(info.value)
    assert "jwt_secret" in msg and "internal_token" in msg and "db_password" in msg
    assert "dev-secret-change-me" not in msg and "ecompilot_root" not in msg


def test_prod_with_strong_secrets_starts():
    hardening.enforce(Settings(env="prod", **GOOD), environ={})


def test_prod_refuses_short_secrets_and_dev_tokens():
    problems = hardening.production_problems(Settings(env="prod", jwt_secret="short", internal_token="short", db_password="p"),
                                             environ={"ECOMPILOT_DEV_TOKENS": "1"})
    assert any("jwt_secret 少于" in p for p in problems) and any("internal_token 少于" in p for p in problems)
    assert any("DEV_TOKENS" in p for p in problems)


def _app():
    app = FastAPI()
    observability.install(app)

    @app.get("/orders/{order_id}")
    def order(order_id: str):
        return {"id": order_id}
    return app


def test_request_id_echoed_generated_and_untrusted_values_replaced(caplog):
    client = TestClient(_app())
    assert client.get("/orders/A", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    generated = client.get("/orders/A").headers["x-request-id"]
    assert len(generated) == 32
    bad = client.get("/orders/A", headers={"X-Request-ID": "x y\t<script>"}).headers["x-request-id"]
    assert bad != "x y\t<script>" and len(bad) == 32


def test_access_log_is_json_with_route_template_not_raw_path(caplog):
    logger_ = logging.getLogger("ecompilot.access"); logger_.propagate = True; caplog.set_level(logging.INFO, logger="ecompilot.access")
    TestClient(_app()).get("/orders/SECRET_ORDER_42", headers={"X-Request-ID": "rid-1"})
    record = json.loads(caplog.records[-1].message)
    assert record["request_id"] == "rid-1" and record["route"] == "/orders/{order_id}" and record["status"] == 200
    assert "SECRET_ORDER_42" not in caplog.records[-1].message


def test_metrics_endpoint_uses_route_templates():
    client = TestClient(_app())
    for oid in ("A", "B", "C"):
        client.get(f"/orders/{oid}")
    client.get("/nope")
    text = client.get("/metrics").text
    assert 'ecompilot_http_requests_total{method="GET",route="/orders/{order_id}",status="200"}' in text
    assert 'route="unmatched",status="404"' in text
    assert "/orders/A" not in text            # 不能因为 id 造成指标基数爆炸
