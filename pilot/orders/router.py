from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from pilot.core.auth import Identity, current_identity, require_internal
from pilot.core.db import get_engine
from pilot.orders import quotes, service

router = APIRouter()


class ItemIn(BaseModel):
    sku_id: str
    qty: int = Field(ge=1, le=999)


class QuoteIn(BaseModel):
    items: list[ItemIn] = Field(min_length=1)
    coupon_ids: list[str] | None = None
    auto_coupon: bool = False


class AddonsIn(BaseModel):
    addon_sku_ids: list[str]


class OrderCreateIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    quote_id: str


@router.post("/api/v1/quotes", status_code=201)
def create_quote(body: QuoteIn, ident: Identity = Depends(current_identity)):
    # 未显式选券且未要求自动择优时不用券（避免未经同意消耗用户券）
    coupons = body.coupon_ids if body.coupon_ids is not None else (None if body.auto_coupon else [])
    return quotes.create_quote(get_engine(), ident, [i.model_dump() for i in body.items], coupons)


@router.post("/api/v1/quotes/{quote_id}/confirm-addons", status_code=201)
def confirm_addons(quote_id: str, body: AddonsIn, ident: Identity = Depends(current_identity)):
    return quotes.confirm_addons(get_engine(), ident, quote_id, body.addon_sku_ids)


@router.post("/api/v1/orders/create")
def create_order(body: OrderCreateIn, response: Response, ident: Identity = Depends(current_identity)):
    order, status = service.create_order(get_engine(), ident, body.idempotency_key, body.quote_id)
    response.status_code = status
    return order


@router.get("/api/v1/orders")
def list_orders(ident: Identity = Depends(current_identity)):
    return {"orders": service.list_orders(get_engine(), ident)}


@router.get("/api/v1/orders/{order_id}")
def get_order(order_id: str, ident: Identity = Depends(current_identity)):
    return service.get_order(get_engine(), ident, order_id)


@router.post("/api/v1/orders/{order_id}/pay")
def pay(order_id: str, ident: Identity = Depends(current_identity)):
    return service.pay_order(get_engine(), ident, order_id)


@router.post("/api/v1/orders/{order_id}/cancel")
def cancel(order_id: str, ident: Identity = Depends(current_identity)):
    return service.cancel_order(get_engine(), ident, order_id)


@router.post("/internal/v1/orders/{order_id}/fulfill", dependencies=[Depends(require_internal)])
def fulfill(order_id: str):
    return service.fulfill_order(get_engine(), order_id)
