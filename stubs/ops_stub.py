"""In-memory Ops sandbox; run with uvicorn stubs.ops_stub:app --port 8000."""
from copy import deepcopy
from datetime import datetime
import os
from threading import Lock
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

app = FastAPI()
claims: dict[str, dict] = {}
pending: dict[str, dict] = {}
state_lock = Lock()
fault_mode = "none"


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    claim_no: str
    tenant_id: str
    user_id: str
    order_id: str
    order_item_id: str
    sku_id: str
    max_refundable_cents: StrictInt


class FaultIn(BaseModel):
    mode: Literal["none", "refund_callback_timeout", "duplicate_callback", "submit_fail"]


class ApproveIn(BaseModel):
    claim_no: str
    approved_refund_cents: StrictInt | None = Field(default=None, ge=1)


class ReconcileIn(BaseModel):
    refund_idempotency_key: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/internal/v1/claims/submit", status_code=202)
def submit(body: ClaimIn):
    with state_lock:
        if fault_mode == "submit_fail":
            raise HTTPException(503, "claim submission failed (injected)")
        claims.setdefault(body.claim_no, body.model_dump())
    return {"claim_no": body.claim_no, "status": "SUBMITTED"}


@app.get("/internal/v1/claims/{claim_no}/status")
def claim_status(claim_no: str):
    with state_lock:
        claim = claims.get(claim_no)
    if claim is None:
        raise HTTPException(404, "claim not found")
    status = claim.get("claim_status", "SUBMITTED")
    return {"claim_no": claim_no, "claim_status": status, "audit_round": 1,
            "needs_evidence": status == "NEED_MORE_EVIDENCE", "evidence_count": len(claim.get("evidence_urls") or [])}


class EvidenceIn(BaseModel):
    evidence_urls: list[str] = Field(min_length=1)


@app.post("/internal/v1/claims/{claim_no}/evidence", status_code=202)
def add_evidence(claim_no: str, body: EvidenceIn):
    with state_lock:
        claim = claims.get(claim_no)
        if claim is None:
            raise HTTPException(404, "claim not found")
        if claim.get("claim_status") != "NEED_MORE_EVIDENCE":
            raise HTTPException(409, "claim is not waiting for evidence")
        claim["evidence_urls"] = list(claim.get("evidence_urls") or []) + body.evidence_urls
        claim["claim_status"] = "SUBMITTED"
    return {"claim_no": claim_no, "status": "SUBMITTED"}


@app.get("/stub/admin/claims")
def list_claims():
    with state_lock:
        return {"items": deepcopy(list(claims.values()))}


@app.post("/stub/admin/fault")
def set_fault(body: FaultIn):
    global fault_mode
    with state_lock:
        fault_mode = body.mode
    return {"mode": body.mode}


@app.post("/stub/admin/approve")
def approve(body: ApproveIn):
    with state_lock:
        claim = deepcopy(claims.get(body.claim_no))
        mode = fault_mode
    if claim is None:
        raise HTTPException(404, "claim not found")
    payload = dict(refund_idempotency_key=f"REFUND_{body.claim_no}_V1",
                   claim_no=body.claim_no, order_id=claim["order_id"],
                   order_item_id=claim["order_item_id"],
                   approved_refund_cents=(body.approved_refund_cents if body.approved_refund_cents is not None
                                          else claim["max_refundable_cents"]),
                   restock_policy="DEFECTIVE_WAREHOUSE", approval_mode="SUPERVISOR_CONFIRMED",
                   supervisor_user_id="SUP_007", approved_at=datetime.now().isoformat())
    base_url = os.environ.get("PILOT_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
    token = os.environ.get("PILOT_INTERNAL_TOKEN", "dev-internal-token")
    with httpx.Client(timeout=30.0) as client:
        for _ in range(2 if mode == "duplicate_callback" else 1):
            response = client.post(f"{base_url}/internal/v1/refunds/execute",
                                   json=payload, headers={"X-Internal-Token": token})
    if mode == "refund_callback_timeout":
        key = payload["refund_idempotency_key"]
        with state_lock:
            pending.setdefault(key, {"refund_idempotency_key": key, "execute_payload": payload})
        raise HTTPException(504, "callback read timeout (injected)")
    return {"status_code": response.status_code, "response": response.json()}


@app.get("/stub/admin/pending")
def list_pending():
    with state_lock:
        return {"items": deepcopy(list(pending.values()))}


@app.post("/stub/admin/reconcile_done")
def reconcile_done(body: ReconcileIn):
    with state_lock:
        pending.pop(body.refund_idempotency_key, None)
    return {"status": "ok"}
