"""T2-D：聊天专项（报价卡 / 种草 / 会话鉴权 / 代理宕机降级）。"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

_CF_PATH = Path(__file__).resolve().parent / "conftest.py"
_CF_NAME = "tests_e2e_conftest_helpers"
if _CF_NAME not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(_CF_NAME, _CF_PATH)
    _CF = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    sys.modules[_CF_NAME] = _CF
    _SPEC.loader.exec_module(_CF)
else:
    _CF = sys.modules[_CF_NAME]

chat_stream = _CF.chat_stream
issue_dev_token = _CF.issue_dev_token
sse_cards = _CF.sse_cards
sse_errors = _CF.sse_errors
stop_procs = _CF.stop_procs
unique_user_id = _CF.unique_user_id
wait_health = _CF.wait_health


def test_e2e_chat_shopping_quote_card(e2e_stack):
    stack = e2e_stack
    token = issue_dev_token(stack, unique_user_id("USR_SHOP"))
    r, events = chat_stream(
        stack,
        token,
        session_id=f"SESS_{uuid.uuid4().hex[:12]}",
        message="我要买一台平板，再带洗发水和牛奶",
    )
    assert r.status_code == 200, f"shopping chat expected 200, got {r.status_code}: {r.text[:800]}"
    quote_cards = sse_cards(events, "quote")
    assert quote_cards, f"expected card type=quote; events={events!r}"
    card = quote_cards[0]
    quote = card.get("quote") if isinstance(card.get("quote"), dict) else card
    items = quote.get("items")
    assert isinstance(items, list) and items, f"quote card missing items: {card!r}"
    payable = quote.get("total_payable_cents")
    assert isinstance(payable, int), (
        f"total_payable_cents 应为整数，实际 type={type(payable).__name__} value={payable!r} card={card!r}"
    )


def test_e2e_chat_seeding_note_card(e2e_stack):
    stack = e2e_stack
    token = issue_dev_token(stack, unique_user_id("USR_SEED"))
    r, events = chat_stream(
        stack,
        token,
        session_id=f"SESS_{uuid.uuid4().hex[:12]}",
        message="帮我生成一份杭州出差采购种草笔记，要平板和洗发水",
    )
    assert r.status_code == 200, f"seeding chat expected 200, got {r.status_code}: {r.text[:800]}"
    note_cards = sse_cards(events, "seeding_note")
    assert note_cards, f"expected card type=seeding_note; events={events!r}"
    note = note_cards[0].get("note")
    assert isinstance(note, dict), f"seeding_note card missing note object: {note_cards[0]!r}"
    for field in ("title", "body", "images"):
        assert field in note, f"note missing {field!r}: {note!r}"
    assert isinstance(note["images"], list), f"note.images should be list: {note!r}"


def test_e2e_chat_session_cross_user_forbidden(e2e_stack):
    stack = e2e_stack
    session_id = f"SESS_SHARED_{uuid.uuid4().hex[:10]}"
    token_a = issue_dev_token(stack, unique_user_id("USR_A"))
    token_b = issue_dev_token(stack, unique_user_id("USR_B"))

    r_a, _ = chat_stream(stack, token_a, session_id=session_id, message="你好")
    assert r_a.status_code == 200, (
        f"user A first chat expected 200, got {r_a.status_code}: {r_a.text[:500]}"
    )

    r_b, _ = chat_stream(stack, token_b, session_id=session_id, message="我也要用这个会话")
    assert r_b.status_code == 403, (
        f"user B reusing session_id should get 403, got {r_b.status_code}: {r_b.text[:500]}"
    )
    try:
        code = r_b.json().get("error", {}).get("code")
    except Exception:  # noqa: BLE001
        code = None
    assert code == "FORBIDDEN", f"expected error.code=FORBIDDEN, body={r_b.text[:500]}"


def test_e2e_chat_missing_token_unauthorized(e2e_stack):
    stack = e2e_stack
    r, _ = chat_stream(
        stack,
        token="",
        session_id=f"SESS_{uuid.uuid4().hex[:12]}",
        message="你好",
    )
    assert r.status_code == 401, f"missing token expected 401, got {r.status_code}: {r.text[:500]}"
    try:
        code = r.json().get("error", {}).get("code")
    except Exception:  # noqa: BLE001
        code = None
    assert code == "UNAUTHORIZED", f"expected error.code=UNAUTHORIZED, body={r.text[:500]}"


def test_e2e_chat_forged_user_id_in_body_ignored(e2e_stack):
    """请求体夹带他人 user_id 不生效：身份仍以 JWT 为准。"""
    stack = e2e_stack
    user_a = unique_user_id("USR_FORGE_A")
    user_b = unique_user_id("USR_FORGE_B")
    token_a = issue_dev_token(stack, user_a)
    session_id = f"SESS_{uuid.uuid4().hex[:12]}"

    r1, _ = chat_stream(
        stack,
        token_a,
        session_id=session_id,
        message="随便聊聊天气",
        extra_body={"user_id": user_b, "tenant_id": "OTHER_TENANT"},
    )
    assert r1.status_code == 200, (
        f"forged user_id in body should still succeed as JWT user A, "
        f"got {r1.status_code}: {r1.text[:500]}"
    )

    # 用户 B 用同一 session → 仍应 403，证明会话绑的是 A 而非 body 里的 B
    token_b = issue_dev_token(stack, user_b)
    r2, _ = chat_stream(stack, token_b, session_id=session_id, message="越权试探")
    assert r2.status_code == 403, (
        f"session should belong to JWT user A (body user_id ignored); "
        f"user B got {r2.status_code}: {r2.text[:500]}"
    )


def test_e2e_chat_selection_down_emits_error_event(e2e_stack_killable_selection):
    stack = e2e_stack_killable_selection
    assert stack.selection_proc is not None, "killable stack missing selection_proc"
    stop_procs([stack.selection_proc])
    # 从总进程列表去掉已终止的 selection，避免 fixture teardown 重复 wait
    stack._procs = [p for p in stack._procs if p is not stack.selection_proc]

    sel_url = stack.agent_urls["selection"]
    dead = False
    try:
        wait_health(sel_url, timeout=2.0)
    except RuntimeError:
        dead = True
    assert dead, f"selection process should be down at {sel_url}"

    token = issue_dev_token(stack, unique_user_id("USR_SELDOWN"))
    r, events = chat_stream(
        stack,
        token,
        session_id=f"SESS_{uuid.uuid4().hex[:12]}",
        message="我要买一台平板，再带洗发水和牛奶",
        timeout=30.0,
    )
    assert r.status_code == 200, (
        f"selection 宕机时聊天应 HTTP 200（流内 error），不得 500；"
        f"实际 {r.status_code}: {r.text[:800]}"
    )
    errs = sse_errors(events)
    assert errs, f"expected event:error when selection is down; events={events!r}"
    event_names = [e.get("event") for e in events]
    assert "error" in event_names, f"SSE should contain event:error; names={event_names}"
