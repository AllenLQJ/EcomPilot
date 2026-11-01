"""EcomOps 回写售后终态：驳回/撤回后订单恢复 FULFILLED（幂等）。"""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from pilot.core.auth import require_internal
from pilot.core.db import get_engine
from pilot.core.errors import ApiError

router = APIRouter(dependencies=[Depends(require_internal)])


class ResultIn(BaseModel):
    result: Literal["REJECTED", "WITHDRAWN"]


@router.post("/internal/v1/aftersales/{claim_no}/result")
def claim_result(claim_no: str, body: ResultIn):
    with get_engine().begin() as conn:
        req = conn.execute(text("SELECT order_id, claim_status FROM aftersale_requests WHERE claim_no=:c FOR UPDATE"),
                           {"c": claim_no}).first()
        if req is None:
            raise ApiError(404, "NOT_FOUND", "claim not found")
        order_id, status = req
        if status == "COMPLETED":
            raise ApiError(409, "INVALID_ORDER_STATE", "claim already completed")
        conn.execute(text("UPDATE orders SET order_status='FULFILLED' WHERE order_id=:o AND order_status='AFTER_SALE_OPEN'"),
                     {"o": order_id})
        if body.result == "WITHDRAWN":
            conn.execute(text("DELETE FROM aftersale_requests WHERE claim_no=:c"), {"c": claim_no})   # 撤回后允许重新申请
        else:
            conn.execute(text("UPDATE aftersale_requests SET claim_status='REJECTED' WHERE claim_no=:c"), {"c": claim_no})
    return {"claim_no": claim_no, "result": body.result, "order_id": order_id}
