"""Independent oracle for bundle pricing / addon suggestion (contracts §2).

Deliberately avoids typical production styles (float ratios, early-exit search):
allocation uses ``fractions.Fraction`` + largest-remainder; addon search uses
full ``itertools.combinations`` with no pruning.
"""

from __future__ import annotations

import copy
import itertools
from fractions import Fraction
from typing import Any


def _line_original(line: dict[str, Any]) -> int:
    return int(line["qty"]) * int(line["unit_price_cents"])


def _allocate_largest_remainder(
    amount: int,
    weights: list[int],
    *,
    caps: list[int] | None = None,
) -> list[int]:
    """Hamilton / largest-remainder allocation with optional per-bucket caps.

    Ideal shares use exact ``Fraction``; take floors first, then assign leftover
    cents by fractional part descending and line index ascending (spec tie-break).

    When ``caps`` is set, buckets whose ideal meets remaining capacity are locked
    at the cap and the residual amount is re-solved among the rest, so
    ``sum(shares) == amount`` and ``0 <= share_i <= cap_i`` whenever
    ``amount <= sum(caps)``.
    """
    n = len(weights)
    shares = [0] * n
    if amount <= 0 or n == 0:
        return shares

    if caps is None:
        caps_eff = [max(0, int(w)) for w in weights]
    else:
        caps_eff = [max(0, int(c)) for c in caps]

    pending = int(amount)
    active = {i for i in range(n) if caps_eff[i] > 0 and weights[i] > 0}

    while pending > 0 and active:
        w_sum = sum(weights[i] for i in active)
        if w_sum <= 0:
            break

        ideals: dict[int, Fraction] = {
            i: Fraction(pending * weights[i], w_sum) for i in active
        }
        locked = [
            i
            for i in list(active)
            if ideals[i] >= Fraction(caps_eff[i] - shares[i])
        ]
        if locked:
            for i in locked:
                room = caps_eff[i] - shares[i]
                shares[i] += room
                pending -= room
                active.remove(i)
            continue

        floors = {i: int(ideals[i]) for i in active}
        for i, flo in floors.items():
            shares[i] += flo
            pending -= flo

        if pending == 0:
            break

        order = sorted(
            active,
            key=lambda i: (-(ideals[i] - floors[i]), i),
        )
        for i in order:
            if pending == 0:
                break
            if caps_eff[i] - shares[i] <= 0:
                continue
            shares[i] += 1
            pending -= 1
        break

    if pending != 0:
        for i in range(n):
            if pending == 0:
                break
            room = caps_eff[i] - shares[i]
            if room <= 0:
                continue
            take = min(room, pending)
            shares[i] += take
            pending -= take

    return shares


def _select_promo(
    total_original: int, promo_rules: list[dict[str, Any]]
) -> tuple[str | None, int]:
    eligible = [
        r for r in promo_rules if int(r["threshold_cents"]) <= total_original
    ]
    if not eligible:
        return None, 0
    best = min(
        eligible,
        key=lambda r: (
            -int(r["threshold_cents"]),
            -int(r["discount_cents"]),
            str(r["rule_id"]),
        ),
    )
    return str(best["rule_id"]), int(best["discount_cents"])


def _applicable_indices(
    lines: list[dict[str, Any]], coupon: dict[str, Any]
) -> list[int]:
    cat = coupon.get("catalog_category_id", None)
    if cat is None:
        return list(range(len(lines)))
    return [
        i
        for i, ln in enumerate(lines)
        if ln.get("catalog_category_id") == cat
    ]


def _select_coupon(
    lines: list[dict[str, Any]],
    originals: list[int],
    coupons: list[dict[str, Any]],
) -> tuple[str | None, int, list[int]]:
    usable: list[tuple[dict[str, Any], list[int], int]] = []
    for cp in coupons:
        idxs = _applicable_indices(lines, cp)
        subtotal = sum(originals[i] for i in idxs)
        if idxs and subtotal >= int(cp["threshold_cents"]):
            usable.append((cp, idxs, subtotal))
    if not usable:
        return None, 0, []
    best_cp, best_idxs, best_sub = min(
        usable,
        key=lambda t: (
            -int(t[0]["discount_cents"]),
            str(t[0]["user_coupon_id"]),
        ),
    )
    discount = min(int(best_cp["discount_cents"]), best_sub)
    return str(best_cp["user_coupon_id"]), discount, best_idxs


def _gap_to_next(total_original: int, promo_rules: list[dict[str, Any]]) -> int | None:
    higher = [
        int(r["threshold_cents"])
        for r in promo_rules
        if int(r["threshold_cents"]) > total_original
    ]
    if not higher:
        return None
    return min(higher) - total_original


def oracle_price(
    lines: list[dict[str, Any]],
    coupons: list[dict[str, Any]],
    promo_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reference ``price_lines`` per contracts/module_interfaces.md §2."""
    originals = [_line_original(ln) for ln in lines]
    total_original = sum(originals)

    promo_rule_id, promo_raw = _select_promo(total_original, promo_rules)
    coupon_id, coupon_raw, coupon_idxs = _select_coupon(lines, originals, coupons)

    # total_discount = promo + coupon, capped at total_original (trim coupon).
    promo_discount = min(promo_raw, total_original)
    coupon_discount = min(coupon_raw, max(0, total_original - promo_discount))

    promo_shares = _allocate_largest_remainder(
        promo_discount,
        list(originals),
        caps=list(originals),
    )

    rem_caps = [originals[i] - promo_shares[i] for i in range(len(lines))]
    if coupon_idxs:
        coupon_discount = min(
            coupon_discount, sum(rem_caps[i] for i in coupon_idxs)
        )

    coupon_shares = [0] * len(lines)
    if coupon_discount > 0 and coupon_idxs:
        weights = [0] * len(lines)
        caps = [0] * len(lines)
        for i in coupon_idxs:
            weights[i] = originals[i]
            caps[i] = rem_caps[i]
        coupon_shares = _allocate_largest_remainder(
            coupon_discount, weights, caps=caps
        )

    total_discount = promo_discount + coupon_discount
    out_lines = []
    for i, ln in enumerate(lines):
        share = promo_shares[i] + coupon_shares[i]
        out_lines.append(
            {
                "sku_id": ln["sku_id"],
                "original_cents": originals[i],
                "discount_share_cents": share,
                "payable_cents": originals[i] - share,
            }
        )

    return {
        "total_original_cents": total_original,
        "total_discount_cents": total_discount,
        "total_payable_cents": total_original - total_discount,
        "promo_rule_id": promo_rule_id,
        "promo_discount_cents": promo_discount,
        "coupon_id": coupon_id,
        "coupon_discount_cents": coupon_discount,
        "lines": out_lines,
        "gap_to_next_tier_cents": _gap_to_next(total_original, promo_rules),
    }


def _merge_addon_lines(
    lines: list[dict[str, Any]], addons: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    new_lines = copy.deepcopy(lines)
    by_sku = {ln["sku_id"]: i for i, ln in enumerate(new_lines)}
    for ad in addons:
        sid = ad["sku_id"]
        if sid in by_sku:
            new_lines[by_sku[sid]]["qty"] = int(new_lines[by_sku[sid]]["qty"]) + 1
        else:
            new_lines.append(
                {
                    "sku_id": sid,
                    "shop_id": ad["shop_id"],
                    "catalog_category_id": ad["catalog_category_id"],
                    "qty": 1,
                    "unit_price_cents": int(ad["unit_price_cents"]),
                }
            )
            by_sku[sid] = len(new_lines) - 1
    return new_lines


def _preprocess_pool(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    in_stock = [c for c in pool if int(c.get("sellable_stock", 0)) >= 1]
    ranked = sorted(
        in_stock,
        key=lambda c: (-float(c["positive_review_ratio"]), str(c["sku_id"])),
    )
    if len(ranked) > 15:
        ranked = ranked[:15]
    return ranked


def oracle_best_addon(
    lines: list[dict[str, Any]],
    coupons: list[dict[str, Any]],
    promo_rules: list[dict[str, Any]],
    pool: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Reference ``suggest_addons``: full 1–2 combination brute force, no prune."""
    base = oracle_price(lines, coupons, promo_rules)
    gap = base["gap_to_next_tier_cents"]
    if gap is None or not (0 < int(gap) <= 4000):
        return None

    payable_without = int(base["total_payable_cents"])
    candidates = _preprocess_pool(pool)

    best_key: tuple[int, int, tuple[str, ...]] | None = None
    best_items: list[dict[str, Any]] | None = None
    best_payable: int | None = None

    def consider(items: list[dict[str, Any]]) -> None:
        nonlocal best_key, best_items, best_payable
        merged = _merge_addon_lines(lines, items)
        payable_with = int(
            oracle_price(merged, coupons, promo_rules)["total_payable_cents"]
        )
        if payable_with >= payable_without:
            return
        sku_tuple = tuple(sorted(str(x["sku_id"]) for x in items))
        key = (payable_with, len(items), sku_tuple)
        if best_key is None or key < best_key:
            best_key = key
            best_items = list(items)
            best_payable = payable_with

    # Materialize every combination first so evaluation never short-circuits.
    single_idxs = list(itertools.combinations(range(len(candidates)), 1))
    pair_idxs = list(itertools.combinations(range(len(candidates)), 2))

    for (i,) in single_idxs:
        consider([candidates[i]])
    for i, j in pair_idxs:
        a, b = candidates[i], candidates[j]
        if a["sku_id"] == b["sku_id"]:
            continue
        consider([a, b])

    if best_items is None or best_payable is None:
        return None

    addons = [
        {
            "sku_id": x["sku_id"],
            "shop_id": x["shop_id"],
            "catalog_category_id": x["catalog_category_id"],
            "unit_price_cents": int(x["unit_price_cents"]),
            "qty": 1,
        }
        for x in sorted(best_items, key=lambda z: str(z["sku_id"]))
    ]
    return {
        "addons": addons,
        "payable_with_addon_cents": best_payable,
        "payable_without_addon_cents": payable_without,
        "saving_cents": payable_without - best_payable,
    }
