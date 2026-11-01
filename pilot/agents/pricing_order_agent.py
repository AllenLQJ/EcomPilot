"""Pricing agent: delegate authenticated pricing to the Pilot API."""
import os
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from pilot.core.errors import install_error_handlers

from pilot.agents.base import AgentError, make_agent_app


class Item(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sku_id: str = Field(min_length=1)
    qty: StrictInt = Field(ge=1)


class PricingInput(BaseModel):
    auth_token: str = Field(min_length=1)
    items: list[Item] | None = Field(default=None, min_length=1)
    coupon_ids: list[str] = Field(default_factory=list)
    quote_id: str | None = Field(default=None, min_length=1)
    addon_sku_ids: list[str] | None = None


def handle(data: dict, trace_id: str) -> dict:
    try:
        request = PricingInput.model_validate(data)
        if request.quote_id is not None:
            if request.addon_sku_ids is None:
                raise ValueError("addon_sku_ids required")
            path = f"/api/v1/quotes/{quote(request.quote_id, safe='')}/confirm-addons"
            payload = {"addon_sku_ids": request.addon_sku_ids}
        else:
            if request.items is None or request.addon_sku_ids is not None:
                raise ValueError("items required")
            path = "/api/v1/quotes"
            payload = {"items": [item.model_dump() for item in request.items],
                       "coupon_ids": request.coupon_ids}
    except (ValidationError, ValueError) as exc:
        raise HTTPException(400, detail="INVALID_REQUEST") from exc
    try:
        response = httpx.post(os.environ.get("ECOMPILOT_PILOT_URL", "http://127.0.0.1:8085").rstrip("/") + path,
                              json=payload, headers={"Authorization": f"Bearer {request.auth_token}"}, timeout=3.0)
    except httpx.TimeoutException as exc:
        raise AgentError("UPSTREAM_TIMEOUT") from exc
    except httpx.HTTPError as exc:
        raise AgentError("UPSTREAM_ERROR") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise AgentError("UPSTREAM_ERROR") from exc
    if not 200 <= response.status_code < 300:
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        raise AgentError(error.get("code", "UPSTREAM_ERROR"), error.get("message", ""))
    if not isinstance(body, dict):
        raise AgentError("UPSTREAM_ERROR")
    return {"quote": body}


app = make_agent_app("pricing_order", 8013, ["pricing_order"], handle)

install_error_handlers(app)
