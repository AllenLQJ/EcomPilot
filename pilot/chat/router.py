"""Chat HTTP boundary; ownership failures happen before streaming starts."""
import json
import logging

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pilot.agents.a2a_client import A2AError
from pilot.core.auth import Identity, current_identity
from pilot.core.errors import ApiError
from pilot.chat import service

router = APIRouter()
logger = logging.getLogger(__name__)
ERROR_CODES = {
    'INVALID_REQUEST', 'UNAUTHORIZED', 'INTERNAL_AUTH_FAILED', 'FORBIDDEN', 'NOT_FOUND',
    'QUOTE_EXPIRED_OR_CHANGED', 'COUPON_UNAVAILABLE', 'INVALID_ORDER_STATE',
    'IDEMPOTENCY_KEY_CONFLICT', 'INSUFFICIENT_STOCK', 'REFUND_EXCEEDS_LIMIT',
    'INVALID_ADDON_SELECTION', 'CONCURRENT_VERSION_CONFLICT', 'UPSTREAM_ERROR', 'UPSTREAM_TIMEOUT',
}


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r'\S')
    message: str = Field(min_length=1, pattern=r'\S')
    confirmed_order_id: str | None = Field(default=None, min_length=1)
    confirmed_order_item_id: str | None = Field(default=None, min_length=1)


@router.post('/api/v1/chat/stream')
def chat(body: ChatRequest, identity: Identity = Depends(current_identity),
         authorization: str = Header(default='')):
    service.append_messages(identity, body.session_id, [{'role': 'user', 'content': body.message}])

    def stream():
        try:
            for event, data in service.events(body, identity, authorization[7:].strip()):
                service.append_messages(identity, body.session_id, [{'role': 'assistant', 'event': event, 'content': data}])
                yield encode(event, data)
        except (A2AError, ApiError) as exc:
            # A2A business errors use the same frozen error vocabulary.
            code = exc.code if exc.code in ERROR_CODES else 'UPSTREAM_ERROR'
            yield encode('error', {'code': code, 'message': exc.message})
        except Exception:
            logger.exception('Chat stream failed')
            yield encode('error', {'code': 'UPSTREAM_ERROR', 'message': '服务暂时不可用，请重试'})

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def encode(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
