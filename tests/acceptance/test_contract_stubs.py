"""T2-E：外部依赖桩 OpenAPI 契约一致性（对照 contracts/openapi/stubs.yaml）。"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest

_CC_PATH = Path(__file__).resolve().parent / "conftest_contract.py"
_CC_NAME = "tests_acceptance_conftest_contract"
if _CC_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CC_NAME, _CC_PATH)
    _CC = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CC_NAME] = _CC
    _SPEC.loader.exec_module(_CC)
else:
    _CC = sys.modules[_CC_NAME]

assert_response_matches_schema = _CC.assert_response_matches_schema
load_openapi = _CC.load_openapi
stubs_live_stack_cm = _CC.stubs_live_stack_cm


@pytest.fixture(scope="module")
def stubs_spec():
    return load_openapi("stubs.yaml")


@pytest.fixture(scope="module")
def stubs_stack():
    with stubs_live_stack_cm() as s:
        yield s


def test_contract_nlu_analyze(stubs_stack, stubs_spec):
    path, method = "/api/v1/nlu/analyze", "post"
    with httpx.Client(base_url=stubs_stack.nlu_base_url, timeout=15.0) as client:
        r = client.post(path, json={"text": "热水器漏水"})
    body = assert_response_matches_schema(
        stubs_spec,
        path,
        method,
        r,
        context=f"NLU {method.upper()} {path}",
    )
    assert r.status_code == 200, f"NLU analyze expected 200, got {r.status_code}: {r.text}"
    assert isinstance(body, dict)


def test_contract_rag_search(stubs_stack, stubs_spec):
    path, method = "/api/v1/rag/search", "post"
    with httpx.Client(base_url=stubs_stack.rag_base_url, timeout=15.0) as client:
        r = client.post(
            path,
            json={
                "query": "退货",
                "catalog_category_id": "HOME_APPLIANCE_HEATER",
                "top_k": 3,
            },
        )
    body = assert_response_matches_schema(
        stubs_spec,
        path,
        method,
        r,
        context=f"RAG {method.upper()} {path}",
    )
    assert r.status_code == 200, f"RAG search expected 200, got {r.status_code}: {r.text}"
    assert isinstance(body, dict)


def test_contract_ops_claims_submit(stubs_stack, stubs_spec):
    path, method = "/internal/v1/claims/submit", "post"
    claim_no = f"CLM_CONTRACT_{uuid.uuid4().hex[:12]}"
    payload = {
        "claim_no": claim_no,
        "tenant_id": "SHOP_SELF_01",
        "user_id": "USR_A",
        "order_id": "ORD_CONTRACT_1",
        "order_item_id": "ITEM_CONTRACT_1",
        "sku_id": "SKU_HEATER_003",
        "max_refundable_cents": 112000,
        "catalog_category_id": "HOME_APPLIANCE_HEATER",
        "item_payable_cents": 112000,
        "days_since_delivery": 3,
        "claim_type": "RETURN_REFUND",
        "buyer_reason_text": "漏水",
        "evidence_urls": ["https://storage.example.invalid/e1.jpg"],
    }
    with httpx.Client(base_url=stubs_stack.ops_base_url, timeout=15.0) as client:
        r = client.post(path, json=payload)
    body = assert_response_matches_schema(
        stubs_spec,
        path,
        method,
        r,
        context=f"Ops {method.upper()} {path}",
    )
    assert r.status_code == 202, (
        f"Ops claims/submit expected 202, got {r.status_code}: {r.text}"
    )
    assert isinstance(body, dict)
