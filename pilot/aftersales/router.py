"""JWT-scoped, read-only candidates and explicit aftersale submission."""
from datetime import datetime
from hashlib import sha1

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import text

from pilot.core.auth import Identity, current_identity
from pilot.core.config import get_settings
from pilot.core.db import get_engine
from pilot.core.errors import ApiError

router = APIRouter(prefix="/api/v1/aftersales")


class CandidatesIn(BaseModel):
    text: str | None = None
    category_hint: str | None = None


class ConfirmIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    order_item_id: str = Field(min_length=1, max_length=64)
    buyer_reason_text: str = ""
    evidence_urls: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    predicted_label: str = "ABSTAIN"
    category_confidence: float = 0
    catalog_category_hint: str | None = None
    sentiment: str
    sentiment_score: float
    priority_boost: bool


def _analyze(reason: str | None) -> dict | None:
    if not reason:
        return None
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.post(get_settings().nlu_url.rstrip("/") + "/api/v1/nlu/analyze",
                                   json={"text": reason, "scene_type": "aftersales"})
            response.raise_for_status()
            data = response.json()
            from pilot.catalog.labels import resolve_label
            clause = next((dict(c) for c in data["clauses"]
                           if c["category_confidence"] >= 0.65 and c["predicted_label"] != "ABSTAIN"), {})
            if clause and not clause.get("catalog_category_hint"):
                # NLU 自带的品类提示优先；它没给才用 Pilot 的映射表补（读表失败只是少一个来源）
                try:
                    mapped = resolve_label(get_engine(), clause["predicted_label"])
                except Exception:
                    mapped = None
                if mapped is not None:
                    clause["catalog_category_hint"] = mapped["catalog_category_id"]
            return Signal(**{k: clause[k] for k in (
                "predicted_label", "category_confidence", "catalog_category_hint") if k in clause},
                **{k: data[k] for k in ("sentiment", "sentiment_score", "priority_boost")}).model_dump()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ValidationError):
        return None


@router.post("/candidates")
def candidates(body: CandidatesIn = CandidatesIn(), identity: Identity = Depends(current_identity)):
    signal = _analyze(body.text)
    hint = body.category_hint or (signal or {}).get("catalog_category_hint")
    with get_engine().connect() as conn:
        orders = [dict(row) for row in conn.execute(text("""
            SELECT order_id, order_status, total_payable_cents, created_at FROM orders
            WHERE tenant_id=:tenant AND user_id=:user
              AND order_status IN ('PAID_SIMULATED','FULFILLED')
            ORDER BY created_at DESC, order_id
        """), {"tenant": identity.tenant_id, "user": identity.user_id}).mappings()]
        for order in orders:
            order["items"] = [dict(row) for row in conn.execute(text(
                "SELECT * FROM order_items WHERE order_id=:id ORDER BY order_item_id"),
                {"id": order["order_id"]}).mappings()]
            order["category_match"] = bool(hint and any(
                item["catalog_category_id"] == hint for item in order["items"]))
    orders.sort(key=lambda order: not order["category_match"])
    return {"candidates": orders, "nlu_signal": signal}


@router.post("/confirm", status_code=202)
def confirm(body: ConfirmIn, identity: Identity = Depends(current_identity)):
    engine = get_engine()
    claim_no = "CLM_" + sha1(f"{body.order_id}:{body.order_item_id}".encode()).hexdigest()[:12].upper()
    params = {"id": body.order_id, "item": body.order_item_id, "tenant": identity.tenant_id,
              "user": identity.user_id, "claim": claim_no, "reason": body.buyer_reason_text}
    with engine.begin() as conn:
        order = conn.execute(text("""SELECT * FROM orders WHERE order_id=:id
            AND tenant_id=:tenant AND user_id=:user FOR UPDATE"""), params).mappings().first()
        if order is None:
            raise ApiError(404, "NOT_FOUND", "order not found")
        item = conn.execute(text("SELECT * FROM order_items WHERE order_id=:id AND order_item_id=:item FOR UPDATE"),
                            params).mappings().first()
        if item is None:
            raise ApiError(404, "NOT_FOUND", "order item not found")
        if order["order_status"] != "FULFILLED":
            message = ("未发货订单请走取消退款" if order["order_status"] == "PAID_SIMULATED"
                       else "order is not fulfilled")
            raise ApiError(409, "INVALID_ORDER_STATE", message)
        if conn.execute(text("SELECT claim_no FROM aftersale_requests WHERE order_id=:id AND order_item_id=:item"), params).first():
            raise ApiError(409, "INVALID_ORDER_STATE", "aftersale already requested")
        if conn.execute(text("UPDATE orders SET order_status='AFTER_SALE_OPEN' WHERE order_id=:id AND order_status='FULFILLED'"), params).rowcount == 0:
            raise ApiError(409, "INVALID_ORDER_STATE", "order state changed")
        conn.execute(text("""INSERT INTO aftersale_requests
            (claim_no, tenant_id, user_id, order_id, order_item_id, claim_status, buyer_reason_text, created_at)
            VALUES (:claim, :tenant, :user, :id, :item, 'SUBMITTED', :reason, :now)"""),
                     {**params, "now": datetime.now()})
    signal = _analyze(body.buyer_reason_text) or Signal(
        sentiment="NEUTRAL", sentiment_score=0, priority_boost=False).model_dump()
    signal.pop("catalog_category_hint", None)
    payload = {
        "claim_no": claim_no, "tenant_id": identity.tenant_id, "user_id": identity.user_id,
        "order_id": body.order_id, "order_item_id": body.order_item_id,
        "sku_id": item["sku_id"], "catalog_category_id": item["catalog_category_id"],
        "item_payable_cents": item["payable_cents"],
        "max_refundable_cents": item["payable_cents"] - item["refunded_cents"],
        "days_since_delivery": max(0, (datetime.now() - order["fulfilled_at"]).days) if order["fulfilled_at"] else 0,
        "claim_type": "QUALITY_REFUND", "buyer_reason_text": body.buyer_reason_text,
        "evidence_urls": body.evidence_urls, "nlu_signal": signal,
    }
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.post(get_settings().ops_url.rstrip("/") + "/internal/v1/claims/submit",
                                   json=payload, headers={"X-Internal-Token": get_settings().internal_token})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        with engine.begin() as conn:
            # A synchronous refund callback may already have advanced the order.
            changed = conn.execute(text("UPDATE orders SET order_status='FULFILLED' WHERE order_id=:id AND order_status='AFTER_SALE_OPEN'"), params).rowcount
            if changed:
                conn.execute(text("DELETE FROM aftersale_requests WHERE claim_no=:claim AND claim_status='SUBMITTED'"), params)
        if isinstance(exc, httpx.TimeoutException):
            raise ApiError(504, "UPSTREAM_TIMEOUT", "Ops submission timed out") from exc
        raise ApiError(502, "UPSTREAM_ERROR", "Ops submission failed") from exc
    return {"claim_no": claim_no, "claim_status": "SUBMITTED", "order_status": "AFTER_SALE_OPEN"}


class EvidenceIn(BaseModel):
    evidence_urls: list[str] = Field(min_length=1, max_length=10)

    @field_validator("evidence_urls")
    @classmethod
    def _urls(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if not (url.startswith(("http://", "https://")) and len(url) <= 1024):
                raise ValueError("evidence url must be http(s) and at most 1024 chars")
        return urls


def _own_claim(conn, claim_no: str, identity: Identity):
    row = conn.execute(text("""SELECT a.claim_no, a.order_id, a.order_item_id, a.claim_status, o.order_status
        FROM aftersale_requests a JOIN orders o ON o.order_id=a.order_id
        WHERE a.claim_no=:claim AND a.tenant_id=:tenant AND a.user_id=:user"""),
        {"claim": claim_no, "tenant": identity.tenant_id, "user": identity.user_id}).mappings().first()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "aftersale not found")   # 他人的申请与不存在一视同仁，防枚举
    return dict(row)


def _ops_call(method: str, path: str, **kwargs):
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.request(method, get_settings().ops_url.rstrip("/") + path,
                                      headers={"X-Internal-Token": get_settings().internal_token}, **kwargs)
    except httpx.TimeoutException as exc:
        raise ApiError(504, "UPSTREAM_TIMEOUT", "Ops request timed out") from exc
    except httpx.HTTPError as exc:
        raise ApiError(502, "UPSTREAM_ERROR", "Ops request failed") from exc
    if response.status_code == 404:
        raise ApiError(404, "NOT_FOUND", "aftersale not found")
    if response.status_code == 409:
        raise ApiError(409, "INVALID_ORDER_STATE", "aftersale is not waiting for more evidence")
    if response.status_code >= 400:
        raise ApiError(502, "UPSTREAM_ERROR", "Ops request failed")
    return response.json()


@router.get("/{claim_no}")
def claim_status(claim_no: str, identity: Identity = Depends(current_identity)):
    with get_engine().connect() as conn:
        mine = _own_claim(conn, claim_no, identity)
    try:
        ops = _ops_call("GET", f"/internal/v1/claims/{claim_no}/status")
    except ApiError:
        ops = None      # Ops 暂不可用时只给本地视图，不让买家看到 5xx
    return {"claim_no": claim_no, "order_id": mine["order_id"], "order_status": mine["order_status"],
            "claim_status": ops["claim_status"] if ops else mine["claim_status"],
            "needs_evidence": bool(ops and ops.get("needs_evidence")), "ops_available": ops is not None}


@router.post("/{claim_no}/evidence", status_code=202)
def add_evidence(claim_no: str, body: EvidenceIn, identity: Identity = Depends(current_identity)):
    with get_engine().connect() as conn:
        _own_claim(conn, claim_no, identity)
    result = _ops_call("POST", f"/internal/v1/claims/{claim_no}/evidence", json={"evidence_urls": body.evidence_urls})
    return {"claim_no": claim_no, "claim_status": result.get("status", "SUBMITTED")}
