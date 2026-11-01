"""Atomic refund execution, serialized by the order row lock."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from pilot.core.auth import require_internal
from pilot.core.db import get_engine
from pilot.core.errors import ApiError


class RefundExecuteIn(BaseModel):
    refund_idempotency_key: str = Field(min_length=1, max_length=128)
    claim_no: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    order_item_id: str | None = Field(default=None, max_length=64)
    approved_refund_cents: Annotated[int, Field(strict=True, ge=1)]
    restock_policy: Literal['SELLABLE_WAREHOUSE', 'DEFECTIVE_WAREHOUSE', 'NO_RESTOCK']
    approval_mode: str | None = Field(default=None, max_length=32)
    supervisor_user_id: str | None = Field(default=None, max_length=64)
    approved_at: str | None = None


class RefundStatus(BaseModel):
    refund_idempotency_key: str
    refund_execution_id: str | None = None
    order_id: str | None = None
    execution_status: Literal['SUCCESS', 'FAILED', 'NOT_FOUND']
    order_status_after: str | None = None
    refunded_cents: int = 0
    idempotent_replayed: bool = False
    executed_at: datetime | None = None


router = APIRouter(dependencies=[Depends(require_internal)])


def _lookup(conn, key):
    return conn.execute(text('SELECT * FROM refund_executions WHERE refund_idempotency_key=:key'),
                        {'key': key}).mappings().first()


def _status(row, replayed=False):
    return RefundStatus(**{k: row[k] for k in (
        'refund_idempotency_key', 'refund_execution_id', 'order_id', 'execution_status',
        'order_status_after', 'refunded_cents', 'executed_at')}, idempotent_replayed=replayed)


@router.get('/internal/v1/refunds/status', response_model=RefundStatus)
def refund_status(refund_idempotency_key: str):
    with get_engine().connect() as conn:
        row = _lookup(conn, refund_idempotency_key)
        return _status(row) if row else RefundStatus(
            refund_idempotency_key=refund_idempotency_key, execution_status='NOT_FOUND')


@router.post('/internal/v1/refunds/execute', response_model=RefundStatus)
def execute_refund(body: RefundExecuteIn):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            order = conn.execute(text('SELECT * FROM orders WHERE order_id=:id FOR UPDATE'),
                                 {'id': body.order_id}).mappings().first()
            if order is None:
                raise ApiError(404, 'NOT_FOUND', 'order not found')
            previous = _lookup(conn, body.refund_idempotency_key)
            if previous:
                return _status(previous, True)
            if order['order_status'] not in {'FULFILLED', 'AFTER_SALE_OPEN', 'REFUNDING', 'REFUNDED_PARTIAL'}:
                raise ApiError(409, 'INVALID_ORDER_STATE')
            items = conn.execute(text('SELECT * FROM order_items WHERE order_id=:id ORDER BY order_item_id FOR UPDATE'),
                                 {'id': body.order_id}).mappings().all()
            if body.order_item_id is not None:
                items = [i for i in items if i['order_item_id'] == body.order_item_id]
                if not items:
                    raise ApiError(404, 'NOT_FOUND', 'order item not found')
                limit = items[0]['payable_cents'] - items[0]['refunded_cents']
            else:
                limit = order['total_payable_cents'] - order['refunded_cents']
            amount = body.approved_refund_cents
            if amount > min(limit, order['total_payable_cents'] - order['refunded_cents']):
                raise ApiError(422, 'REFUND_EXCEEDS_LIMIT')
            total = order['refunded_cents'] + amount
            after = 'REFUNDED_FULL' if total == order['total_payable_cents'] else 'REFUNDED_PARTIAL'
            record = dict(refund_execution_id='RF_EXEC_' + uuid4().hex[:12],
                          refund_idempotency_key=body.refund_idempotency_key,
                          claim_no=body.claim_no, order_id=body.order_id, order_item_id=body.order_item_id,
                          refunded_cents=amount, restock_policy=body.restock_policy,
                          execution_status='SUCCESS', order_status_after=after,
                          approval_mode=body.approval_mode, supervisor_user_id=body.supervisor_user_id,
                          executed_at=datetime.now().replace(microsecond=0))
            columns = ', '.join(record)
            values = ', '.join(':' + k for k in record)
            conn.execute(text(f'INSERT INTO refund_executions ({columns}) VALUES ({values})'), record)
            remaining = amount
            # Stable allocation keeps per-item totals consistent for whole-order partial refunds.
            for item in items:
                delta = min(remaining, item['payable_cents'] - item['refunded_cents'])
                remaining -= delta
                if delta <= 0:
                    continue
                conn.execute(text('UPDATE order_items SET refunded_cents=refunded_cents+:delta WHERE order_item_id=:id'),
                             {'delta': delta, 'id': item['order_item_id']})
                if item['refunded_cents'] + delta == item['payable_cents']:
                    stock = {'DEFECTIVE_WAREHOUSE': 'defective_stock',
                             'SELLABLE_WAREHOUSE': 'sellable_stock'}.get(body.restock_policy)
                    if stock:
                        version = ', version=version+1' if stock == 'sellable_stock' else ''
                        conn.execute(text(f'UPDATE products_sku SET {stock}={stock}+:qty{version} WHERE sku_id=:sku'),
                                     {'qty': item['qty'], 'sku': item['sku_id']})
            if remaining:
                raise ApiError(422, 'REFUND_EXCEEDS_LIMIT', 'insufficient refundable item balance')
            conn.execute(text('UPDATE orders SET refunded_cents=:total, order_status=:status WHERE order_id=:id'),
                         {'total': total, 'status': after, 'id': body.order_id})
            conn.execute(text("UPDATE aftersale_requests SET claim_status='COMPLETED' WHERE claim_no=:claim"),
                         {'claim': body.claim_no})
            return _status(record)
    except IntegrityError:
        # The context manager has rolled back all mutations before a fresh read.
        with engine.connect() as conn:
            previous = _lookup(conn, body.refund_idempotency_key)
            if previous:
                return _status(previous, True)
        raise
