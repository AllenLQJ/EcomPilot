"""AC-SEC-01：跨用户越权拦截与免单号候选确认（确认前不写 aftersale_requests）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from pilot.core.auth import issue_token

_CF_PATH = Path(__file__).resolve().parent / "conftest_f.py"
_CF_NAME = "tests_acceptance_conftest_f"
if _CF_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CF_NAME, _CF_PATH)
    _CF = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CF_NAME] = _CF
    _SPEC.loader.exec_module(_CF)
else:
    _CF = sys.modules[_CF_NAME]

f_bearer = _CF.f_bearer
f_count_aftersale_requests = _CF.f_count_aftersale_requests
f_order_status = _CF.f_order_status
f_place_heater_order = _CF.f_place_heater_order
f_confirm_aftersale = _CF.f_confirm_aftersale
F_USER_A = _CF.F_USER_A
F_USER_B = _CF.F_USER_B


@pytest.fixture
def f_acceptance_stack():
    with _CF.f_live_stack_cm() as stack:
        yield stack


def test_ac_sec_01_cross_user_confirm_forbidden(f_acceptance_stack):
    stack = f_acceptance_stack
    a = f_place_heater_order(stack, F_USER_A, pay=True, fulfill=True)
    b = f_place_heater_order(stack, F_USER_B, pay=True, fulfill=True)
    assert a["order_status"] == "FULFILLED"
    assert b["order_status"] == "FULFILLED"

    before = f_count_aftersale_requests()
    b_status_before = f_order_status(b["order_id"])

    r = f_confirm_aftersale(
        stack,
        F_USER_A,
        b["order_id"],
        b["order_item_id"],
        extra_body={"user_id": F_USER_B},
    )
    assert r.status_code in (403, 404), r.text
    assert f_order_status(b["order_id"]) == b_status_before
    assert f_count_aftersale_requests() == before


def test_ac_sec_01_candidates_own_only_no_records(f_acceptance_stack):
    stack = f_acceptance_stack
    a = f_place_heater_order(stack, F_USER_A, pay=True, fulfill=True)
    b = f_place_heater_order(stack, F_USER_B, pay=True, fulfill=True)

    before = f_count_aftersale_requests()
    assert before == 0

    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.post(
            "/api/v1/aftersales/candidates",
            headers=f_bearer(F_USER_A),
            json={"text": "热水器漏水了怎么退"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "candidates" in body
    ids = {c["order_id"] for c in body["candidates"]}
    assert a["order_id"] in ids
    assert b["order_id"] not in ids
    assert f_count_aftersale_requests() == 0


def test_ac_sec_01_get_order_cross_user(f_acceptance_stack):
    stack = f_acceptance_stack
    f_place_heater_order(stack, F_USER_A, pay=True, fulfill=True)
    b = f_place_heater_order(stack, F_USER_B, pay=True, fulfill=True)

    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.get(f"/api/v1/orders/{b['order_id']}", headers=f_bearer(F_USER_A))
    assert r.status_code in (403, 404), r.text


def test_ac_sec_01_token_missing_tampered_expired(f_acceptance_stack):
    stack = f_acceptance_stack
    a = f_place_heater_order(stack, F_USER_A, pay=True, fulfill=True)

    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r_none = client.post(
            "/api/v1/aftersales/candidates",
            json={"text": "热水器漏水了怎么退"},
        )
        assert r_none.status_code == 401, r_none.text

        good = issue_token(F_USER_A)
        tampered = good[:-6] + ("aaaaaa" if not good.endswith("aaaaaa") else "bbbbbb")
        r_bad = client.post(
            "/api/v1/aftersales/candidates",
            headers={"Authorization": f"Bearer {tampered}"},
            json={"text": "热水器漏水了怎么退"},
        )
        assert r_bad.status_code == 401, r_bad.text

        expired = issue_token(F_USER_A, ttl=-10)
        r_exp = client.get(
            f"/api/v1/orders/{a['order_id']}",
            headers={"Authorization": f"Bearer {expired}"},
        )
        assert r_exp.status_code == 401, r_exp.text


def test_ac_sec_01_cancelled_not_in_candidates(f_acceptance_stack):
    stack = f_acceptance_stack
    fulfilled = f_place_heater_order(stack, F_USER_A, pay=True, fulfill=True)
    paid = f_place_heater_order(stack, F_USER_A, pay=True, fulfill=False)
    cancelled = f_place_heater_order(stack, F_USER_A, pay=False, fulfill=False, cancel=True)

    assert paid["order_status"] == "PAID_SIMULATED"
    assert cancelled["order_status"] == "CANCELLED"

    with httpx.Client(base_url=stack.pilot_base_url, timeout=30.0) as client:
        r = client.post(
            "/api/v1/aftersales/candidates",
            headers=f_bearer(F_USER_A),
            json={"text": "热水器漏水了怎么退"},
        )
    assert r.status_code == 200, r.text
    ids = {c["order_id"] for c in r.json()["candidates"]}
    assert fulfilled["order_id"] in ids
    assert paid["order_id"] in ids
    assert cancelled["order_id"] not in ids
