# A2A 代理与对话层契约（W2 冻结）

## 1. 通用 A2A 协议
每个代理是独立 FastAPI 进程，模块级 `app`，启动命令 `python -m uvicorn pilot.agents.<模块>:app --port <端口>`。
- `GET /health` → `{"status":"ok","agent":"<name>"}`
- `GET /.well-known/agent.json` → `{"name","port","skills":[...]}`
- `POST /a2a/invoke`，请求 `{"trace_id": str, "input": {...}}`，成功 `200 {"status":"ok","output": {...}}`；业务失败 `200 {"status":"error","error":{"code","message"}}`（code 取自 `errors.md`）；入参不合法 `400`。
- 调下游代理用 `pilot/agents/a2a_client.py`：`invoke(url, input, trace_id, timeout=3.0) -> output`，超时抛 `A2AError("UPSTREAM_TIMEOUT")`，失败抛 `A2AError("UPSTREAM_ERROR")`。
- 代理地址环境变量（默认本机）：`ECOMPILOT_AGENT_ENV_FIT_URL=:8011`、`..._SELECTION_URL=:8012`、`..._PRICING_URL=:8013`、`..._AFTERSALES_URL=:8014`、`..._SEEDING_URL=:8015`；Pilot 主 API `ECOMPILOT_PILOT_URL=http://127.0.0.1:8085`。
- 需要用户身份的代理，入参带 `auth_token`（用户 JWT 原文），用它以 `Authorization: Bearer` 调 Pilot 公开 API；**绝不**自行信任入参里的 user_id/tenant_id。

| 模块 | 端口 | 模块路径 |
|---|---|---|
| env_fit | 8011 | `pilot.agents.env_fit_agent` |
| selection | 8012 | `pilot.agents.selection_agent` |
| pricing_order | 8013 | `pilot.agents.pricing_order_agent` |
| aftersales | 8014 | `pilot.agents.aftersales_agent` |
| seeding_note | 8015 | `pilot.agents.seeding_note_agent` |

## 2. 各代理 input / output
- **env_fit**：in `{city:str, days?:int}`；out `{city, temperature_c:int, humidity_pct:int, advice:{clothing:str, shampoo_spec:str}}`。固定数据表（杭州 18℃/75%、北京 8℃/30%、广州 26℃/80%、其它 20℃/50%），C 级。
- **selection**：in `{tenant_id, needs:[{text, category_hint?, budget_cents?}]}`；out `{candidates:[{need_index, sku_id, sku_name, shop_id, catalog_category_id, unit_price_cents, positive_review_ratio, sellable_stock, reviews:[str]}]}`。每个 need 先调 NLU（`ECOMPILOT_NLU_URL`，失败降级为 ABSTAIN）得类目/品牌提示（提示仅作加权，ABSTAIN 不做类目过滤），再从 `products_sku` 用关键词匹配 sku_name/品类名 + 好评率排序取前 3（有库存、非凑单池）。`reviews` 用固定模板文案（含 sku_name）。
- **pricing_order**：in `{auth_token, items:[{sku_id,qty}], coupon_ids?}`；out `{quote: <Quote schema>}`（直接调 Pilot `POST /api/v1/quotes`）；另支持 `{auth_token, quote_id, addon_sku_ids}` → 调 `confirm-addons` 返回新 quote。
- **aftersales**：in `{auth_token, action:"candidates"|"confirm", text?, category_hint?, order_id?, order_item_id?, buyer_reason_text?, evidence_urls?}`；out 为 Pilot 对应接口响应原样（`candidates` 或 `claim`），Pilot 的 4xx/5xx 转成 `status:"error"` 并保留 code。**candidates 阶段绝不产生写入；confirm 必须带用户确认的 order_id+order_item_id。**
- **seeding_note**：in `{auth_token, tenant_id, city, needs:[{text,...}]}`；流程：并行 A2A 调 env_fit、selection，再 A2A 调 pricing_order（用 selection 每个 need 的首选，qty=1）；out `{note:{title, body, images:[{sku_id,url,source}], quote_id, savings_formula:str|null, evidence:{city_advice, picks:[{sku_id, review}]}}}`。图片用 `pilot/mcp_tools/image_fallback.resolve_image`。任一下游失败：该部分降级（缺失字段置空并在 `note.degraded:[...]` 列出），仍返回 ok。`savings_formula` 在 quote 有 suggested_addons 时形如 `"加购 X 后实付 ¥a → ¥b，省 ¥c"`，否则 null。

## 3. 对话层 `pilot/chat/router.py`（暴露 `router`）
`POST /api/v1/chat/stream`，body `{session_id, message, confirmed_order_id?, confirmed_order_item_id?}`，返回 `text/event-stream`。
- 身份只取 JWT；会话归属表 `user_session_memory(tenant_id,user_id,session_id)`：session_id 若已被**其它用户**占用（按 `session_id` 查到不同 user/tenant）→ `403 FORBIDDEN`（在开始流之前返回普通 JSON 错误）；新会话插入；每轮把消息追加写回 messages_json。缓存键 `{tenant}:{user}:{session}`。
- SSE 事件：`event: token` data `{"text": "..."}`（逐段文本）；`event: card` data `{"type": ..., ...}`；`event: done` data `{"intent": ...}`；异常 `event: error` data `{"code","message"}` 后结束。
- 意图（关键词规则即可）：
  - `aftersales`（退/换/坏/漏/投诉/售后）：无已确认订单 → 经 aftersales 代理 candidates → card `{"type":"candidate_orders","candidates":[...]}` + 安抚文本，**不自动选单**；`confirmed_order_id`+`confirmed_order_item_id` 均有 → aftersales 代理 confirm → card `{"type":"claim_submitted","claim_no",...}`；candidates 为空 → 文本提示无可售后订单。
  - `shopping`（买/选/推荐/出差/住）：解析必买需求（按「，、和以及」切分）→ selection → pricing_order → card `{"type":"quote","quote":...}`，有 `suggested_addons` 再发 card `{"type":"addon_suggestion","quote_id","addons":[...],"saving_cents"}`（用户需勾选，不自动加入）。
  - `seeding`（种草/笔记/清单）：seeding_note 代理 → card `{"type":"seeding_note","note":...}`。
  - 其它：闲聊兜底文本，`intent:"chitchat"`。
- 下游代理不可用：发 `event: error`（code `UPSTREAM_ERROR`/`UPSTREAM_TIMEOUT`）并正常结束流，不抛 500。

## 4. 前端 `static/index.html`（C/B 级）
单文件，无构建；`GET /` 由 Pilot 主 API 提供（`pilot/main.py` 挂载 `static/`）。功能：顶部输入 user_id 获取/粘贴 JWT（开发用：页面提供 `POST /api/v1/dev/token` 调用——**仅当 `ECOMPILOT_DEV_TOKENS=1`**，由统筹实现）、聊天框（fetch + ReadableStream 解析 SSE）、候选订单卡片（含"确认此订单"按钮，点击后带 confirmed_order_id/item_id 再发一轮）、凑单建议卡（复选框 + 调 confirm-addons）、购物车抽屉（本地状态，C 级）。

## NLU `scene_type` 取值（补充）
Pilot 调用 `/api/v1/nlu/analyze` 时发送 `aftersales`（售后吐槽）或 `shopping`（导购）；桩的 `AFTER_SALE_FEEDBACK` 与 `aftersales` 等价。`PDF_BODY` / `CS_APOLOGY` 返回 `scene_not_applicable=true`。`priority_boost` 仅在售后场景且情感为 NEGATIVE 时为真。
