"""Intent routing and durable, identity-scoped session history."""
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import text

from pilot.agents import a2a_client
from pilot.core.db import get_engine
from pilot.core.errors import ApiError

logger = logging.getLogger(__name__)


@contextmanager
def session_connection(session_id):
    # The schema's composite primary key alone cannot protect global session ownership.
    key = 'chat:' + hashlib.sha256(session_id.encode()).hexdigest()[:59]
    with get_engine().connect() as conn:
        locked = conn.execute(text('SELECT GET_LOCK(:key, 10)'), {'key': key}).scalar()
        conn.commit()
        if locked != 1:
            raise ApiError(503, 'CONCURRENT_VERSION_CONFLICT', '会话繁忙，请重试')
        try:
            with conn.begin():
                yield conn
        finally:
            conn.execute(text('SELECT RELEASE_LOCK(:key)'), {'key': key})
            conn.commit()


def append_messages(identity, session_id, messages):
    with session_connection(session_id) as conn:
        rows = conn.execute(text('SELECT tenant_id,user_id,messages_json FROM user_session_memory '
                                 'WHERE session_id=:session FOR UPDATE'), {'session': session_id}).mappings().all()
        if any(r['tenant_id'] != identity.tenant_id or r['user_id'] != identity.user_id for r in rows):
            logger.warning('Chat session ownership denied: tenant=%r user=%r session=%r',
                           identity.tenant_id, identity.user_id, session_id)
            raise ApiError(403, 'FORBIDDEN', '会话不属于当前用户')
        params = {'session': session_id, 'tenant': identity.tenant_id, 'user': identity.user_id}
        history = rows[0]['messages_json'] if rows else []
        if isinstance(history, str):
            history = json.loads(history)
        params['messages'] = json.dumps(history + messages, ensure_ascii=False)
        params['context'] = json.dumps({'cache_key': f'{identity.tenant_id}:{identity.user_id}:{session_id}'})
        if rows:
            conn.execute(text('UPDATE user_session_memory SET messages_json=:messages, updated_at=NOW() '
                              'WHERE tenant_id=:tenant AND user_id=:user AND session_id=:session'), params)
        else:
            conn.execute(text('INSERT INTO user_session_memory '
                              '(session_id,tenant_id,user_id,messages_json,context_json,updated_at) '
                              'VALUES (:session,:tenant,:user,:messages,:context,NOW())'), params)


def intent_for(body):
    if body.confirmed_order_id and body.confirmed_order_item_id:
        return 'aftersales'
    for intent, words in [('aftersales', '退 换 坏 漏 投诉 售后'),
                          ('seeding', '种草 笔记 清单'), ('shopping', '买 选 推荐 出差 住')]:
        if any(word in body.message for word in words.split()):
            return intent
    return 'chitchat'


def events(body, identity, token):
    intent = intent_for(body)
    trace = uuid4().hex

    def invoke(agent, port, payload):
        url = os.environ.get(f'ECOMPILOT_AGENT_{agent}_URL', f'http://127.0.0.1:{port}')
        return a2a_client.invoke(url, {**payload, 'auth_token': token}, trace_id=trace)

    if intent == 'aftersales':
        confirmed = bool(body.confirmed_order_id and body.confirmed_order_item_id)
        payload = {'action': 'confirm' if confirmed else 'candidates', 'text': body.message}
        if confirmed:
            payload.update(order_id=body.confirmed_order_id, order_item_id=body.confirmed_order_item_id,
                           buyer_reason_text=body.message)
        result = invoke('AFTERSALES', 8014, payload)
        if confirmed:
            yield 'card', {**result, 'type': 'claim_submitted'}
        else:
            candidates = result.get('candidates', [])
            yield 'token', {'text': '很抱歉给您带来不便，请确认具体订单商品。' if candidates else '当前账号下暂无可售后订单。'}
            yield 'card', {'type': 'candidate_orders', 'candidates': candidates}
    elif intent in ('shopping', 'seeding'):
        needs = [{'text': part.strip()} for part in re.split(r'以及|[，、,和+]', body.message) if part.strip()]
        if intent == 'seeding':
            city = next((c for c in ('杭州', '北京', '广州') if c in body.message), '其它')
            result = invoke('SEEDING', 8015, {'tenant_id': identity.tenant_id, 'city': city, 'needs': needs})
            yield 'card', {'type': 'seeding_note', 'note': result['note']}
        else:
            result = invoke('SELECTION', 8012, {'tenant_id': identity.tenant_id, 'needs': needs})
            picks = {}
            for candidate in result['candidates']:
                picks.setdefault(candidate['need_index'], candidate['sku_id'])
            if not picks:
                yield 'token', {'text': '暂无匹配商品，请换个需求试试。'}
            else:
                quantities = {}
                for sku in picks.values():
                    quantities[sku] = quantities.get(sku, 0) + 1
                quote = invoke('PRICING', 8013, {'items': [{'sku_id': sku, 'qty': qty}
                                                       for sku, qty in quantities.items()]})['quote']
                yield 'card', {'type': 'quote', 'quote': quote}
                if quote.get('suggested_addons'):
                    addons = quote['suggested_addons']
                    yield 'card', {'type': 'addon_suggestion', 'quote_id': quote['quote_id'],
                                   'addons': addons, 'saving_cents': max(a['saving_cents'] for a in addons)}
    else:
        yield 'token', {'text': '您好，我可以帮您选购商品、查询售后订单或生成种草笔记。'}
    yield 'done', {'intent': intent}
