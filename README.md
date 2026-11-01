# EcomPilot · 智购领航

电商智能体系统的架构规格、业务流程与数据模型文档。

包含架构规格（路线图与 14 张图表）以及项目三 EcomPilot（C 端交易与导购）的可运行实现。
实现深度分级与未完成项见 [交付报告](docs/交付报告.md)；EcomNLU / EcomRAG / EcomOps 为桩服务，不是真实实现。

## 运行

前置：Docker、`uv`。

```sh
uv venv --python 3.12 .venv && uv pip install -r requirements.txt --python .venv/bin/python
./scripts/start_all.sh      # MySQL(3307) + 3 桩 + 5 代理 + Pilot 主 API，首次自动建库导入 schema/seed
./scripts/smoke.sh          # 全链路冒烟（下单→支付→发货→售后→超时对账→退款）
./scripts/stop_all.sh       # 停服务（DOWN_DB=1 同时停 MySQL）
```

浏览器打开 http://127.0.0.1:8085/ 即聊天界面（启动脚本已设 `ECOMPILOT_DEV_TOKENS=1`，可在页面获取开发令牌）。`RESET_DB=1 ./scripts/start_all.sh` 重置库。

| 端口 | 服务 |
|---|---|
| 8085 | Pilot 主 API（订单/报价/售后/退款内部接口/聊天 SSE/前端） |
| 8011~8015 | env_fit / selection / pricing_order / aftersales / seeding_note 代理 |
| 8000 / 8001 / 8002 | Ops / NLU / RAG 桩 |
| 3307 | MySQL 8.0（docker） |

环境变量（前缀 `ECOMPILOT_`，示例见 `deploy/.env.example`）：`DB_HOST/PORT/USER/PASSWORD/NAME`、`JWT_SECRET`、`INTERNAL_TOKEN`、`NLU_URL/RAG_URL/OPS_URL`、`PILOT_URL`、`AGENT_*_URL`、`DEV_TOKENS`。

## 测试

```sh
.venv/bin/python -m pytest tests -q            # 全量：unit + acceptance + oracle + e2e（需 MySQL 已启动）
.venv/bin/python -m pytest tests/acceptance -q # AC-TX-01~04(C 端)、AC-SEC-01、契约一致性
.venv/bin/python scripts/hardening_check.py    # 对运行中的栈做故障注入（需先 start_all）
```

每个测试用独立库 `ecompilot_test_<uuid8>`，结束删除。契约在 `contracts/`（OpenAPI、schema、错误码、模块与代理接口）。

## 阅读入口

- [项目路线图与 Mermaid 源码](ecompilot_project_roadmap.md)：供开发 Agent 阅读的主要规格。
- [架构文档图片版](项目架构图/项目架构文档（图片版）.md)：适合人工阅读。
- [图表目录](项目架构图/)：中文命名的 PNG、SVG 与 Mermaid 源文件。

## 更新图表

需要 Node.js 22 或更新版本。

```sh
npm ci
npm run docs:sync
npm run docs:render
```

脚本读取仓库内路线图，不依赖下载目录、用户目录或系统工具缓存。新增图表时需同步维护图表清单。

默认使用 Puppeteer 管理的浏览器。需要指定本机浏览器时，自行创建 `项目架构图/浏览器配置.json` 并设置 Puppeteer 的 `executablePath`；此配置被 Git 忽略。

## 环境与服务边界

文档中的端口、内部 API 路径和服务名称是架构约定，不是已部署服务地址。后续开发应通过环境配置提供数据库连接、对象存储和各服务端点。

对象存储链接使用保留示例域名，仅作数据结构演示。本机路径、真实地址、凭据和环境覆盖配置不应提交到仓库。
