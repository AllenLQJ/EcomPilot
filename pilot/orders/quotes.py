"""报价：商品价 + 品类券 + 跨店满减 + 受控凑单建议（算法见 pilot/bundle）。"""
import json
from datetime import datetime, timedelta

from sqlalchemy import bindparam, text

from pilot.bundle.bundle_math_tools import price_lines
from pilot.core.auth import Identity
from pilot.core.config import get_settings
from pilot.core.errors import ApiError
from pilot.mcp_tools.bundle_tool import suggest_bundle
from pilot.orders.common import iso, jload, new_id


def _load_skus(conn, ident: Identity, qty_by_sku: dict[str, int]) -> list[dict]:
    rows = conn.execute(
        text("SELECT sku_id, shop_id, catalog_category_id, sku_name, unit_price_cents FROM products_sku "
             "WHERE tenant_id=:t AND sku_id IN :ids").bindparams(bindparam("ids", expanding=True)),
        {"t": ident.tenant_id, "ids": list(qty_by_sku)}).mappings().all()
    by_id = {r["sku_id"]: r for r in rows}
    missing = [s for s in qty_by_sku if s not in by_id]
    if missing:
        raise ApiError(404, "NOT_FOUND", "sku not found", {"sku_ids": missing})
    return [{**dict(by_id[s]), "qty": q} for s, q in qty_by_sku.items()]


def _load_coupons(conn, ident: Identity, requested: list[str] | None) -> list[dict]:
    rows = conn.execute(
        text("SELECT user_coupon_id, catalog_category_id, threshold_cents, discount_cents FROM user_coupons "
             "WHERE tenant_id=:t AND user_id=:u AND status='UNUSED' AND expires_at>:now"),
        {"t": ident.tenant_id, "u": ident.user_id, "now": datetime.now()}).mappings().all()
    coupons = [dict(r) for r in rows]
    if requested is not None:
        have = {c["user_coupon_id"] for c in coupons}
        bad = [c for c in requested if c not in have]
        if bad:
            raise ApiError(409, "COUPON_UNAVAILABLE", "coupon unavailable", {"coupon_ids": bad})
        coupons = [c for c in coupons if c["user_coupon_id"] in set(requested)]
    return coupons


def compute_quote(conn, ident: Identity, qty_by_sku: dict[str, int], requested_coupons: list[str] | None,
                  with_addons: bool = True) -> dict:
    skus = _load_skus(conn, ident, qty_by_sku)
    lines = [{"sku_id": s["sku_id"], "shop_id": s["shop_id"], "catalog_category_id": s["catalog_category_id"],
              "qty": s["qty"], "unit_price_cents": int(s["unit_price_cents"])} for s in skus]
    coupons = _load_coupons(conn, ident, requested_coupons)
    pr = price_lines(lines, coupons, _promo_rules(conn, ident.tenant_id))
    items = []
    for s, ln in zip(skus, pr["lines"]):
        items.append({"sku_id": s["sku_id"], "sku_name": s["sku_name"], "qty": s["qty"], "shop_id": s["shop_id"],
                      "catalog_category_id": s["catalog_category_id"], "unit_price_cents": int(s["unit_price_cents"]),
                      "original_cents": ln["original_cents"], "discount_share_cents": ln["discount_share_cents"],
                      "payable_cents": ln["payable_cents"]})
    addons = []
    if with_addons:
        sug = suggest_bundle(conn.engine, ident.tenant_id, lines, coupons)
        if sug:
            names = {r[0]: r[1] for r in conn.execute(
                text("SELECT sku_id, sku_name FROM products_sku WHERE sku_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": [a["sku_id"] for a in sug["addons"]]})}
            addons = [{"sku_id": a["sku_id"], "sku_name": names.get(a["sku_id"], a["sku_id"]),
                       "unit_price_cents": a["unit_price_cents"], "qty": a.get("qty", 1),
                       "saving_cents": sug["saving_cents"],
                       "payable_with_addon_cents": sug["payable_with_addon_cents"]} for a in sug["addons"]]
    return {"items": items, "coupon_ids": [pr["coupon_id"]] if pr["coupon_id"] else [],
            "promo_rule_id": pr["promo_rule_id"], "total_original_cents": pr["total_original_cents"],
            "total_discount_cents": pr["total_discount_cents"], "total_payable_cents": pr["total_payable_cents"],
            "gap_to_next_tier_cents": pr["gap_to_next_tier_cents"], "suggested_addons": addons,
            "requested_coupon_ids": requested_coupons}


def _promo_rules(conn, tenant_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        text("SELECT rule_id, threshold_cents, discount_cents FROM promo_rules WHERE tenant_id=:t AND is_active=1"),
        {"t": tenant_id}).mappings()]


def persist_quote(conn, ident: Identity, q: dict) -> dict:
    now = datetime.now()
    q = {**q, "quote_id": new_id("QT"), "expires_at": iso(now + timedelta(seconds=get_settings().quote_ttl_seconds))}
    conn.execute(
        text("INSERT INTO price_quotes (quote_id,tenant_id,user_id,items_json,coupon_ids_json,total_original_cents,"
             "total_discount_cents,total_payable_cents,quote_snapshot_json,suggested_addons_json,created_at,expires_at) "
             "VALUES (:id,:t,:u,:items,:cpn,:o,:d,:p,:snap,:add,:now,:exp)"),
        {"id": q["quote_id"], "t": ident.tenant_id, "u": ident.user_id,
         "items": json.dumps([{"sku_id": i["sku_id"], "qty": i["qty"]} for i in q["items"]]),
         "cpn": json.dumps(q["coupon_ids"]), "o": q["total_original_cents"], "d": q["total_discount_cents"],
         "p": q["total_payable_cents"], "snap": json.dumps(q, ensure_ascii=False),
         "add": json.dumps(q["suggested_addons"], ensure_ascii=False), "now": now,
         "exp": datetime.strptime(q["expires_at"], "%Y-%m-%dT%H:%M:%S")})
    return q


def public_quote(q: dict) -> dict:
    return {k: v for k, v in q.items() if k not in ("requested_coupon_ids",)}


def merge_items(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it["sku_id"]] = out.get(it["sku_id"], 0) + int(it["qty"])
    return out


def create_quote(engine, ident: Identity, items: list[dict], coupon_ids: list[str] | None) -> dict:
    with engine.begin() as conn:
        q = compute_quote(conn, ident, merge_items(items), coupon_ids)
        return public_quote(persist_quote(conn, ident, q))


def get_owned_quote(conn, ident: Identity, quote_id: str):
    row = conn.execute(
        text("SELECT * FROM price_quotes WHERE quote_id=:q AND tenant_id=:t AND user_id=:u"),
        {"q": quote_id, "t": ident.tenant_id, "u": ident.user_id}).mappings().first()
    if not row:
        raise ApiError(404, "NOT_FOUND", "quote not found")
    return row


def confirm_addons(engine, ident: Identity, quote_id: str, addon_sku_ids: list[str]) -> dict:
    with engine.begin() as conn:
        row = get_owned_quote(conn, ident, quote_id)
        if row["expires_at"] < datetime.now():
            raise ApiError(409, "QUOTE_EXPIRED_OR_CHANGED", "quote expired")
        snap = jload(row["quote_snapshot_json"])
        suggested = {a["sku_id"]: a for a in jload(row["suggested_addons_json"]) or []}
        if not addon_sku_ids or any(s not in suggested for s in addon_sku_ids):
            raise ApiError(422, "INVALID_ADDON_SELECTION", "addon not in suggestions")
        qty = merge_items([{"sku_id": i["sku_id"], "qty": i["qty"]} for i in snap["items"]]
                          + [{"sku_id": s, "qty": 1} for s in dict.fromkeys(addon_sku_ids)])
        q = compute_quote(conn, ident, qty, snap.get("requested_coupon_ids"), with_addons=False)
        return public_quote(persist_quote(conn, ident, q))
