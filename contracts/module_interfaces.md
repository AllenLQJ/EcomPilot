# 模块间接口（冻结）

## 1. 路由挂载
每个外围模块在 `pilot/<模块>/router.py` 暴露 `router: fastapi.APIRouter`（已含完整路径，如 `/internal/v1/refunds/execute`），
由统筹的 `pilot/main.py` 统一挂载。模块内部自行用 `pilot.core.db.get_engine()` 访问 MySQL（SQLAlchemy Core + `text()`），
身份用 `pilot.core.auth.current_identity`，内部接口用 `pilot.core.auth.require_internal`，错误用 `pilot.core.errors.ApiError`（错误码见 `errors.md`）。
- `pilot/refunds/router.py`：`/internal/v1/refunds/execute`、`/internal/v1/refunds/status`
- `pilot/aftersales/router.py`：`/api/v1/aftersales/candidates`、`/api/v1/aftersales/confirm`
- `pilot/orders/router.py`（统筹）：quotes / orders / pay / cancel / fulfill

时间一律 `datetime.now()` 本地无时区（库时区 +08:00）；ID 用 `前缀_` + 12 位随机十六进制，如 `RF_EXEC_ab12cd34ef56`、`CLM_20261101_ab12cd`。

## 2. bundle_math_tools（纯函数，无 IO）：`pilot/bundle/bundle_math_tools.py`

数据结构均为 dict：
- line: `{sku_id, shop_id, catalog_category_id, qty, unit_price_cents}`
- coupon: `{user_coupon_id, catalog_category_id | None, threshold_cents, discount_cents}`（None=全品类）
- promo_rule: `{rule_id, threshold_cents, discount_cents}`
- addon_candidate: `{sku_id, shop_id, catalog_category_id, unit_price_cents, sellable_stock, positive_review_ratio}`

### `price_lines(lines, coupons, promo_rules) -> dict`
返回 `{total_original_cents, total_discount_cents, total_payable_cents, promo_rule_id|None, promo_discount_cents, coupon_id|None, coupon_discount_cents, lines:[{sku_id, original_cents, discount_share_cents, payable_cents}], gap_to_next_tier_cents|None}`，`lines` 与入参 `lines` 同序同长。规则：
1. `original_i = qty*unit_price_cents`；总原价为各行之和。
2. 跨店满减：以**总原价**匹配 `threshold_cents <= total_original` 的**最高门槛**一档，优惠 = 该档 `discount_cents`；无则 0。
3. 品类券：从传入 `coupons` 中，筛选可用者——可用 = 其适用行（品类匹配，None 则全部行）的**原价小计** `>= threshold_cents`；在可用者中取 `discount_cents` 最大的 **1 张**，平局取 `user_coupon_id` 字典序最小；无可用则不用券。券优惠额 = `min(discount_cents, 适用行原价小计)`。
4. `total_discount = promo + coupon`，且不得超过总原价；`payable = original - discount`。
5. 分摊：满减额按各行 `original_i` 占比分摊到**所有行**；券优惠额按适用行 `original_i` 占比分摊到**适用行**。采用最大余数法（先取整除下界，余数按小数部分从大到小补 1 分，平局取行序小者），保证各行份额之和**精确等于**对应优惠额，且每行份额 ≤ 该行原价。`discount_share_i` 为两部分之和，`payable_i = original_i - discount_share_i`。
6. `gap_to_next_tier_cents`：满减档中门槛 `> total_original` 的**最小门槛** 减 `total_original`；无更高档则 None。

### `suggest_addons(lines, coupons, promo_rules, pool) -> dict | None`
1. 先 `price_lines` 得 `gap`；仅当 `0 < gap <= 4000` 才继续，否则返回 None。
2. `pool` 超过 15 件，或含 `sellable_stock < 1` 的，先剔除缺货件；剩余 >15 件只取 `positive_review_ratio` 降序（平局 `sku_id` 升序）前 15 件。
3. 穷举 1 件或 2 件**不同** SKU 的组合（每件数量 1，加到 `lines` 上；与必买行同 `sku_id` 的合并数量），用 `price_lines`（券可重新择优）算 `payable_with_addon`。
4. 仅保留 `payable_with_addon < payable_without_addon` 的组合；取 `payable_with_addon` 最小者，平局取件数少者，再平局取排序后 `sku_id` 元组字典序最小者。无则返回 None。
5. 返回 `{addons:[{sku_id, shop_id, catalog_category_id, unit_price_cents, qty:1}], payable_with_addon_cents, payable_without_addon_cents, saving_cents}`。

### MCP 工具层：`pilot/mcp_tools/`
- `bundle_tool.py`：`suggest_bundle(engine, tenant_id, lines, coupons) -> dict | None`，从库读 `promo_rules` 与 `products_sku WHERE is_addon_pool=1 AND tenant_id=...` 组装参数后调用 `suggest_addons`。
- `inventory_tool.py`：`get_stock(engine, sku_id) -> {sellable_stock, defective_stock, version} | None`（只读）。
- `image_fallback.py`：`resolve_image(engine, sku_id) -> {url, source}`，source ∈ `primary|gallery|category_default`，依次回退。

## 3. 退款对账：`pilot/refunds/reconcile.py`
`reconcile_items(items, base_url, internal_token, client=None) -> list[dict]`
- `items`：`[{refund_idempotency_key, execute_payload}]`，`execute_payload` 即 `/internal/v1/refunds/execute` 请求体。
- 对每项先 `GET {base_url}/internal/v1/refunds/status?refund_idempotency_key=`；`SUCCESS` → 记 `RECONCILED`；`NOT_FOUND` → `POST execute` 补发，成功记 `REPLAYED`；其他/异常记 `RETRY_LATER`。
- 返回 `[{refund_idempotency_key, outcome, execution_status}]`。`scripts/reconcile.py` 为 CLI 薄封装（`--base-url --token --items file.json`）。

## 4. 桩服务：`stubs/`
- `stubs/nlu_stub.py`（:8001）、`stubs/rag_stub.py`（:8002）、`stubs/ops_stub.py`（:8000），各暴露 `app`，契约见 `openapi/stubs.yaml`。
- NLU 桩确定性关键词规则：含「热水器」→ label 热水器；「平板」「洗发水」「牛奶/蒙牛」「酒店」同理；含「漏水/打不着火/坑/退货/退款」→ NEGATIVE 0.96；其余 POSITIVE 0.60；无关键词 → `ABSTAIN`。
- Ops 桩：`POST /internal/v1/claims/submit` 按 `claim_no` 幂等保存并返回 202；`/stub/admin/fault` 设置 `mode`；`/stub/admin/approve` 模拟批准——向 Pilot（地址取环境变量 `PILOT_BASE_URL`，令牌 `PILOT_INTERNAL_TOKEN`）调用 `refunds/execute`：`none` 正常；`refund_callback_timeout` 为请求发出并**已被 Pilot 执行**后丢弃响应、把项记入待对账列表（`GET /stub/admin/pending` 返回 `items` 数组，格式同 reconcile 入参）；`duplicate_callback` 连发两次相同请求。
