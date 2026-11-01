import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pilot.chat import service
from pilot.chat.router import router
from pilot.agents.a2a_client import A2AError
from pilot.core.auth import issue_token
from pilot.core.db import get_engine
from pilot.core.errors import install_error_handlers


@pytest.fixture
def setup(db_name, monkeypatch):
    calls = []
    state = {'error': None, 'candidates': [{'order_id': 'order', 'order_status': 'FULFILLED',
                                         'items': [{'order_item_id': 'item'}]}]}
    def invoke(url, input, trace_id):
        calls.append((url, input, trace_id))
        if state['error']:
            raise A2AError(state['error'], 'injected')
        if 'action' in input:
            return {'claim_no': 'claim', 'claim_status': 'SUBMITTED'} if input['action'] == 'confirm' else {'candidates': state['candidates']}
        if 'needs' in input:
            if 'city' in input:
                return {'note': {'title': '笔记', 'body': '正文', 'images': []}}
            return {'candidates': [{'need_index': 0, 'sku_id': 'sku'}, {'need_index': 0, 'sku_id': 'alternative'}]}
        return {'quote': {'quote_id': 'quote', 'items': input['items'], 'suggested_addons': [
            {'sku_id': 'addon', 'sku_name': '凑单', 'unit_price_cents': 100, 'saving_cents': 50}]}}
    monkeypatch.setattr(service.a2a_client, 'invoke', invoke)
    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + issue_token('buyer', 'tenant')
        yield client, calls, state


def send(client, **body):
    return client.post('/api/v1/chat/stream', json={'session_id': 'session', 'message': '坏了要退', **body})


def events(response):
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    return [(lines[0][7:], json.loads(lines[1][6:])) for frame in response.text.strip().split('\n\n')
            if (lines := frame.splitlines())]


def test_candidates_never_confirm_and_history(setup):
    client, calls, _ = setup
    result = events(send(client))
    assert [e for e, _ in result] == ['token', 'card', 'done']
    assert result[1][1]['type'] == 'candidate_orders'
    assert [c[1]['action'] for c in calls] == ['candidates']
    assert calls[0][1]['auth_token'] == client.headers['Authorization'][7:]
    events(send(client, confirmed_order_id='order'))
    assert [c[1]['action'] for c in calls] == ['candidates', 'candidates']
    with get_engine().connect() as conn:
        row = conn.execute(text('SELECT * FROM user_session_memory')).mappings().one()
    history = json.loads(row['messages_json']) if isinstance(row['messages_json'], str) else row['messages_json']
    assert len([m for m in history if m['role'] == 'user']) == 2
    assert row['user_id'] == 'buyer'


def test_explicit_confirm(setup):
    client, calls, _ = setup
    result = events(send(client, message='确认此订单', confirmed_order_id='order', confirmed_order_item_id='item'))
    assert result[0][1]['type'] == 'claim_submitted'
    assert calls[0][1]['action'] == 'confirm'
    assert calls[0][1]['order_id'] == 'order'
    assert calls[0][1]['order_item_id'] == 'item'
    assert result[-1] == ('done', {'intent': 'aftersales'})


@pytest.mark.parametrize('user,tenant', [('other', 'tenant'), ('buyer', 'other')])
def test_session_forbidden_before_stream(setup, user, tenant):
    client, calls, _ = setup
    send(client)
    calls.clear()
    client.headers['Authorization'] = 'Bearer ' + issue_token(user, tenant)
    result = send(client)
    assert result.status_code == 403
    assert result.json()['error']['code'] == 'FORBIDDEN'
    assert result.headers['content-type'].startswith('application/json')
    assert not calls


def test_missing_token(setup):
    client, calls, _ = setup
    client.headers.pop('Authorization')
    assert send(client).json()['error']['code'] == 'UNAUTHORIZED'
    assert send(client).status_code == 401
    assert not calls


def test_shopping(setup, monkeypatch):
    client, calls, _ = setup
    monkeypatch.setenv('ECOMPILOT_AGENT_SELECTION_URL', 'http://selection.test')
    result = events(send(client, message='买牛奶和洗发水'))
    assert [e for e, _ in result] == ['card', 'card', 'done']
    assert [d['type'] for e, d in result if e == 'card'] == ['quote', 'addon_suggestion']
    assert result[1][1]['saving_cents'] == 50
    assert calls[0][0] == 'http://selection.test'
    assert len(calls[0][1]['needs']) == 2
    assert calls[1][1]['items'] == [{'sku_id': 'sku', 'qty': 1}]
    assert len(calls) == 2


@pytest.mark.parametrize('code', ['UPSTREAM_ERROR', 'UPSTREAM_TIMEOUT'])
def test_downstream_error_ends_stream(setup, code):
    client, _, state = setup
    state['error'] = code
    assert events(send(client)) == [('error', {'code': code, 'message': 'injected'})]


def test_forged_identity_ignored(setup):
    client, calls, _ = setup
    events(send(client, message='买牛奶', user_id='forged', tenant_id='forged'))
    assert calls[0][1]['tenant_id'] == 'tenant'
    assert 'user_id' not in calls[0][1]
    with get_engine().connect() as conn:
        row = conn.execute(text('SELECT tenant_id,user_id FROM user_session_memory')).one()
    assert tuple(row) == ('tenant', 'buyer')


def test_empty_candidates_and_seeding_and_chitchat(setup):
    client, calls, state = setup
    state['candidates'] = []
    result = events(send(client))
    assert result[1][1]['candidates'] == []
    assert '暂无' in result[0][1]['text']
    result = events(send(client, message='推荐杭州种草清单'))
    assert result[0][1]['type'] == 'seeding_note'
    assert result[-1][1]['intent'] == 'seeding'
    calls.clear()
    assert events(send(client, message='你好'))[-1][1]['intent'] == 'chitchat'
    assert not calls
