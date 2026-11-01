from copy import deepcopy
from decimal import Decimal

import pytest

from pilot.bundle.bundle_math_tools import price_lines, suggest_addons


def line(sku='a', price=9000, category='cat', shop='shop', qty=1):
    return dict(sku_id=sku, shop_id=shop, catalog_category_id=category,
                qty=qty, unit_price_cents=price)


def rule(threshold=10000, discount=2000, rule_id='r'):
    return dict(rule_id=rule_id, threshold_cents=threshold, discount_cents=discount)


def coupon(discount=800, threshold=5000, category='cat', coupon_id='c'):
    return dict(user_coupon_id=coupon_id, catalog_category_id=category,
                threshold_cents=threshold, discount_cents=discount)


def candidate(sku='x', price=1000, stock=1, ratio='0.950', **kwargs):
    result = line(sku, price, **kwargs)
    result.pop('qty')
    result.update(sellable_stock=stock, positive_review_ratio=Decimal(ratio))
    return result


def test_no_promo_and_exact_threshold():
    result = price_lines([line()], [], [rule()])
    assert result == dict(total_original_cents=9000, total_discount_cents=0,
                          total_payable_cents=9000, promo_rule_id=None, promo_discount_cents=0,
                          coupon_id=None, coupon_discount_cents=0,
                          lines=[dict(sku_id='a', original_cents=9000, discount_share_cents=0,
                                      payable_cents=9000)], gap_to_next_tier_cents=1000)
    exact = price_lines([line(price=10000)], [], [rule()])
    assert exact['total_payable_cents'] == 8000
    assert exact['gap_to_next_tier_cents'] is None
    assert price_lines([], [], [])['total_payable_cents'] == 0


@pytest.mark.parametrize('gap', [0, 1, 4000, 4001])
def test_gap_boundaries(gap):
    # Even the 4000-cent addon saves 1000 cents when the new tier saves 5000.
    result = suggest_addons([line(price=10000-gap)], [], [rule(discount=5000)],
                            [candidate(price=max(gap, 1))])
    if gap in (1, 4000):
        assert result['saving_cents'] == 5000-gap
    else:
        assert result is None


def test_stacked_coupon_and_promo_across_shops():
    lines = [line(price=6000, shop='one'), line('b', 4000, 'other', 'two')]
    result = price_lines(lines, [coupon()], [rule(discount=1000)])
    assert result['total_payable_cents'] == 8200
    assert [r['discount_share_cents'] for r in result['lines']] == [1400, 400]
    assert sum(r['discount_share_cents'] for r in result['lines']) == 1800
    assert sum(r['payable_cents'] for r in result['lines']) == 8200


def test_largest_remainders_and_line_order_ties():
    lines = [line('z', 1), line('a', 1), line('b', 1)]
    result = price_lines(lines, [coupon(1, 0)], [rule(1, 1)])
    assert [r['discount_share_cents'] for r in result['lines']] == [1, 1, 0]
    # Without saturation, both tied cents go to the first input line.
    result = price_lines([line('z', 10), line('a', 10), line('b', 10)],
                         [coupon(1, 0)], [rule(1, 1)])
    assert [r['discount_share_cents'] for r in result['lines']] == [2, 0, 0]
    result = price_lines([line('a', 1), line('b', 2), line('c', 4)], [], [rule(1, 3)])
    assert [r['discount_share_cents'] for r in result['lines']] == [0, 1, 2]


def test_coupon_threshold_face_value_and_id_ties():
    coupons = [coupon(1000, 9001), coupon(900, 1, 'missing'),
               coupon(800, coupon_id='z'), coupon(800, coupon_id='a')]
    result = price_lines([line()], coupons, [])
    assert result['coupon_id'] == 'a'
    assert result['total_payable_cents'] == 8200
    assert price_lines([line()], [coupon(1000, 9001)], [])['coupon_id'] is None
    assert price_lines([line(price=10)], [coupon(100, 0)], [])['coupon_discount_cents'] == 10


def test_highest_threshold_and_next_tier():
    result = price_lines([line(price=30000)], [], [rule(10000, 1000, 'low'),
                         rule(30000, 4000, 'mid'), rule(50000, 8000, 'high')])
    assert result['promo_rule_id'] == 'mid'
    assert result['gap_to_next_tier_cents'] == 20000


def test_reaching_tier_can_cost_more():
    assert suggest_addons([line()], [], [rule(discount=500)], [candidate()]) is None
    assert suggest_addons([line()], [], [rule(discount=1000)], [candidate()]) is None


def test_pool_truncation_and_stock_filter():
    # The winning cheap SKU ranks 16th among sellable candidates and is excluded.
    pool = [candidate(f'a{i:02}', 2000, ratio='0.990') for i in range(15)]
    pool += [candidate('cheap', 1000, ratio='0.900'), candidate('out', 1000, stock=0, ratio='1.000')]
    assert suggest_addons([line()], [], [rule(discount=1500)], pool) is None
    pool[0]['sellable_stock'] = 0
    result = suggest_addons([line()], [], [rule(discount=1500)], pool)
    assert [p['sku_id'] for p in result['addons']] == ['cheap']
    assert result['saving_cents'] == 500
    # Equal review ratios use SKU order at the cutoff.
    tied = [candidate(f'{i:02}', 2000) for i in range(15)] + [candidate('zz', 1000)]
    assert suggest_addons([line()], [], [rule(discount=1500)], tied) is None


def test_combinations_ties_merging_and_determinism():
    pool = [candidate('z', 1000), candidate('b', 500), candidate('a', 500)]
    expected = suggest_addons([line('main')], [], [rule()], pool)
    assert [p['sku_id'] for p in expected['addons']] == ['z']  # fewer items
    assert suggest_addons([line('main')], [], [rule()], list(reversed(pool))) == expected
    pair = suggest_addons([line('main')], [], [rule()], pool[1:])
    assert [p['sku_id'] for p in pair['addons']] == ['a', 'b']
    tied = suggest_addons([line('main')], [], [rule()], [candidate('z'), candidate('a')])
    assert tied['addons'][0]['sku_id'] == 'a'
    merged = suggest_addons([line(price=4500, qty=2)], [], [rule(discount=6000)],
                            [candidate('a', 1)])
    assert merged['payable_with_addon_cents'] == 7500
    assert merged['saving_cents'] == 1500


def test_coupon_reselected_and_input_unchanged():
    lines = [line()]
    coupons = [coupon(100, 0), coupon(3000, 10000, None, 'new')]
    rules = [rule(discount=500)]
    pool = [candidate()]
    before = deepcopy((lines, coupons, rules, pool))
    expected = suggest_addons(lines, coupons, rules, pool)
    assert expected['payable_without_addon_cents'] == 8900
    assert expected['payable_with_addon_cents'] == 6500
    for _ in range(3):
        assert suggest_addons(lines, coupons, rules, pool) == expected
    price_lines(lines, coupons, rules)
    assert (lines, coupons, rules, pool) == before
