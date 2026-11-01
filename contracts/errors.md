# 错误码（统一，禁止自造）

响应体统一：`{"error": {"code": "<CODE>", "message": "<人类可读>", "details": {...}}}`

| HTTP | code | 含义 |
|---|---|---|
| 400 | INVALID_REQUEST | 参数缺失或格式错误 |
| 401 | UNAUTHORIZED | JWT 缺失、签名错误或过期 |
| 401 | INTERNAL_AUTH_FAILED | 内部接口 `X-Internal-Token` 校验失败 |
| 403 | FORBIDDEN | 资源不属于当前 JWT 身份（含会话越权） |
| 404 | NOT_FOUND | 资源不存在（或对当前用户不可见，防枚举可用） |
| 409 | QUOTE_EXPIRED_OR_CHANGED | 报价过期或重新核价不一致；details 含最新 `quote` |
| 409 | COUPON_UNAVAILABLE | 券已被占用/过期/不适用 |
| 409 | INVALID_ORDER_STATE | 当前订单状态不允许该操作 |
| 409 | IDEMPOTENCY_KEY_CONFLICT | 同一幂等键对应了不同的 quote_id |
| 422 | INSUFFICIENT_STOCK | 真实库存不足（仅 `sellable_stock < qty` 时使用） |
| 422 | REFUND_EXCEEDS_LIMIT | 退款金额超过可退上限 |
| 422 | INVALID_ADDON_SELECTION | 勾选的凑单商品不在建议列表内 |
| 503 | CONCURRENT_VERSION_CONFLICT | 乐观锁重试 3 次仍冲突（严禁误报缺货） |
| 502 | UPSTREAM_ERROR | NLU/RAG/Ops/下游代理返回错误 |
| 504 | UPSTREAM_TIMEOUT | 下游超时 |

退款接口幂等重放：HTTP 200 + `idempotent_replayed=true`，不是错误。
对账查询 `NOT_FOUND`：HTTP 200 + `execution_status="NOT_FOUND"`（契约 4.2-3），不抛 404。
