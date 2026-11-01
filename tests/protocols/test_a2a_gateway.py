from contextlib import contextmanager

import httpx
import pytest
from a2a.client import create_client
from a2a.helpers import new_data_message, new_text_message
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import MessageToDict

import protocols.a2a_gateway as gw
from tests.protocols.conftest import Server


@contextmanager
def gateway(agent_base):
    srv = Server()
    srv.app = gw.build_app({"selection": (agent_base, "选品", ["selection"])}, public_url=srv.url)
    with srv:
        yield srv


async def send(url, message):
    client = await create_client(url)
    try:
        events = [e async for e in client.send_message(pb.SendMessageRequest(message=message))]
    finally:
        await client.close()
    for e in events:
        if e.HasField("task"):
            return e.task
    raise AssertionError(f"no task in {events!r}")


async def test_agent_card_is_standard(downstream):
    with gateway(downstream.url) as srv:
        card = httpx.get(srv.url + "/selection/.well-known/agent-card.json").json()
        assert card["name"] == "selection" and card["skills"][0]["id"] == "selection"
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert card["supportedInterfaces"][0]["url"] == srv.url + "/selection/"
        assert httpx.get(srv.url + "/health").json() == {"status": "ok"}
        assert "selection" in httpx.get(srv.url + "/").json()["agents"]


async def test_official_client_message_send_completes_with_output_artifact(downstream):
    with gateway(downstream.url) as srv:
        task = await send(srv.url + "/selection", new_data_message(role=pb.ROLE_USER, data={"tenant_id": "T1", "needs": [{"text": "热水器"}]}))
    assert task.status.state == pb.TASK_STATE_COMPLETED
    data = MessageToDict(task.artifacts[0].parts[0].data)
    assert data["echo"] == {"tenant_id": "T1", "needs": [{"text": "热水器"}]} and data["trace_id"] == task.id
    assert downstream.seen[0]["trace_id"] == task.id


async def test_integers_survive_both_directions(downstream):
    """协议里的数字是 double：入站还原成 int 再交给代理；出站另附原样 JSON 文本，金额不会变成 112000.0。"""
    import json
    with gateway(downstream.url) as srv:
        task = await send(srv.url + "/selection", new_data_message(
            {"qty": 2, "unit_price_cents": 112000, "ratio": 0.98, "nested": [{"n": 3}]}, role=pb.ROLE_USER))
    forwarded = downstream.seen[0]["input"]
    assert forwarded == {"qty": 2, "unit_price_cents": 112000, "ratio": 0.98, "nested": [{"n": 3}]}
    assert type(forwarded["qty"]) is int and type(forwarded["unit_price_cents"]) is int and type(forwarded["nested"][0]["n"]) is int
    exact = json.loads(task.artifacts[0].parts[1].text)
    assert exact["echo"]["unit_price_cents"] == 112000 and type(exact["echo"]["unit_price_cents"]) is int


@pytest.mark.parametrize("payload,expect", [({"mode": "error"}, "INVALID_INPUT"), ({"mode": "boom"}, "UPSTREAM_ERROR")])
async def test_downstream_failures_become_failed_tasks(downstream, payload, expect):
    with gateway(downstream.url) as srv:
        task = await send(srv.url + "/selection", new_data_message(payload, role=pb.ROLE_USER))
    assert task.status.state == pb.TASK_STATE_FAILED
    assert expect in task.status.message.parts[0].text


async def test_downstream_unreachable_is_failed_not_crash():
    with gateway("http://127.0.0.1:9") as srv:
        task = await send(srv.url + "/selection", new_data_message(role=pb.ROLE_USER, data={"x": 1}))
    assert task.status.state == pb.TASK_STATE_FAILED and "UPSTREAM_ERROR" in task.status.message.parts[0].text


async def test_message_without_data_part_is_rejected(downstream):
    with gateway(downstream.url) as srv:
        task = await send(srv.url + "/selection", new_text_message("帮我选个热水器", role=pb.ROLE_USER))
    assert task.status.state == pb.TASK_STATE_FAILED and "INVALID_REQUEST" in task.status.message.parts[0].text
    assert downstream.seen == []
