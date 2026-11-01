"""订单内核：幂等下单 + 乐观锁扣库存 + 券锁 + 模拟支付 + 取消 + 发货。"""
import json
import random
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from pilot.core.auth import Identity
from pilot.core.config import get_settings
from pilot.core.errors import ApiError
from pilot.orders import quotes
from pilot.orders.common import iso, jload, new_order_id

BACKOFF_SECONDS = (0.02, 0.05, 0.10)


class _Conflict(Exception):
    pass


def order_view(conn, order_id: str, replayed: bool | None = None) -> dict:
    o = conn.execute(text("SELECT * FROM orders WHERE order_id=:o"), {"o": order_id}).mappings().first()
    items = conn.execute(text("SELECT * FROM order_items WHERE order_id=:o ORDER BY order_item_id"),
                         {"o": order_id}).mappings().all()
    v = {"order_id": o["order_id"], "order_status": o["order_status"], "quote_id": o["quote_id"],
         "total_original_cents": int(o["total_original_cents"]), "total_discount_cents": int(o["total_discount_cents"]),
         "total_payable_cents": int(o["total_payable_cents"]), "refunded_cents": int(o["refunded_cents"]),
         "created_at": iso(o["created_at"]), "pay_deadline_at": iso(o["pay_deadline_at"]),
         "items": [{k: (int(i[k]) if k.endswith("_cents") or k == "qty" else i[k]) for k in
                    ("order_item_id", "sku_id", "sku_name", "catalog_category_id", "qty", "unit_price_cents",
                     "original_cents", "discount_share_cents", "payable_cents", "refunded_cents")} for i in items]}
    if replayed is not None:
        v["idempotent_replayed"] = replayed
    return v


def _find_by_key(conn, ident: Identity, key: str):
    return conn.execute(
        text("SELECT order_id, quote_id FROM orders WHERE tenant_id=:t AND user_id=:u AND idempotency_key=:k"),
        {"t": ident.tenant_id, "u": ident.user_id, "k": key}).mappings().first()


@contextmanager
def _key_lock(engine, name: str):
    """同一幂等键串行化，避免重复请求各自扣库存后再回滚。拿不到锁时退化为唯一索引兜底。"""
    conn = engine.connect()
    got = False
    try:
        got = conn.exec_driver_sql("SELECT GET_LOCK(%s, 10)", (name,)).scalar() == 1
        yield
    finally:
        try:
            if got:
                conn.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (name,))
        finally:
            conn.close()


def create_order(engine, ident: Identity, idempotency_key: str, quote_id: str) -> tuple[dict, int]:
    lock = f"ecp:idem:{ident.tenant_id}:{ident.user_id}:{idempotency_key}"[:60]
    with _key_lock(engine, lock):
        with engine.connect() as conn:
            ex = _find_by_key(conn, ident, idempotency_key)
            if ex:
                return _replay(conn, ex, quote_id)
            quote = quotes.get_owned_quote(conn, ident, quote_id)
            snap = jload(quote["quote_snapshot_json"])
            fresh = _validate_quote(engine, conn, ident, quote, snap)
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            try:
                return _try_create(engine, ident, idempotency_key, quote_id, fresh), 201
            except _Conflict:
                if attempt == len(BACKOFF_SECONDS):
                    raise ApiError(503, "CONCURRENT_VERSION_CONFLICT", "system busy, please retry")
                time.sleep(BACKOFF_SECONDS[attempt] * (1 + random.random() * 0.5))
            except _Duplicate:
                with engine.connect() as conn:
                    return _replay(conn, _find_by_key(conn, ident, idempotency_key), quote_id)
    raise AssertionError("unreachable")


class _Duplicate(Exception):
    pass


def _replay(conn, ex, quote_id: str) -> tuple[dict, int]:
    if ex["quote_id"] != quote_id:
        raise ApiError(409, "IDEMPOTENCY_KEY_CONFLICT", "idempotency key used with a different quote")
    return order_view(conn, ex["order_id"], replayed=True), 200


def _validate_quote(engine, conn, ident: Identity, quote, snap: dict) -> dict:
    expired = quote["expires_at"] < datetime.now()
    qty = quotes.merge_items([{"sku_id": i["sku_id"], "qty": i["qty"]} for i in snap["items"]])
    changed = False
    fresh = None
    try:
        fresh = quotes.compute_quote(conn, ident, qty, snap.get("requested_coupon_ids"), with_addons=False)
        changed = (fresh["total_payable_cents"] != snap["total_payable_cents"]
                   or fresh["coupon_ids"] != snap["coupon_ids"]
                   or [i["payable_cents"] for i in fresh["items"]] != [i["payable_cents"] for i in snap["items"]])
    except ApiError as e:
        if e.code != "COUPON_UNAVAILABLE":
            raise
        changed = True
    if expired or changed:
        with engine.begin() as c2:
            try:
                latest = quotes.compute_quote(c2, ident, qty, None if fresh is None else snap.get("requested_coupon_ids"))
            except ApiError:
                latest = quotes.compute_quote(c2, ident, qty, None)
            latest = quotes.public_quote(quotes.persist_quote(c2, ident, latest))
        raise ApiError(409, "QUOTE_EXPIRED_OR_CHANGED", "quote expired or price changed", {"quote": latest})
    return snap


def _try_create(engine, ident: Identity, key: str, quote_id: str, snap: dict) -> dict:
    now = datetime.now()
    order_id = new_order_id()
    need: dict[str, int] = {}
    for it in snap["items"]:
        need[it["sku_id"]] = need.get(it["sku_id"], 0) + it["qty"]
    conn = engine.connect()
    trans = conn.begin()
    try:
        for sku_id in sorted(need):
            row = conn.execute(text("SELECT sellable_stock, version FROM products_sku WHERE sku_id=:s"),
                               {"s": sku_id}).first()
            if row is None:
                raise ApiError(404, "NOT_FOUND", "sku not found", {"sku_id": sku_id})
            if row[0] < need[sku_id]:
                raise ApiError(422, "INSUFFICIENT_STOCK", "insufficient stock",
                               {"sku_id": sku_id, "available": int(row[0]), "required": need[sku_id]})
            res = conn.execute(
                text("UPDATE products_sku SET sellable_stock=sellable_stock-:q, version=version+1 "
                     "WHERE sku_id=:s AND version=:v AND sellable_stock>=:q"),
                {"q": need[sku_id], "s": sku_id, "v": row[1]})
            if res.rowcount == 0:
                raise _Conflict()
        if snap["coupon_ids"]:
            res = conn.execute(
                text("UPDATE user_coupons SET status='LOCKED', order_id=:o WHERE user_coupon_id IN :ids "
                     "AND status='UNUSED' AND tenant_id=:t AND user_id=:u AND expires_at>:now")
                .bindparams(bindparam("ids", expanding=True)),
                {"o": order_id, "ids": snap["coupon_ids"], "t": ident.tenant_id, "u": ident.user_id, "now": now})
            if res.rowcount != len(snap["coupon_ids"]):
                raise ApiError(409, "COUPON_UNAVAILABLE", "coupon unavailable")
        try:
            conn.execute(
                text("INSERT INTO orders (order_id,idempotency_key,quote_id,tenant_id,user_id,total_original_cents,"
                     "total_discount_cents,total_payable_cents,order_status,created_at,pay_deadline_at) VALUES "
                     "(:o,:k,:q,:t,:u,:to,:td,:tp,'STOCK_LOCKED',:now,:dl)"),
                {"o": order_id, "k": key, "q": quote_id, "t": ident.tenant_id, "u": ident.user_id,
                 "to": snap["total_original_cents"], "td": snap["total_discount_cents"],
                 "tp": snap["total_payable_cents"], "now": now,
                 "dl": now + timedelta(seconds=get_settings().pay_timeout_seconds)})
        except IntegrityError:
            raise _Duplicate()
        for n, it in enumerate(snap["items"], 1):
            conn.execute(
                text("INSERT INTO order_items (order_item_id,order_id,sku_id,shop_id,catalog_category_id,sku_name,qty,"
                     "unit_price_cents,original_cents,discount_share_cents,payable_cents) VALUES "
                     "(:i,:o,:s,:sh,:c,:n,:q,:up,:oc,:ds,:pc)"),
                {"i": f"{order_id}_I{n:02d}", "o": order_id, "s": it["sku_id"], "sh": it["shop_id"],
                 "c": it["catalog_category_id"], "n": it["sku_name"], "q": it["qty"], "up": it["unit_price_cents"],
                 "oc": it["original_cents"], "ds": it["discount_share_cents"], "pc": it["payable_cents"]})
        trans.commit()
        trans = None
        return order_view(conn, order_id, replayed=False)
    except BaseException:
        if trans is not None:
            trans.rollback()
        raise
    finally:
        conn.close()


def _owned_status(conn, ident: Identity, order_id: str):
    row = conn.execute(
        text("SELECT order_status, cancelled_at FROM orders WHERE order_id=:o AND tenant_id=:t AND user_id=:u"),
        {"o": order_id, "t": ident.tenant_id, "u": ident.user_id}).first()
    if not row:
        raise ApiError(404, "NOT_FOUND", "order not found")
    return row


def get_order(engine, ident: Identity, order_id: str) -> dict:
    with engine.connect() as conn:
        _owned_status(conn, ident, order_id)
        return order_view(conn, order_id)


def list_orders(engine, ident: Identity) -> list[dict]:
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(
            text("SELECT order_id FROM orders WHERE tenant_id=:t AND user_id=:u ORDER BY created_at DESC LIMIT 100"),
            {"t": ident.tenant_id, "u": ident.user_id})]
        return [order_view(conn, i) for i in ids]


def _restore_stock(conn, order_id: str) -> None:
    for sku_id, q in conn.execute(text("SELECT sku_id, SUM(qty) FROM order_items WHERE order_id=:o GROUP BY sku_id "
                                       "ORDER BY sku_id"), {"o": order_id}).all():
        conn.execute(text("UPDATE products_sku SET sellable_stock=sellable_stock+:q, version=version+1 "
                          "WHERE sku_id=:s"), {"q": int(q), "s": sku_id})


def pay_order(engine, ident: Identity, order_id: str) -> dict:
    with engine.begin() as conn:
        st = _owned_status(conn, ident, order_id)[0]
        res = conn.execute(
            text("UPDATE orders SET order_status='PAID_SIMULATED', paid_at=:now WHERE order_id=:o "
                 "AND order_status='STOCK_LOCKED' AND pay_deadline_at>=:now"), {"o": order_id, "now": datetime.now()})
        if res.rowcount == 1:
            conn.execute(text("UPDATE user_coupons SET status='USED' WHERE order_id=:o AND status='LOCKED'"),
                         {"o": order_id})
            return order_view(conn, order_id)
        st = _owned_status(conn, ident, order_id)[0]
        if st == "PAID_SIMULATED":
            return order_view(conn, order_id)
    if st == "STOCK_LOCKED":
        expire_unpaid_orders(engine)
    raise ApiError(409, "INVALID_ORDER_STATE", f"order is {st}, cannot pay")


def cancel_order(engine, ident: Identity, order_id: str) -> dict:
    with engine.begin() as conn:
        st, cancelled_at = _owned_status(conn, ident, order_id)
        now = datetime.now()
        res = conn.execute(
            text("UPDATE orders SET order_status='CANCELLED', cancelled_at=:now WHERE order_id=:o "
                 "AND order_status='STOCK_LOCKED'"), {"o": order_id, "now": now})
        if res.rowcount == 1:
            _restore_stock(conn, order_id)
            conn.execute(text("UPDATE user_coupons SET status='UNUSED', order_id=NULL WHERE order_id=:o"),
                         {"o": order_id})
            return order_view(conn, order_id)
        res = conn.execute(
            text("UPDATE orders SET order_status='REFUNDED_FULL', cancelled_at=:now, refunded_cents=total_payable_cents "
                 "WHERE order_id=:o AND order_status='PAID_SIMULATED'"), {"o": order_id, "now": now})
        if res.rowcount == 1:
            conn.execute(text("UPDATE order_items SET refunded_cents=payable_cents WHERE order_id=:o"), {"o": order_id})
            _restore_stock(conn, order_id)
            conn.execute(text("UPDATE user_coupons SET status='UNUSED', order_id=NULL WHERE order_id=:o"),
                         {"o": order_id})
            return order_view(conn, order_id)
        # 并发下状态可能已被另一取消请求推进：重新读取再判定幂等
        st, cancelled_at = _owned_status(conn, ident, order_id)
        if st == "CANCELLED" or (st == "REFUNDED_FULL" and cancelled_at is not None):
            return order_view(conn, order_id)
    raise ApiError(409, "INVALID_ORDER_STATE", f"order is {st}, cannot cancel")


def fulfill_order(engine, order_id: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(text("SELECT order_status FROM orders WHERE order_id=:o"), {"o": order_id}).first()
        if not row:
            raise ApiError(404, "NOT_FOUND", "order not found")
        res = conn.execute(
            text("UPDATE orders SET order_status='FULFILLED', fulfilled_at=:now WHERE order_id=:o "
                 "AND order_status='PAID_SIMULATED'"), {"o": order_id, "now": datetime.now()})
        if res.rowcount == 1:
            return order_view(conn, order_id)
        cur = conn.execute(text("SELECT order_status FROM orders WHERE order_id=:o"), {"o": order_id}).scalar()
        if cur == "FULFILLED":
            return order_view(conn, order_id)
    raise ApiError(409, "INVALID_ORDER_STATE", f"order is {cur}, cannot fulfill")


def expire_unpaid_orders(engine) -> int:
    """超时未支付的 STOCK_LOCKED 订单自动取消（幂等，库存/券只释放一次）。"""
    n = 0
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(
            text("SELECT order_id FROM orders WHERE order_status='STOCK_LOCKED' AND pay_deadline_at<:now"),
            {"now": datetime.now()})]
    for oid in ids:
        with engine.begin() as conn:
            res = conn.execute(text("UPDATE orders SET order_status='CANCELLED', cancelled_at=:now WHERE order_id=:o "
                                    "AND order_status='STOCK_LOCKED'"), {"o": oid, "now": datetime.now()})
            if res.rowcount == 1:
                _restore_stock(conn, oid)
                conn.execute(text("UPDATE user_coupons SET status='UNUSED', order_id=NULL WHERE order_id=:o"),
                             {"o": oid})
                n += 1
    return n
