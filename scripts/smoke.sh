#!/usr/bin/env bash
# 全链路冒烟：token → 报价 → 下单 → 支付 → 发货 → 吐槽(候选) → 确认售后 → Ops 批准(含超时) → 对账 → 校验。
set -euo pipefail
cd "$(dirname "$0")/.."
P=http://127.0.0.1:8085; OPS=http://127.0.0.1:8000; TOK=${ECOMPILOT_INTERNAL_TOKEN:-dev-internal-token}
U=SMOKE_$(date +%s)
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }
JWT=$(curl -sf -XPOST $P/api/v1/dev/token -H 'content-type: application/json' -d "{\"user_id\":\"$U\"}" | j "d['token']")
A=(-H "Authorization: Bearer $JWT" -H 'content-type: application/json')
Q=$(curl -sf -XPOST $P/api/v1/quotes "${A[@]}" -d '{"items":[{"sku_id":"SKU_HEATER_003","qty":1}]}')
QID=$(echo "$Q" | j "d['quote_id']"); PAY=$(echo "$Q" | j "d['total_payable_cents']"); echo "quote $QID payable=$PAY"
O=$(curl -sf -XPOST $P/api/v1/orders/create "${A[@]}" -d "{\"idempotency_key\":\"$U\",\"quote_id\":\"$QID\"}")
OID=$(echo "$O" | j "d['order_id']"); ITEM=$(echo "$O" | j "d['items'][0]['order_item_id']"); echo "order $OID $ITEM"
curl -sf -XPOST $P/api/v1/orders/$OID/pay "${A[@]}" >/dev/null
curl -sf -XPOST $P/internal/v1/orders/$OID/fulfill -H "X-Internal-Token: $TOK" >/dev/null
C=$(curl -sf -XPOST $P/api/v1/aftersales/candidates "${A[@]}" -d '{"text":"刚买的热水器打不着火还漏水，怎么退货？"}')
echo "candidates: $(echo "$C" | j "[c['order_id'] for c in d['candidates']]")"
CL=$(curl -sf -XPOST $P/api/v1/aftersales/confirm "${A[@]}" -d "{\"order_id\":\"$OID\",\"order_item_id\":\"$ITEM\",\"buyer_reason_text\":\"漏水\"}")
CLM=$(echo "$CL" | j "d['claim_no']"); echo "claim $CLM"
curl -sf -XPOST $OPS/stub/admin/fault -H 'content-type: application/json' -d '{"mode":"refund_callback_timeout"}' >/dev/null
curl -s -o /dev/null -w "approve(timeout injected) http=%{http_code}\n" -XPOST $OPS/stub/admin/approve -H 'content-type: application/json' -d "{\"claim_no\":\"$CLM\",\"approved_refund_cents\":$PAY}"
curl -sf $OPS/stub/admin/pending > .run/pending.json
.venv/bin/python scripts/reconcile.py --base-url $P --token "$TOK" --items <(python3 -c "import json;print(json.dumps(json.load(open('.run/pending.json'))['items']))")
curl -sf -XPOST $OPS/stub/admin/fault -H 'content-type: application/json' -d '{"mode":"none"}' >/dev/null
S=$(curl -sf "$P/internal/v1/refunds/status?refund_idempotency_key=REFUND_${CLM}_V1" -H "X-Internal-Token: $TOK")
echo "$S"
[ "$(echo "$S" | j "d['execution_status']")" = SUCCESS ] && [ "$(echo "$S" | j "d['refunded_cents']")" = "$PAY" ]
[ "$(curl -sf $P/api/v1/orders/$OID "${A[@]}" | j "d['order_status']")" = REFUNDED_FULL ]
echo "SMOKE OK"
