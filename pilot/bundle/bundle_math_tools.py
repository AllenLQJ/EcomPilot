"""Deterministic, integer-cent pricing and bounded exhaustive addon search."""
from itertools import combinations


def _allocate(amount, weights):
    total = sum(weights)
    if not total:
        return [0] * len(weights)
    shares = [amount * weight // total for weight in weights]
    order = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % total), i))
    for i in order[:amount - sum(shares)]:
        shares[i] += 1
    return shares


def price_lines(lines, coupons, promo_rules):
    originals = [line['qty'] * line['unit_price_cents'] for line in lines]
    total = sum(originals)
    eligible_rules = [rule for rule in promo_rules if rule['threshold_cents'] <= total]
    rule = max(eligible_rules, key=lambda r: r['threshold_cents'], default=None)
    promo = min(rule['discount_cents'], total) if rule else 0
    usable = []
    for coupon in coupons:
        weights = [value if coupon['catalog_category_id'] is None or
                   line['catalog_category_id'] == coupon['catalog_category_id'] else 0
                   for line, value in zip(lines, originals)]
        if sum(weights) >= coupon['threshold_cents']:
            usable.append((coupon, weights))
    selected = min(usable, key=lambda pair: (-pair[0]['discount_cents'], pair[0]['user_coupon_id']), default=None)
    promo_shares = _allocate(promo, originals)
    coupon_shares = [0] * len(lines)
    coupon_discount = 0
    if selected:
        coupon, weights = selected
        # Saturation protects both the total and each line when offers overlap.
        capacities = [value - share if weight else 0
                      for value, share, weight in zip(originals, promo_shares, weights)]
        coupon_discount = min(coupon['discount_cents'], sum(weights), sum(capacities))
        remaining = coupon_discount
        active_weights = weights[:]
        while remaining:
            proposed = _allocate(remaining, active_weights)
            saturated = [i for i in range(len(lines))
                         if proposed[i] > capacities[i] - coupon_shares[i]]
            if not saturated:
                coupon_shares = [a + b for a, b in zip(coupon_shares, proposed)]
                break
            for i in saturated:
                available = capacities[i] - coupon_shares[i]
                coupon_shares[i] += available
                remaining -= available
                active_weights[i] = 0
    discount = promo + coupon_discount
    higher = [r['threshold_cents'] for r in promo_rules if r['threshold_cents'] > total]
    return {
        'total_original_cents': total, 'total_discount_cents': discount,
        'total_payable_cents': total - discount,
        'promo_rule_id': rule['rule_id'] if rule else None,
        'promo_discount_cents': promo,
        'coupon_id': selected[0]['user_coupon_id'] if selected else None,
        'coupon_discount_cents': coupon_discount,
        'lines': [{'sku_id': line['sku_id'], 'original_cents': value,
                   'discount_share_cents': ps + cs, 'payable_cents': value - ps - cs}
                  for line, value, ps, cs in zip(lines, originals, promo_shares, coupon_shares)],
        'gap_to_next_tier_cents': min(higher) - total if higher else None,
    }


def suggest_addons(lines, coupons, promo_rules, pool):
    baseline = price_lines(lines, coupons, promo_rules)
    gap = baseline['gap_to_next_tier_cents']
    if gap is None or not 0 < gap <= 4000:
        return None
    candidates = sorted((p for p in pool if p['sellable_stock'] >= 1),
                        key=lambda p: (-p['positive_review_ratio'], p['sku_id']))[:15]
    best = None
    best_key = None
    for count in (1, 2):
        for combo in combinations(candidates, count):
            if len({p['sku_id'] for p in combo}) != count:
                continue
            combo = sorted(combo, key=lambda p: p['sku_id'])
            extended = [dict(line) for line in lines]
            addons = []
            for candidate in combo:
                addon = {key: candidate[key] for key in
                         ('sku_id', 'shop_id', 'catalog_category_id', 'unit_price_cents')}
                addon['qty'] = 1
                addons.append(addon)
                existing = next((line for line in extended if line['sku_id'] == addon['sku_id']), None)
                if existing is None:
                    extended.append(dict(addon))
                else:
                    existing['qty'] += 1
            payable = price_lines(extended, coupons, promo_rules)['total_payable_cents']
            if payable >= baseline['total_payable_cents']:
                continue
            key = (payable, count, tuple(p['sku_id'] for p in combo))
            if best_key is None or key < best_key:
                best_key = key
                best = {'addons': addons, 'payable_with_addon_cents': payable,
                        'payable_without_addon_cents': baseline['total_payable_cents'],
                        'saving_cents': baseline['total_payable_cents'] - payable}
    return best
