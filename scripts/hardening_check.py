"""W3 异常分支实测（对运行中的栈）：Ops 故障补偿、重复提交、代理/Ops 宕机时的对话降级。"""
import os, signal, subprocess, sys, time
import httpx

P, OPS, TOK = "http://127.0.0.1:8085", "http://127.0.0.1:8000", "dev-internal-token"
c = httpx.Client(timeout=30)


def user():
    u = f"H_{time.time_ns()}"
    t = c.post(f"{P}/api/v1/dev/token", json={"user_id": u}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def fulfilled_order(h):
    q = c.post(f"{P}/api/v1/quotes", json={"items": [{"sku_id": "SKU_MILK_001", "qty": 1}]}, headers=h).json()
    o = c.post(f"{P}/api/v1/orders/create", json={"idempotency_key": h["Authorization"][-12:] + str(time.time_ns()), "quote_id": q["quote_id"]}, headers=h).json()
    c.post(f"{P}/api/v1/orders/{o['order_id']}/pay", headers=h)
    c.post(f"{P}/internal/v1/orders/{o['order_id']}/fulfill", headers={"X-Internal-Token": TOK})
    return o["order_id"], o["items"][0]["order_item_id"]


def status(h, oid):
    return c.get(f"{P}/api/v1/orders/{oid}", headers=h).json()["order_status"]


def sse(h, body):
    r = c.post(f"{P}/api/v1/chat/stream", json=body, headers=h)
    return r.status_code, [l for l in r.text.splitlines() if l.startswith("event:")]


def kill(name):
    pid = int(open(f".run/{name}.pid").read())
    os.kill(pid, signal.SIGTERM); time.sleep(1)


ok = True
def check(name, cond, info=""):
    global ok
    ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, info)


h = user(); oid, item = fulfilled_order(h)
body = {"order_id": oid, "order_item_id": item, "buyer_reason_text": "坏了"}
c.post(f"{OPS}/stub/admin/fault", json={"mode": "submit_fail"})
r = c.post(f"{P}/api/v1/aftersales/confirm", json=body, headers=h)
check("ops submit_fail -> 502/504", r.status_code in (502, 504), r.status_code)
check("order rolled back to FULFILLED", status(h, oid) == "FULFILLED")
c.post(f"{OPS}/stub/admin/fault", json={"mode": "none"})
r1 = c.post(f"{P}/api/v1/aftersales/confirm", json=body, headers=h)
r2 = c.post(f"{P}/api/v1/aftersales/confirm", json=body, headers=h)
check("retry after recovery -> 202", r1.status_code == 202, r1.status_code)
check("duplicate confirm -> 409", r2.status_code == 409, r2.status_code)

h2 = user()
kill("selection")
code, ev = sse(h2, {"session_id": "hs1", "message": "我想买一台平板"})
check("selection down: 200 + event error", code == 200 and "event: error" in ev, (code, ev))

h3 = user(); oid3, item3 = fulfilled_order(h3)
kill("ops_stub")
code, ev = sse(h3, {"session_id": "hs2", "message": "退货", "confirmed_order_id": oid3, "confirmed_order_item_id": item3})
check("ops down via chat: 200 + event error", code == 200 and "event: error" in ev, (code, ev))
check("order rolled back after ops down", status(h3, oid3) == "FULFILLED")
sys.exit(0 if ok else 1)
