"""Oracle anchors + random differential checks vs pilot.bundle.bundle_math_tools."""

from __future__ import annotations

import json
import random
from typing import Any

from pilot.bundle.bundle_math_tools import price_lines, suggest_addons
from tests.oracle.oracle_bundle import oracle_best_addon, oracle_price

RNG_SEED = 20261101
N_RANDOM_CASES = 1000


def _assert_price_invariants(result: dict[str, Any]) -> None:
    lines = result["lines"]
    share_sum = sum(int(ln["discount_share_cents"]) for ln in lines)
    payable_sum = sum(int(ln["payable_cents"]) for ln in lines)
    assert share_sum == int(result["total_discount_cents"])
    assert payable_sum == int(result["total_payable_cents"])
    assert (
        int(result["total_original_cents"]) - int(result["total_discount_cents"])
        == int(result["total_payable_cents"])
    )
    for ln in lines:
        share = int(ln["discount_share_cents"])
        original = int(ln["original_cents"])
        assert share >= 0
        assert share <= original
        assert int(ln["payable_cents"]) == original - share


def _assert_price_equal(
    actual: dict[str, Any], expected: dict[str, Any], payload: dict[str, Any]
) -> None:
    try:
        assert actual["total_original_cents"] == expected["total_original_cents"]
        assert actual["total_discount_cents"] == expected["total_discount_cents"]
        assert actual["total_payable_cents"] == expected["total_payable_cents"]
        assert actual["promo_rule_id"] == expected["promo_rule_id"]
        assert actual["promo_discount_cents"] == expected["promo_discount_cents"]
        assert actual["coupon_id"] == expected["coupon_id"]
        assert actual["coupon_discount_cents"] == expected["coupon_discount_cents"]
        assert actual["gap_to_next_tier_cents"] == expected["gap_to_next_tier_cents"]
        assert len(actual["lines"]) == len(expected["lines"])
        for a, e in zip(actual["lines"], expected["lines"]):
            assert a["sku_id"] == e["sku_id"]
            assert a["original_cents"] == e["original_cents"]
            assert a["discount_share_cents"] == e["discount_share_cents"]
            assert a["payable_cents"] == e["payable_cents"]
    except AssertionError:
        raise AssertionError(
            "price_lines mismatch; input JSON for reproduction:\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        ) from None


def _assert_addon_equal(
    actual: dict[str, Any] | None,
    expected: dict[str, Any] | None,
    payload: dict[str, Any],
) -> None:
    try:
        if expected is None:
            assert actual is None
            return
        assert actual is not None
        assert set(a["sku_id"] for a in actual["addons"]) == set(
            a["sku_id"] for a in expected["addons"]
        )
        assert actual["payable_with_addon_cents"] == expected["payable_with_addon_cents"]
        assert (
            actual["payable_without_addon_cents"]
            == expected["payable_without_addon_cents"]
        )
        assert actual["saving_cents"] == expected["saving_cents"]
    except AssertionError:
        raise AssertionError(
            "suggest_addons mismatch; input JSON for reproduction:\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        ) from None


# ---------------------------------------------------------------------------
# Hand-calculated oracle anchors
# ---------------------------------------------------------------------------


def test_oracle_gap_boundary_none_and_zero_gate():
    """gap falsy / 0-gate: no higher tier → gap None → suggest None."""
    lines = [
        {
            "sku_id": "A",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "qty": 1,
            "unit_price_cents": 50000,
        }
    ]
    promo_rules = [
        {"rule_id": "P_TOP", "threshold_cents": 50000, "discount_cents": 5000},
    ]
    got = oracle_price(lines, [], promo_rules)
    assert got["gap_to_next_tier_cents"] is None
    assert got["promo_rule_id"] == "P_TOP"
    assert got["promo_discount_cents"] == 5000
    assert got["total_payable_cents"] == 45000
    # 0 < gap is false for None (and would be for 0)
    assert oracle_best_addon(lines, [], promo_rules, []) is None


def test_oracle_gap_boundary_1():
    lines = [
        {
            "sku_id": "A",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "qty": 1,
            "unit_price_cents": 10000,
        }
    ]
    promo_rules = [
        {"rule_id": "P1", "threshold_cents": 10000, "discount_cents": 1000},
        {"rule_id": "P2", "threshold_cents": 10001, "discount_cents": 5000},
    ]
    got = oracle_price(lines, [], promo_rules)
    assert got["gap_to_next_tier_cents"] == 1
    assert got["promo_discount_cents"] == 1000
    assert got["total_payable_cents"] == 9000

    pool = [
        {
            "sku_id": "ADD1",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "unit_price_cents": 1,
            "sellable_stock": 10,
            "positive_review_ratio": 0.99,
        }
    ]
    # +1 reaches P2: original 10001, promo 5000, payable 5001 < 9000
    sug = oracle_best_addon(lines, [], promo_rules, pool)
    assert sug is not None
    assert {a["sku_id"] for a in sug["addons"]} == {"ADD1"}
    assert sug["payable_without_addon_cents"] == 9000
    assert sug["payable_with_addon_cents"] == 5001
    assert sug["saving_cents"] == 9000 - 5001


def test_oracle_gap_boundary_4000():
    lines = [
        {
            "sku_id": "A",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "qty": 1,
            "unit_price_cents": 10000,
        }
    ]
    promo_rules = [
        {"rule_id": "P1", "threshold_cents": 10000, "discount_cents": 1000},
        {"rule_id": "P2", "threshold_cents": 14000, "discount_cents": 6000},
    ]
    got = oracle_price(lines, [], promo_rules)
    assert got["gap_to_next_tier_cents"] == 4000

    pool = [
        {
            "sku_id": "ADD_GAP",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "unit_price_cents": 4000,
            "sellable_stock": 5,
            "positive_review_ratio": 0.95,
        }
    ]
    # with addon: original 14000, promo 6000, payable 8000 < 9000
    sug = oracle_best_addon(lines, [], promo_rules, pool)
    assert sug is not None
    assert sug["payable_with_addon_cents"] == 8000
    assert sug["saving_cents"] == 1000


def test_oracle_gap_boundary_4001():
    lines = [
        {
            "sku_id": "A",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "qty": 1,
            "unit_price_cents": 10000,
        }
    ]
    promo_rules = [
        {"rule_id": "P1", "threshold_cents": 10000, "discount_cents": 1000},
        {"rule_id": "P2", "threshold_cents": 14001, "discount_cents": 8000},
    ]
    got = oracle_price(lines, [], promo_rules)
    assert got["gap_to_next_tier_cents"] == 4001
    pool = [
        {
            "sku_id": "ADD_X",
            "shop_id": "S1",
            "catalog_category_id": "CAT1",
            "unit_price_cents": 4001,
            "sellable_stock": 9,
            "positive_review_ratio": 0.99,
        }
    ]
    assert oracle_best_addon(lines, [], promo_rules, pool) is None


def test_oracle_remainder_tie_prefers_smaller_line_index():
    """Equal fractional parts → leftover cents go to smaller line index."""
    lines = [
        {
            "sku_id": "L0",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "qty": 1,
            "unit_price_cents": 100,
        },
        {
            "sku_id": "L1",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "qty": 1,
            "unit_price_cents": 100,
        },
        {
            "sku_id": "L2",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "qty": 1,
            "unit_price_cents": 100,
        },
    ]
    promo_rules = [
        {"rule_id": "P100", "threshold_cents": 300, "discount_cents": 100},
    ]
    # ideals 100/3; floors 33 each; rem 1 → index 0
    got = oracle_price(lines, [], promo_rules)
    assert got["promo_discount_cents"] == 100
    assert [ln["discount_share_cents"] for ln in got["lines"]] == [34, 33, 33]
    assert [ln["payable_cents"] for ln in got["lines"]] == [66, 67, 67]
    assert got["total_payable_cents"] == 200


def test_oracle_coupon_threshold_exactly_equals_subtotal():
    lines = [
        {
            "sku_id": "SHAMPOO",
            "shop_id": "S1",
            "catalog_category_id": "PERSONAL_SHAMPOO",
            "qty": 1,
            "unit_price_cents": 5000,
        },
        {
            "sku_id": "OTHER",
            "shop_id": "S2",
            "catalog_category_id": "OTHER",
            "qty": 1,
            "unit_price_cents": 3000,
        },
    ]
    coupons = [
        {
            "user_coupon_id": "CPN_EXACT",
            "catalog_category_id": "PERSONAL_SHAMPOO",
            "threshold_cents": 5000,
            "discount_cents": 800,
        }
    ]
    got = oracle_price(lines, coupons, [])
    assert got["coupon_id"] == "CPN_EXACT"
    assert got["coupon_discount_cents"] == 800
    assert got["total_original_cents"] == 8000
    assert got["total_discount_cents"] == 800
    assert got["total_payable_cents"] == 7200
    # coupon only on applicable line
    assert got["lines"][0]["discount_share_cents"] == 800
    assert got["lines"][1]["discount_share_cents"] == 0


def test_oracle_addon_more_expensive_returns_none():
    lines = [
        {
            "sku_id": "A",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "qty": 1,
            "unit_price_cents": 10000,
        }
    ]
    promo_rules = [
        {"rule_id": "P1", "threshold_cents": 10000, "discount_cents": 1000},
        {"rule_id": "P2", "threshold_cents": 13000, "discount_cents": 1100},
    ]
    # gap=3000; adding 3000 yields promo 1100, payable 11900 > 9000
    pool = [
        {
            "sku_id": "EXPENSIVE",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "unit_price_cents": 3000,
            "sellable_stock": 3,
            "positive_review_ratio": 0.9,
        }
    ]
    got = oracle_price(lines, [], promo_rules)
    assert got["gap_to_next_tier_cents"] == 3000
    assert got["total_payable_cents"] == 9000
    assert oracle_best_addon(lines, [], promo_rules, pool) is None


def test_oracle_pool_truncation_over_15():
    """Only top-15 by review desc / sku_id asc participate; rank-16 ignored."""
    lines = [
        {
            "sku_id": "MUST",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "qty": 1,
            "unit_price_cents": 10000,
        }
    ]
    promo_rules = [
        {"rule_id": "P1", "threshold_cents": 10000, "discount_cents": 500},
        {"rule_id": "P2", "threshold_cents": 12000, "discount_cents": 5000},
    ]
    # gap=2000. Cheap saver at low review would win if not truncated.
    pool: list[dict[str, Any]] = []
    for i in range(15):
        pool.append(
            {
                "sku_id": f"TOP_{i:02d}",
                "shop_id": "S1",
                "catalog_category_id": "C",
                "unit_price_cents": 50000,  # too expensive to help
                "sellable_stock": 10,
                "positive_review_ratio": 0.99 - i * 0.001,
            }
        )
    pool.append(
        {
            "sku_id": "CHEAP_BUT_RANK16",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "unit_price_cents": 2000,
            "sellable_stock": 10,
            "positive_review_ratio": 0.50,
        }
    )
    pool.append(
        {
            "sku_id": "OOS",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "unit_price_cents": 2000,
            "sellable_stock": 0,
            "positive_review_ratio": 1.0,
        }
    )
    # If CHEAP_BUT_RANK16 were kept: payable 10000+2000-5000=7000 < 9500.
    # Truncation → only expensive TOP_* → no improving combo → None.
    assert oracle_price(lines, [], promo_rules)["gap_to_next_tier_cents"] == 2000
    assert oracle_best_addon(lines, [], promo_rules, pool) is None

    # Same pool but boost cheap item into top-15 → suggestion appears.
    pool_in = [dict(x) for x in pool if x["sku_id"] != "OOS"]
    for item in pool_in:
        if item["sku_id"] == "CHEAP_BUT_RANK16":
            item["positive_review_ratio"] = 1.0
            item["sku_id"] = "CHEAP_IN"
    sug = oracle_best_addon(lines, [], promo_rules, pool_in)
    assert sug is not None
    assert {a["sku_id"] for a in sug["addons"]} == {"CHEAP_IN"}
    assert sug["payable_with_addon_cents"] == 7000


def test_oracle_coupon_tie_break_user_coupon_id():
    lines = [
        {
            "sku_id": "A",
            "shop_id": "S1",
            "catalog_category_id": "C",
            "qty": 1,
            "unit_price_cents": 10000,
        }
    ]
    coupons = [
        {
            "user_coupon_id": "CPN_B",
            "catalog_category_id": None,
            "threshold_cents": 1000,
            "discount_cents": 500,
        },
        {
            "user_coupon_id": "CPN_A",
            "catalog_category_id": None,
            "threshold_cents": 1000,
            "discount_cents": 500,
        },
    ]
    got = oracle_price(lines, coupons, [])
    assert got["coupon_id"] == "CPN_A"
    assert got["coupon_discount_cents"] == 500


# ---------------------------------------------------------------------------
# Random differential suite
# ---------------------------------------------------------------------------


def _rand_case(rng: random.Random) -> dict[str, Any]:
    n_shops = rng.randint(1, 3)
    shops = [f"SHOP_{i}" for i in range(n_shops)]
    n_cats = rng.randint(2, 4)
    cats = [f"CAT_{i}" for i in range(n_cats)]

    n_lines = rng.randint(1, 4)
    lines = []
    for i in range(n_lines):
        lines.append(
            {
                "sku_id": f"SKU_L{i}",
                "shop_id": rng.choice(shops),
                "catalog_category_id": rng.choice(cats),
                "qty": rng.randint(1, 3),
                "unit_price_cents": rng.randint(100, 300000),
            }
        )

    n_coupons = rng.randint(0, 3)
    coupons = []
    for i in range(n_coupons):
        is_all = rng.random() < 0.4
        threshold = rng.randint(100, 200000)
        coupons.append(
            {
                "user_coupon_id": f"CPN_{i}_{rng.randint(0, 9999):04d}",
                "catalog_category_id": None if is_all else rng.choice(cats),
                "threshold_cents": threshold,
                "discount_cents": rng.randint(1, max(1, threshold)),
            }
        )

    n_promos = rng.randint(1, 4)
    thresholds = sorted(rng.sample(range(500, 500000), n_promos))
    promo_rules = []
    for i, th in enumerate(thresholds):
        promo_rules.append(
            {
                "rule_id": f"PROMO_{i}_{th}",
                "threshold_cents": th,
                "discount_cents": rng.randint(1, th),
            }
        )

    n_pool = rng.randint(0, 20)
    pool = []
    for i in range(n_pool):
        ratio = round(rng.uniform(0.5, 1.0), 3)
        # Occasionally duplicate ratios to exercise sku_id tie-break.
        if i > 0 and rng.random() < 0.15:
            ratio = pool[rng.randint(0, i - 1)]["positive_review_ratio"]
        pool.append(
            {
                "sku_id": f"SKU_P{i:02d}",
                "shop_id": rng.choice(shops),
                "catalog_category_id": rng.choice(cats),
                "unit_price_cents": rng.randint(100, 50000),
                "sellable_stock": 0 if rng.random() < 0.15 else rng.randint(1, 20),
                "positive_review_ratio": ratio,
            }
        )

    return {
        "lines": lines,
        "coupons": coupons,
        "promo_rules": promo_rules,
        "pool": pool,
    }


def test_random_oracle_matches_production():
    rng = random.Random(RNG_SEED)
    for case_idx in range(N_RANDOM_CASES):
        payload = _rand_case(rng)
        lines = payload["lines"]
        coupons = payload["coupons"]
        promo_rules = payload["promo_rules"]
        pool = payload["pool"]

        expected_price = oracle_price(lines, coupons, promo_rules)
        actual_price = price_lines(lines, coupons, promo_rules)
        _assert_price_invariants(actual_price)
        _assert_price_equal(
            actual_price,
            expected_price,
            {**payload, "case_idx": case_idx, "seed": RNG_SEED},
        )

        expected_addon = oracle_best_addon(lines, coupons, promo_rules, pool)
        actual_addon = suggest_addons(lines, coupons, promo_rules, pool)
        _assert_addon_equal(
            actual_addon,
            expected_addon,
            {**payload, "case_idx": case_idx, "seed": RNG_SEED},
        )

