"""Read candidates or submit an explicitly confirmed order item via Pilot."""
import os
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from pilot.core.errors import install_error_handlers

from pilot.agents.base import AgentError, make_agent_app


class AftersalesInput(BaseModel):
    auth_token: str = Field(min_length=1)
    action: Literal["candidates", "confirm"]
    text: str | None = None
    category_hint: str | None = None
    order_id: str | None = None
    order_item_id: str | None = None
    buyer_reason_text: str | None = None
    evidence_urls: list[str] | None = None


def handle(data: dict, trace_id: str) -> dict:
    try:
        request = AftersalesInput.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(400, detail="INVALID_REQUEST") from exc
    if request.action == "confirm":
        if not request.order_id or not request.order_id.strip() or not request.order_item_id or not request.order_item_id.strip():
            raise AgentError("INVALID_REQUEST", "order_id and order_item_id are required")
        fields = {"order_id", "order_item_id", "buyer_reason_text", "evidence_urls"}
    else:
        fields = {"text", "category_hint"}
    payload = request.model_dump(include=fields, exclude_none=True)
    try:
        response = httpx.post(
            os.environ.get("ECOMPILOT_PILOT_URL", "http://127.0.0.1:8085").rstrip("/")
            + f"/api/v1/aftersales/{request.action}", json=payload,
            headers={"Authorization": f"Bearer {request.auth_token}"}, timeout=3.0)
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
    return body


app = make_agent_app("aftersales", 8014, ["candidates", "confirm"], handle)

install_error_handlers(app)
