# Ecom 全栈智能体矩阵（四件套）可执行架构规格与全链路流程图手册（v2.1 完整图表版）

## 一、 架构修订原则与全局总览流程图

本手册基于对现有资产（`EcomNLU` 评论十分类与情感模型、`EcomRAG` 客服知识检索、`SmartVoyage` A2A+MCP 原型、`EduAgent` LangGraph+Vue3 原型）的严格代码审计与工程边界评估编制，内含**全局架构总览图、跨系统时序图、各子系统细分业务流程图、状态机图与数据实体 ER 图（共 13 张 Mermaid 图表）**。

### 1.1 核心工程准则（10 项边界收敛修正）

1. **模型能力与业务目录解耦**：`EcomNLU` 的 10 个固定标签（含品牌“蒙牛”、服务“酒店”等异构标签）保留为**评论实验模型标签**，不直接充当全站业务商品分类树；增加低置信度拒识（`ABSTAIN`）与分句候选，严禁对跨领域输入做强制硬过滤。
2. **明确“模拟支付的订单交易闭环”**：首版聚焦数据库级事务状态机、报价有效期核验、下单/取消/退款幂等键与优惠分摊计算，不虚构真实第三方资金清算。
3. **单一数据源（SSOT）与跨系统幂等回调**：废除模糊的“共享库”概念。C 端 `EcomPilot`（MySQL）独占订单与退款执行写权限；B 端 `EcomOps`（PostgreSQL）独占审核工单与审批流状态，跨系统通过带幂等键的内部 API + 异步对账通信。
4. **受控凑单优化替代宽泛“全局最优”承诺**：补齐 `shop_id` 与整型“分（`cents`）”计价，首版限定在“必买商品固定 + 限定候选凑单池（`<=15` 件）至多加购 2 件”的明确规则下通过穷举校核求解最低实付。
5. **认证鉴权与租户归属强校验**：严禁信任客户端传入的 `user_id` / `tenant_id`，统一从 JWT 凭据解析身份并校验资源归属；免订单号售后反查仅返回候选订单供用户确认，严禁自动选中退款。
6. **知识服务单一所有权与跨存储最终一致**：由 `EcomRAG` 唯一负责 `BGE-M3` 嵌入、Milvus 索引写入与混合检索；`EcomOps` 仅负责待补录问题审核流，通过 `待审核 → 发布中 → 已发布/发布失败` 状态机与确定性 `chunk_id` 实现 PG 与 Milvus 的幂等同步。
7. **按真实业务重绘 LangGraph 分支拓扑**：打破“节点数 1:1 僵化映射”，为售后审核增加风险条件路由（低风险自动放行、高风险才进入 `interrupt` 人工复核）及补材料、驳回、撤回、回调失败分支；清理客服训练中的原面试残留阶段名。
8. **合规审查采用“阻断项一票否决 + 待核实项转人工 + 建议项评分”**：废除用加权总分决定合规通过的做法；调用失败触发降级（`fallback_used=True`）时严禁以默认分放行。
9. **检查点持久化配合节点副作用幂等**：`AsyncPostgresSaver` 仅解决图状态保存；所有含外部写入的节点均须凭 `step_execution_id` 实现重入幂等，并配套超时挂起任务扫描恢复机制。
10. **“长期四项目矩阵 + 首版 MVP 单闭环先行”排期**：首版 MVP 集中打通 **「售后申请 → 条款检索 → 三轨审核 → 主管确认 → 模拟退款回写」** 核心闭环，导购凑单、种草、合规审查与客服训练按 Milestone 分批交付。

### 1.2 四项目矩阵定位总览

| 项目代号 | 仓库名与中文名 | 改造基座 | 端口 | 核心技术栈 | 角色定位 | 交付批次 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **项目一** | `EcomNLU`（智购感知） | 原 `ecommerce-text-classification` | `8001` | PyTorch / Transformers / FastAPI | 评论十类实验标签预测 + 正负向情感极性分析 + 低置信拒识（提供辅助信号） | **MVP (M1)** |
| **项目二** | `EcomRAG`（智购百科） | 原 `EcomRAG` + EduAgent 检索组件 | `8002` | FastAPI / BGE-M3 / CrossEncoder / Milvus `2.4.4` | **全系统唯一知识索引与检索服务**：管理带版本与生效期的售后规则、商品百科与 Milvus 向量库 | **MVP (M1)** |
| **项目三** | `EcomPilot`（智购领航） | `SmartVoyage` | `8085` (主API)<br>`8011~8015` (A2A) | FastAPI / A2A / MCP / MySQL `8.0` / SSE | **C 端交易与导购系统**：拥有商品目录、优惠券、订单状态机与模拟支付/退款执行权 | **MVP (M1 售后与订单内核)**<br>**M2/M3 (导购凑单与种草)** |
| **项目四** | `EcomOps`（智购中台） | `EduAgent` | `8000` (后端)<br>`3000` (Vue3) | FastAPI / LangGraph `1.0.9` / PostgreSQL `15` / Vue 3 | **B 端风控审批与运营实训平台**：拥有售后三轨审核与人工复核、知识补录审核、合规审查与客服对练工作流 | **MVP (M1 售后三轨审核)**<br>**M2/M4 (知识飞轮/合规/实训)** |

### 1.3 【图 1-1：全局总览】Ecom 四件套系统分层、SSOT 存储边界与跨系统调用全景图

```mermaid
flowchart TB
    subgraph C_Client["C 端消费者入口 (Web / SSE)"]
        CU["C 端买家"] --> CFE["EcomPilot 聊天与购物车界面 (static/index.html)"]
    end

    subgraph B_Client["B 端商家与风控运营入口 (Vue 3 :3000)"]
        BU["运营专员 / 售后主管 / 受训客服"] --> BFE["EcomOps 管理端 (Vue 3 + Pinia + Element Plus)"]
    end

    subgraph EcomPilot_Sys["项目三：EcomPilot 智购领航 (C 端交易与多智能体层 :8085)"]
        P_API["FastAPI 网关 + JWT 鉴权 (:8085)"]
        P_CHAT["chat_service.py 意图识别与任务调度"]
        P_TX["Order & Refund 交易执行服务 (SSOT)"]
        subgraph A2A_Group["A2A 领域代理群 (:8011~8015)"]
            A_ENV["env_fit_agent 环境与规格适配 (:8011)"]
            A_SEL["selection_agent 跨品类选品 (:8012)"]
            A_PRI["pricing_order_agent 受控凑单与锁单 (:8013)"]
            A_AFT["aftersales_agent 售后候选确认与建单 (:8014)"]
            A_SEED["seeding_note_agent 小红书种草二级代理 (:8015)"]
        end
        P_MCP["MCP 工具层 (库存乐观锁 / 凑单穷举校核 / 多图源回退)"]
        MYSQL[("MySQL 8.0 (C 端唯一写库)\n商品目录 / SKU库存 / 优惠券 / 订单 / 退款流水")]
    end

    subgraph EcomOps_Sys["项目四：EcomOps 智购中台 (B 端受控工作流层 :8000)"]
        O_API["FastAPI 网关 + RBAC 租户校验 (:8000)"]
        subgraph LG_Group["LangGraph 四大受控工作流"]
            G_CLM["claim_audit 售后三轨审核与主管 Interrupt 图"]
            G_QA["kb_qa 自适应问答与低分入队图"]
            G_LIS["listing_review 商品文案三层合规审查图"]
            G_TRN["cs_training 客服五阶段抗压对练图"]
        end
        PG_SAVER["AsyncPostgresSaver 持久化检查点"]
        OUTBOX["Outbox 异步回调与超时对账器"]
        PG[("PostgreSQL 15 (:5433 B 端唯一写库)\n审核工单 / 审批记录 / 待补录队列 / 合规报告")]
        MINIO[("MinIO 对象存储\n售后凭证图 / 商品详情 PDF")]
    end

    subgraph Shared_AI["底层共享感知与唯一知识底座"]
        NLU["项目一：EcomNLU 智购感知 (:8001)\n评论10类预测 + 正负情感 + <0.65 低置信拒识"]
        RAG["项目二：EcomRAG 智购百科 (:8002 唯一知识所有者)\nBGE-M3 稠密稀疏混合召回 + CrossEncoder 精排 + 幂等索引管理"]
        MILVUS[("Milvus 2.4.4 (:19531)\nknowledge_domain 知识向量库 + 商品口碑库")]
    end

    CFE -->|HTTP + JWT / SSE| P_API
    P_API --> P_CHAT
    P_API --> P_TX
    P_CHAT --> A_ENV & A_SEL & A_PRI & A_AFT & A_SEED
    A_SEED -.->|二级 A2A 协作调用| A_SEL & A_PRI & A_ENV
    A_SEL & A_PRI & A_AFT --> P_MCP
    P_MCP --> MYSQL
    P_TX --> MYSQL

    BFE -->|HTTP + JWT / POST SSE| O_API
    O_API --> G_CLM & G_QA & G_LIS & G_TRN
    G_CLM & G_QA & G_TRN --> PG_SAVER
    PG_SAVER --> PG
    G_CLM & G_QA & G_LIS & G_TRN --> PG
    G_LIS & G_CLM --> MINIO

    P_CHAT -->|分句类目提示 & 情绪辅助信号| NLU
    G_CLM -->|投诉文本情绪辅助信号| NLU
    A_AFT -->|A2A 检索售后规则| RAG
    G_CLM & G_QA & G_TRN -->|MCP / HTTP 检索条款| RAG
    G_QA -->|审核通过后调用幂等 Upsert 接口| RAG
    RAG --> MILVUS

    A_AFT ==>|1. POST /internal/v1/claims/submit 提交售后审核单| O_API
    G_CLM --> OUTBOX
    OUTBOX ==>|2. POST /internal/v1/refunds/execute 幂等退款回调 & 状态对账| P_TX
```

### 1.4 【图 1-2：MVP 闭环总览】C 端售后申请 → B 端三轨审核 → 主管审批 → 模拟退款回写与超时对账时序图

```mermaid
sequenceDiagram
    autonumber
    actor Buyer as C 端买家
    participant Pilot as EcomPilot (C 端 :8085)
    participant MySQL as MySQL (订单与退款 SSOT)
    participant NLU as EcomNLU (:8001)
    participant RAG as EcomRAG (:8002)
    participant Ops as EcomOps (B 端 :8000)
    participant PG as PostgreSQL (工单与检查点 SSOT)
    actor Sup as B 端售后主管 (Vue 3)

    Buyer->>Pilot: 吐槽：“刚买的热水器打不着火还漏水，怎么退货？” (携带 JWT)
    Pilot->>Pilot: 解析 JWT 获取可信 user_id 与 tenant_id
    Pilot->>NLU: POST /api/v1/nlu/analyze (售后主观短句)
    NLU-->>Pilot: 返回 label="热水器"(0.98), sentiment="NEGATIVE"(0.96)
    Pilot->>MySQL: 查询该 user_id 名下处于 FULFILLED 状态的候选订单 (按热水器类目优先排序)
    MySQL-->>Pilot: 返回候选订单列表 [ORD_88310]
    Pilot-->>Buyer: 展示候选订单卡片 + 初步安抚，请买家点击确认要申请售后的订单

    Buyer->>Pilot: 点击确认订单 ORD_88310 并上传漏水凭证提交退货退款
    Pilot->>MySQL: 校验归属与可退上限，更新订单状态为 AFTER_SALE_OPEN
    Pilot->>Ops: POST /internal/v1/claims/submit (携带 claim_no, order_id, max_refundable_cents, nlu_signal)
    Ops->>PG: 幂等写入 claim_tickets (状态 SUBMITTED)，启动 claim_audit LangGraph

    par 三轨并发审核 (run_three_tracks)
        Ops->>Ops: 1. 规则轨：核验签收 5 天 <= 15 天质量换退期 (Pass)
    and
        Ops->>RAG: 2. 条款轨：检索 HOME_APPLIANCE_HEATER 有效期内退换与运费规则
        RAG-->>Ops: 返回《热水器整机保修与漏水退换细则》[沙盒演示规则]
    and
        Ops->>Ops: 3. 算费与凭证轨：核算应退 112000 分，结合负向信号评估归因
    end

    Ops->>Ops: aggregate_and_route 判定金额 112000 分 > 5000 分阈值，需主管复核
    Ops->>PG: 触发 supervisor_review: interrupt()，状态持久化至 AsyncPostgresSaver
    Ops-->>Pilot: 返回 202 Accepted (status="PENDING_SUPERVISOR_REVIEW")

    Sup->>Ops: 在 Vue 3 审批页查看挂起工单，点击“批准全额退款 112000 分”
    Ops->>PG: Command(resume=APPROVE)，在同一 PG 事务写审批记录与 outbox_events (APPROVED_PENDING_CALLBACK)
    Ops->>Pilot: POST /internal/v1/refunds/execute (携带 refund_idempotency_key="REFUND_CLM_182_V1")

    alt 正常回调响应
        Pilot->>MySQL: 校验 refund_idempotency_key 唯一索引，写退款流水，更新订单为 REFUNDED_FULL (入次品仓)
        MySQL-->>Pilot: 事务提交成功
        Pilot-->>Ops: 200 OK (execution_status="SUCCESS")
        Ops->>PG: 更新工单为 COMPLETED，标记 outbox 已完成
    else 回调网络读取超时 (但 Pilot 实际已执行或未执行)
        Pilot--xOps: ReadTimeout 网络超时
        Ops->>PG: 工单置为 CALLBACK_RETRYING
        loop 后台对账协程轮询与幂等重发
            Ops->>Pilot: GET /internal/v1/refunds/status?refund_idempotency_key=REFUND_CLM_182_V1
            Pilot->>MySQL: 查询 refund_executions 表
            Pilot-->>Ops: 返回 status="SUCCESS" (或 NOT_FOUND 则触发幂等重发)
            Ops->>PG: 确认对账一致，更新工单为 COMPLETED
        end
    end
```

## 二、 规格一：系统责任边界与感知/安全细分流程图

### 2.1 跨系统单一数据源（SSOT）划分表

| 业务领域实体 | 唯一拥有者（SSOT） | 物理存储 | 允许写操作方 | 外部系统读取/触发方式 |
| :--- | :--- | :--- | :--- | :--- |
| **业务商品目录 (`catalog_categories`, `products_sku`)** | `EcomPilot` | MySQL | `EcomPilot` 交易/库存模块 | `EcomOps` 通过只读 API 查询商品快照 |
| **订单、库存锁、优惠券核销、退款执行流水 (`orders`, `refund_executions`)** | `EcomPilot` | MySQL | `EcomPilot` 交易服务 | `EcomOps` 审批通过后，调用 `EcomPilot` 内部幂等退款接口触发执行 |
| **售后审核工单、三轨评估结果、主管审批记录 (`claim_tickets`, `claim_approvals`)** | `EcomOps` | PostgreSQL | `EcomOps` 售后工作流与审批接口 | `EcomPilot` 通过 API 提交售后申请单并查询审核进度 |
| **知识文档、分块元数据、向量索引 (`knowledge_docs`, Milvus `knowledge_domain`)** | `EcomRAG` | Milvus + 本地存储 | `EcomRAG` 索引管理服务 | `EcomPilot` 与 `EcomOps` 均通过 HTTP/MCP 调用 `EcomRAG` 检索；`EcomOps` 审核通过的待补录知识调用 `EcomRAG` 发布接口入库 |
| **待补录问题审核队列 (`knowledge_pending_queue`)** | `EcomOps` | PostgreSQL | `EcomOps` 问答工作流与运营审核台 | 运营点击发布后，由 `EcomOps` 后台任务调用 `EcomRAG` 写入接口 |
| **商品文本合规审查报告、客服对练会话 (`listing_reviews`, `training_sessions`)** | `EcomOps` | PostgreSQL + MinIO | `EcomOps` 对应工作流 | 仅 B 端内部按 `tenant_id` 隔离访问 |

### 2.2 【图 2-1：细分流程】`EcomNLU` 分句推理、低置信拒识 (`ABSTAIN`) 与业务目录解耦流程图

针对原十分类模型基于评论数据训练的特点，通过**输入场景守卫 + 分句切分 + `0.65` 拒识阈值 + 软映射表**防止跨领域误杀：

```mermaid
flowchart TD
    IN["输入文本请求 + scene_type 场景标识"] --> GUARD{"检查 scene_type 是否属于适用域?\n(仅限: 买家评论 / 售后反馈 / 导购短句)"}
    GUARD -->|"否 (如: 商品PDF正文 / 客服道歉话术)"| REJ_SCENE["拒绝调用模型\n返回 scene_not_applicable=True"]
    GUARD -->|"是"| SPLIT["按标点与连词(和/以及/顺便/还要)切分为独立子句列表"]
    SPLIT --> INFER["PyTorch 批量推理:\n1. 10类实验标签 Softmax 概率\n2. 正负向情感 Softmax 概率"]
    INFER --> CHECK_CONF{"逐子句检查最高类别概率\nmax_prob >= 0.65 ?"}

    CHECK_CONF -->|"否 (如搜'电脑内胆包'或跨界商品)"| ABSTAIN["标记 predicted_label = 'ABSTAIN' (低置信拒识)\n不生成任何类目过滤条件"]
    CHECK_CONF -->|"是"| MAP["查询 nlu_label_to_catalog_map 解耦映射表"]

    MAP --> TYPE_SPLIT{"判断实验标签属性"}
    TYPE_SPLIT -->|"标准品类标签\n(书籍/平板/手机/水果/洗发水/热水器/衣服/计算机)"| HINT_CAT["输出 catalog_category_hint\n(仅作检索加权或候选池提示, 非硬过滤)"]
    TYPE_SPLIT -->|"品牌型标签\n(蒙牛)"| HINT_BRAND["输出 brand_hint='蒙牛'\n+ catalog_category_hint='DAIRY_MILK'"]
    TYPE_SPLIT -->|"服务型标签\n(酒店)"| HINT_SVC["输出 item_type='SERVICE'\n+ catalog_category_hint='TRAVEL_HOTEL'"]

    ABSTAIN & HINT_CAT & HINT_BRAND & HINT_SVC --> SENT_CHECK{"检查场景是否为售后反馈\n且 sentiment=='NEGATIVE' & score>=0.85?"}
    SENT_CHECK -->|"是"| BOOST["设置 priority_boost = True\n(仅作售后工单高优排序辅助信号, 不直接触发退款)"]
    SENT_CHECK -->|"否"| NORMAL["设置 priority_boost = False"]
    BOOST & NORMAL --> OUT["汇总输出 NLUAnalysisResponse\n(含各子句候选标签列表与拒识标记)"]
```

### 2.3 【图 2-2：细分流程】JWT 认证鉴权、多租户会话隔离与“免订单号反查候选确认”安全流程图

```mermaid
flowchart TD
    REQ["客户端请求 (Header: Authorization Bearer JWT, Body: session_id, message)"] --> JWT_VERIFY{"验证 JWT 签名与有效期"}
    JWT_VERIFY -->|"失败或过期"| ERR_401["返回 401 Unauthorized"]
    JWT_VERIFY -->|"成功"| EXTRACT["从 Token 载荷提取可信身份:\njwt_user_id, jwt_role, jwt_tenant_id\n(忽略客户端 Body 中伪造的 user_id/tenant_id)"]

    EXTRACT --> SESS_CHECK{"检查 session_id 是否存在?"}
    SESS_CHECK -->|"已存在"| OWNER_CHECK{"校验 user_session_memory 表:\nsession.user_id == jwt_user_id\nAND session.tenant_id == jwt_tenant_id?"}
    OWNER_CHECK -->|"不匹配 (越权访问)"| ERR_403["返回 403 Forbidden 并记录安全审计日志"]
    OWNER_CHECK -->|"匹配"| LOAD_MEM["按复合键加载会话上下文:\ncache_key = '{jwt_tenant_id}:{jwt_user_id}:{session_id}'"]
    SESS_CHECK -->|"新会话"| INIT_MEM["创建绑定 jwt_user_id 与 jwt_tenant_id 的新会话"]

    LOAD_MEM & INIT_MEM --> INTENT{"识别用户本轮意图"}
    INTENT -->|"售后退换/投诉诉求"| HAS_OID{"用户消息或上下文是否已包含\n经确认的 order_id?"}
    HAS_OID -->|"未提供 order_id (免单号反查)"| QUERY_CAND["查询 MySQL orders 表:\nWHERE user_id = :jwt_user_id\nAND status IN ('PAID_SIMULATED','FULFILLED')\n(若有 EcomNLU 识别标签则按匹配品类优先排序)"]
    QUERY_CAND --> CAND_COUNT{"是否存在可售后候选订单?"}
    CAND_COUNT -->|"0 笔"| NO_ORD["提示用户当前账号下无已支付/已履约订单"]
    CAND_COUNT -->|">= 1 笔"| ASK_CONFIRM["向前端返回候选订单选择卡片\n(严禁后台自动选中最近一单直接退赔!)"]
    ASK_CONFIRM --> WAIT_CLICK["等待用户点击确认具体 order_id 与 order_item_id"]
    HAS_OID -->|"已提供且用户已确认 order_id"| VERIFY_ORD{"校验该 order_id 归属:\norder.user_id == jwt_user_id?"}
    VERIFY_ORD -->|"否"| ERR_403
    VERIFY_ORD -->|"是"| SUBMIT_CLAIM["允许构建售后申请单提交至 EcomOps"]
```

## 三、 规格二：C 端 `EcomPilot` 与 B 端 `EcomOps` 细分业务流程图与状态机

### 3.1 【图 3-1：细分状态机】`EcomPilot` 订单全生命周期、库存锁与模拟支付/部分退款状态图

```mermaid
stateDiagram-v2
    [*] --> QUOTE_GENERATED : 凑单算价完成\n生成 quote_id (有效期15分钟)
    QUOTE_GENERATED --> CREATED : 提交下单请求\n(校验 idempotency_key 与报价未过期)
    QUOTE_GENERATED --> [*] : 报价超时失效

    CREATED --> CLOSED_OUT_OF_STOCK : sellable_stock < 购买数量\n(真实缺货)
    CREATED --> STOCK_LOCKED : 乐观锁扣减 sellable_stock 成功\n优惠券状态 UNUSED -> LOCKED

    STOCK_LOCKED --> CANCELLED : 买家主动取消 / 15分钟未支付超时\n(WHERE status='STOCK_LOCKED' 幂等加回可售库存并释放券)
    STOCK_LOCKED --> PAID_SIMULATED : 模拟支付网关确认成功\n优惠券状态 LOCKED -> USED

    PAID_SIMULATED --> REFUNDED_FULL : 未发货整单取消\n(模拟全额退款 + 加回 sellable_stock + 退回整单券)
    PAID_SIMULATED --> FULFILLED : 商家发货 / 酒店确认入住

    FULFILLED --> AFTER_SALE_OPEN : 买家确认订单子项并发起售后\n提交工单至 B 端 EcomOps
    AFTER_SALE_OPEN --> FULFILLED : B 端审核驳回 / 买家主动撤回售后

    AFTER_SALE_OPEN --> REFUNDING : 收到 EcomOps 批准回调\n(校验 refund_idempotency_key 唯一)
    REFUNDING --> REFUNDED_PARTIAL : 单品/部分退赔成功\n(按实付占比分摊优惠, 质量退货入次品仓 defective_stock)
    REFUNDING --> REFUNDED_FULL : 整单全额退赔成功\n(质量退货入 defective_stock, 不加回可售库存)
```

### 3.2 【图 3-2：细分流程】`EcomPilot` 报价二次核价、乐观锁防超卖（区分版本冲突与缺货）及幂等下单流程图

```mermaid
flowchart TD
    START(["POST /api/v1/orders/create\n(携带 JWT, idempotency_key, quote_id)"]) --> IDEM_CHECK{"查询 orders 表:\nidempotency_key 是否已存在?"}
    IDEM_CHECK -->|"已存在 (重复提交/网络重试)"| RET_EXISTING["直接返回已有订单快照 (200 OK, idempotent_replayed=True)\n绝不重复扣减库存"]
    IDEM_CHECK -->|"不存在"| QUOTE_CHECK{"查询 price_quotes 表校验 quote_id:\n1. 归属当前 jwt_user_id?\n2. now <= expires_at (15分钟内)?\n3. 重新核算实时商品价与券规则是否一致?"}

    QUOTE_CHECK -->|"报价过期或价格已变动"| ERR_QUOTE["返回 409 QUOTE_EXPIRED_OR_CHANGED\n附上最新重算报价请用户重新确认"]
    QUOTE_CHECK -->|"核价通过"| INIT_RETRY["初始化乐观锁重试计数器 retry_count = 0\n开启 MySQL 数据库事务 BEGIN"]

    INIT_RETRY --> READ_SKU["遍历订单内各 sku_id:\nSELECT sellable_stock, version FROM products_sku WHERE sku_id = :sku"]
    READ_SKU --> STOCK_ENOUGH{"检查当前可售库存:\nsellable_stock >= required_qty ?"}

    STOCK_ENOUGH -->|"否 (真实库存不足)"| ROLLBACK_OOS["ROLLBACK 事务\n写入失败记录并返回 422 INSUFFICIENT_STOCK\n(此时才允许引导 selection_agent 推荐同品类替代品)"]
    STOCK_ENOUGH -->|"是 (库存充足)"| OPT_UPDATE["执行带版本号的乐观锁更新:\nUPDATE products_sku\nSET sellable_stock = sellable_stock - :qty, version = version + 1\nWHERE sku_id = :sku AND version = :ver AND sellable_stock >= :qty"]

    OPT_UPDATE --> ROW_CHECK{"检查受影响行数 rowcount == 1 ?"}
    ROW_CHECK -->|"rowcount == 0 (并非缺货, 而是并发版本冲突!)"| RETRY_JUDGE{"ROLLBACK 当前尝试\n检查 retry_count < 3 ?"}
    RETRY_JUDGE -->|"是"| SLEEP_RETRY["retry_count += 1\n指数退避等待 (20ms / 50ms / 100ms)"] --> READ_SKU
    RETRY_JUDGE -->|"否 (重试3次仍冲突)"| ERR_CONFLICT["返回 503 CONCURRENT_VERSION_CONFLICT\n提示系统繁忙请稍后再试 (严禁误报为缺货!)"]

    ROW_CHECK -->|"所有 SKU 均扣减成功"| LOCK_COUPON["锁定用户优惠券:\nUPDATE user_coupons SET status='LOCKED', order_id=:oid\nWHERE user_coupon_id IN (:cids) AND status='UNUSED'"]
    LOCK_COUPON --> COUPON_OK{"优惠券锁定成功?"}
    COUPON_OK -->|"否 (券已被其他订单占用)"| ROLLBACK_CPN["ROLLBACK 事务\n返回 409 COUPON_UNAVAILABLE"]
    COUPON_OK -->|"是"| INSERT_ORD["写入 orders 与 order_items 表\n(状态 STOCK_LOCKED, 金额全部精确到分 cents)\nCOMMIT 提交事务"]
    INSERT_ORD --> END_OK(["返回 201 Created 订单详情与模拟支付入口"])
```

### 3.3 【图 3-3：细分流程】`EcomPilot` 跨品类选品、受控凑单算价（`<=15` 候选池穷举）与小红书两层 A2A 协作图

```mermaid
flowchart LR
    subgraph Main_Orchestrator["chat_service.py 核心调度器"]
        U_QUERY["用户跨品类采购需求\n(如: 去杭州出差住两晚+买平板+带洗发水和牛奶)"] --> PARSE["拆解必买子需求 + 提取预算上限"]
    end

    subgraph Tier1_Agents["第一层：A2A 领域执行代理并行协作"]
        PARSE -->|A2A| ENV["env_fit_agent (:8011)\n调天气工具查杭州气温湿度\n输出穿搭厚度与洗护规格建议"]
        PARSE -->|A2A| SEL["selection_agent (:8012)\n调 EcomNLU 获取类目/品牌提示\n从 Milvus + MySQL 召回必买候选 SKU"]
        SEL --> PRI["pricing_order_agent (:8013)\n调用 bundle_math_tools.py 受控凑单器"]
    end

    subgraph Controlled_Bundling["bundle_math_tools.py 受控凑单算法 (确定性穷举)"]
        B1["1. 按优惠前原价小计 (cents)\n匹配 1 张最优品类券 + 1 档跨店满减\n(按 shop_id 汇总参与满减金额)"] --> B2{"2. 距离下一档满减门槛差额\n0 < gap_cents <= 4000分(40元)?"}
        B2 -->|"否"| B_DIRECT["直接输出当前必买组合报价 quote_id"]
        B2 -->|"是"| B3["3. 从预设 <=15 件高好评日常凑单池中\n穷举挑选 1~2 件商品组合 (最多 C(15,1)+C(15,2)=120 种)"]
        B3 --> B4{"4. 检查加购后实付:\npayable_with_addon < payable_without_addon?"}
        B4 -->|"是 (凑单倒省钱)"| B_SUGGEST["生成必买报价单 + suggested_addons 凑单建议卡\n(必须经用户勾选确认才加入结算单)"]
        B4 -->|"否"| B_DIRECT
    end

    PRI --> B1

    subgraph Tier2_Seeding["第二层：seeding_note_agent 小红书种草代理 (:8015 二级 A2A 协作)"]
        SEED_IN["收到生成采购/开箱种草清单指令"] -->|二级 A2A 调用| ENV
        SEED_IN -->|二级 A2A 调用: 拉取商品参数与正向买家评论| SEL
        SEED_IN -->|二级 A2A 调用: 拉取凑单省钱公式明细| PRI
        SEED_IN --> IMG["调用 MCP 多图源回退工具\n(商品主图 -> 备选图库 -> 品类默认图)"]
        IMG --> NOTE_OUT["汇总生成带真实口碑引用的小红书多图种草卡片"]
    end

    B_DIRECT & B_SUGGEST --> SEED_IN
```

### 3.4 【图 3-4：细分流程】`EcomOps` 售后理赔三轨审核 (`claim_audit` LangGraph) 与 `interrupt/resume` 完整分支图

打破原 `Exam` 图无条件进入人工中断的线性限制，引入风险条件分流、四类主管决策、买家撤回与幂等回调对账：

```mermaid
flowchart TD
    START(["START: 收到售后申请单 claim_no\n写入 PG claim_tickets (SUBMITTED)"]) --> NODE_LOAD["load_order_and_policy 节点:\n1. 校验订单快照与可退上限 max_refundable_cents\n2. 调用 EcomRAG (:8002) 检索对应品类有效期内售后条款"]

    NODE_LOAD --> NODE_TRACKS["run_three_tracks 节点 (asyncio.gather 节点内三轨并发)"]

    subgraph Three_Tracks["三轨并发审核明细"]
        T1["轨道 1：时效与状态规则轨 (纯规则无 LLM)\n核验签收天数、是否支持7天无理由、可退余额上限"]
        T2["轨道 2：问题描述与凭证归因轨\n结合买家凭证、文本与 EcomNLU 辅助信号\n评估是否属实质量问题并记录 confidence / fallback_used"]
        T3["轨道 3：条款算费与优惠分摊轨\n按实付占比分摊公式核算建议退赔分值 suggested_refund_cents\n及退货入库策略 (可售仓 vs 次品仓)"]
    end

    NODE_TRACKS --> T1 & T2 & T3
    T1 & T2 & T3 --> NODE_AGG["aggregate_and_route 节点:\n汇总三轨结论与风险等级"]

    NODE_AGG --> COND_ROUTE{"条件路由边: route_after_audit"}

    COND_ROUTE -->|"硬性时效/规则不满足\n且无质量故障证据"| AUTO_REJ["auto_reject 节点:\n状态置为 AUTO_REJECTED\n通知 C 端买家驳回原因"] --> END_NODE(["END"])

    COND_ROUTE -->|"关键凭证缺失\n(如漏水/坏果未传清晰照片)"| NEED_EVI["request_more_evidence 节点:\n状态置为 NEED_MORE_EVIDENCE\n通知买家限时补充材料"] --> WAIT_BUYER{"买家补齐凭证或主动撤回?"}
    WAIT_BUYER -->|"买家补充凭证"| NODE_TRACKS
    WAIT_BUYER -->|"买家主动撤回 / 超时未补"| WITHDRAWN["状态置为 WITHDRAWN\n通知 EcomPilot 恢复订单为 FULFILLED"] --> END_NODE

    COND_ROUTE -->|"小额(<=5000分即50元) + 三轨全通过\n+ confidence>=0.85 + fallback_used==False"| AUTO_APP["auto_approve 节点:\n写入本地 PG 审批通过与 outbox_events\n生成唯一 refund_idempotency_key"]

    COND_ROUTE -->|"大额(>5000分) 或 规则冲突\n或 confidence<0.85 或 fallback_used==True"| NOTIFY_SUP["notify_supervisor 节点:\n标记高风险复核原因并推送待审列表"]
    NOTIFY_SUP --> INT_NODE["supervisor_review 节点:\n调用 LangGraph interrupt() 挂起工作流!\n状态快照持久化至 AsyncPostgresSaver (PostgreSQL)"]

    INT_NODE -.->|"主管在 Vue 3 审批页操作\nPOST /api/v1/claims/{no}/confirm\n触发 Command(resume=...)"| RESUME_DEC{"判断主管恢复指令 action"}

    RESUME_DEC -->|"REJECT (驳回申请)"| SUP_REJ["apply_supervisor_decision:\n状态置为 SUPERVISOR_REJECTED\n同步结果至 EcomPilot"] --> END_NODE
    RESUME_DEC -->|"REQUEST_EVIDENCE (退回补材料)"| NEED_EVI
    RESUME_DEC -->|"APPROVE / MODIFY_AMOUNT\n(强制校验修改金额 <= max_refundable_cents)"| SUP_APP["apply_supervisor_decision:\n同一 PG 事务写入审批结论与 outbox_events\n状态置为 APPROVED_PENDING_CALLBACK"]

    AUTO_APP & SUP_APP --> EXEC_CB["execute_refund_callback 节点:\n先检查 step_execution_id 防止节点重入重复调用\n调用 EcomPilot POST /internal/v1/refunds/execute"]

    EXEC_CB --> CB_RESULT{"回调响应结果?"}
    CB_RESULT -->|"200 OK (含幂等重放成功)"| DONE_OK["更新工单状态为 COMPLETED\n标记 outbox_events 已完成"] --> END_NODE
    CB_RESULT -->|"网络超时 / 5xx 异常"| CB_RETRY["状态置为 CALLBACK_RETRYING\n交由后台 Outbox 对账协程轮询 EcomPilot 退款状态直至闭环"] --> END_NODE
```

### 3.5 【图 3-5：细分流程】`EcomOps` ⇄ `EcomPilot` 跨库异步回调与网络超时对账恢复流程图

专门回答“主管已批准，但回写 C 端时网络超时，系统怎么判断退款到底执行了没有”这一核心分布式一致性问题：

```mermaid
flowchart TD
    A["B 端主管点击批准 / 自动审批通过"] --> B["EcomOps 开启本地 PostgreSQL 事务:\n1. 更新 claim_tickets 状态为 APPROVED_PENDING_CALLBACK\n2. 插入 outbox_events 记录 (携带唯一键 refund_idempotency_key = 'REFUND_' + claim_no + '_V1')\n3. 提交 PG 事务"]
    B --> C["调用 EcomPilot 内部接口:\nPOST /internal/v1/refunds/execute\nHeader/Body 携带 refund_idempotency_key"]

    C --> D{"EcomPilot (MySQL) 收到请求?"}
    D -->|"请求未到达 (连接断开)"| E["EcomOps 捕获 ConnectError / Timeout\n保持 outbox 状态为 PENDING_RETRY"]
    D -->|"请求已到达 EcomPilot"| F["EcomPilot 开启 MySQL 事务:\n查询 refund_executions 表 WHERE idempotency_key = :key"]

    F --> G{"该 idempotency_key 是否已存在?"}
    G -->|"已存在且 status='SUCCESS'"| H["直接返回已有执行结果 (200 OK, idempotent_replayed=True)\n绝不重复退款!"]
    G -->|"不存在 (首次执行)"| I["1. 插入 refund_executions (唯一索引 idempotency_key)\n2. 按分摊规则更新 orders 状态为 REFUNDED_FULL / PARTIAL\n3. 质量退货增加 defective_stock\n4. 提交 MySQL 事务"]

    I --> J{"响应返回给 EcomOps 时网络是否超时?"}
    J -->|"正常返回 200 OK"| K["EcomOps 收到成功响应:\n更新 PG claim_tickets 为 COMPLETED\n更新 outbox_events 为 DONE"]
    J -->|"MySQL 已提交, 但响应中途断网超时!"| E

    E --> L["EcomOps 后台 Outbox 对账协程 (每 30 秒扫描 PENDING_RETRY 记录)"]
    L --> M["先调用 EcomPilot 对账查询接口:\nGET /internal/v1/refunds/status?refund_idempotency_key=:key"]
    M --> N{"查询到的真实退款执行状态?"}
    N -->|"status == 'SUCCESS'\n(说明上次超时前 MySQL 其实已经退成功了!)"| K
    N -->|"status == 'NOT_FOUND'\n(说明上次请求根本没落库)"| C
    N -->|"超过最大重试次数 (如 10 次) 仍失败"| O["更新 PG 状态为 CALLBACK_FAILED_MANUAL\n触发运营告警台人工介入"]
```

### 3.6 【图 3-6：细分流程】`EcomOps` (`kb_qa` LangGraph) ⇄ `EcomRAG` 多策略混合检索与知识补录幂等发布飞轮图

明确 **`EcomRAG (:8002)` 是全系统唯一的知识向量化与 Milvus 索引所有者**，`EcomOps` 通过确定性 `chunk_id` 与四态状态机实现跨 PG-Milvus 幂等发布：

```mermaid
flowchart TD
    subgraph Online_QA["在线问答流：EcomOps kb_qa LangGraph 调用 EcomRAG"]
        Q_IN["用户/客服输入问题 query + tenant_id + catalog_category_id"] --> N_CLS["classify_query 节点:\n规则 + 电商术语 + 微调分类器判定问题类型"]
        N_CLS -->|"GENERAL (日常闲聊/通用问题)"| GEN_DIRECT["generate_general 节点"]
        N_CLS -->|"PRECISE (精确条款, top_k=8)"| CALL_RAG["调用 EcomRAG (:8002) 检索接口"]
        N_CLS -->|"VAGUE (模糊表达)"| HYDE["hyde_generate 生成假设性售后条款文档 (top_k=10)"] --> CALL_RAG
        N_CLS -->|"BROAD (宽泛多问)"| MULTI["multi_query_rewrite 拆解至多3路子查询并发 (各召回4条)"] --> CALL_RAG

        subgraph EcomRAG_Engine["EcomRAG (:8002) 唯一混合检索与精排引擎"]
            CALL_RAG --> BGE["BGE-M3 编码 1024维 Dense + Lexical Sparse"]
            BGE --> MV_SEARCH["Milvus knowledge_domain 两路召回:\n标量过滤: tenant_id & catalog_category_id & effective_from<=now<=effective_to\nWeightedRanker(0.7, 0.3) 粗排融合"]
            MV_SEARCH --> RERANK["CrossEncoder (bge-reranker-v2-m3) 配对精排\n输出 Top-3 片段及最高精排分数 top1_score"]
        end

        RERANK --> SCORE_CHECK{"top1_score >= 0.75 ?"}
        SCORE_CHECK -->|"是 (高置信命中)"| GEN_RAG["generate_rag 节点:\n流式生成回答并附带 [rule_source / 版本 / 沙盒演示标记]"]
        SCORE_CHECK -->|"否 (<0.75 低置信) 且开启联网"| WEB_MCP["web_search 节点:\n调用 Web Search MCP (Tavily -> DuckDuckGo 回退)"] --> GEN_LOW["generate_direct 低分兜底回答"]
        SCORE_CHECK -->|"否 (<0.75 低置信) 且未开启联网"| GEN_LOW
        GEN_LOW --> ENQ["enqueue_pending 节点:\n将问题写入 PG knowledge_pending_queue\n初始状态: PENDING_REVIEW"]
        GEN_RAG & GEN_DIRECT & ENQ --> SAVE_MEM["save_memory 节点 (已修复缩进与计数 Bug):\n1. 每轮必定 UPSERT 更新 qa_sessions\n2. 合入 AI 回复后满偶数轮 (20条消息) 触发摘要"]
    end

    subgraph Offline_Flywheel["知识补录飞轮：PG 状态机 + EcomRAG 幂等 Upsert (跨存储最终一致)"]
        ENQ -.-> OP_UI["运营在 Vue 3『知识待补充页』查看 PENDING_REVIEW 记录"]
        OP_UI --> OP_ACT{"运营审核操作"}
        OP_ACT -->|"判定为无效/重复提问"| DISCARD["更新 PG 状态为 DISCARDED"]
        OP_ACT -->|"填写标准解答、适用类目、规则来源、生效期并点击发布"| TX_PUB["1. 开启 PG 事务:\n保存答案草稿，递增 version\n计算确定性主键 chunk_id = sha256(queue_id + ':' + version)\n状态置为 PUBLISHING 并提交 PG"]
        TX_PUB --> CALL_UPSERT["2. 调用 EcomRAG 内部发布接口:\nPOST /internal/v1/kb/upsert (携带确定性 chunk_id)"]
        CALL_UPSERT --> RAG_WRITE["EcomRAG 调用 BGE-M3 生成向量\n按主键 chunk_id 执行 Milvus Upsert (天然幂等覆盖)"]
        RAG_WRITE --> WRITE_OK{"Milvus Upsert 是否成功?"}
        WRITE_OK -->|"成功返回 milvus_pk"| PG_OK["3. 回写 PG 状态为 PUBLISHED\nC 端与 B 端检索立即同步生效!"]
        WRITE_OK -->|"超时或异常"| PG_FAIL["3. 回写 PG 状态为 PUBLISH_FAILED (记录错误原因)\n支持运营一键重试或定时重试 (相同 chunk_id 绝不产生重复向量)"]
        PG_FAIL -.->|"重试发布"| CALL_UPSERT
    end
```

### 3.7 【图 3-7：细分流程】`EcomOps` 商品详情页文案合规审查 (`listing_compliance_review`) 三层结构化判定流程图

废除“加权总分抵消违规项”的做法，明确三层结构化输出与降级强制转人工规则：

```mermaid
flowchart LR
    UP(["运营上传商品详情页 PDF + 申报上架类目 declared_category"]) --> MINIO_UP["upload_to_minio 节点:\n真实写入 MinIO ecomops-docs 桶\n记录 pdf_minio_path"]
    MINIO_UP --> MINIO_DL["download_pdf 节点:\n读取 PDF 字节流"]
    MINIO_DL --> PYMUPDF["extract_text 节点:\nPyMuPDF 提取文本并处理双栏阅读顺序\n(仅作文本级审查, 不承诺视觉首屏排版美学打分)"]
    PYMUPDF --> STRUCT["extract_structured 节点:\n抽取商品标题、核心参数表、功效宣称、证书编号列表"]

    STRUCT --> PARALLEL["run_compliance_checks 节点 (asyncio.gather 并发执行专项检查)"]

    subgraph Checks["专项合规与质量检查器"]
        C1["检查器 1：广告法违禁极限词扫描\n(规则词库 + LLM 上下文复核)"]
        C2["检查器 2：申报类目必填参数完整度核查\n(对照 declared_category 必填属性表)"]
        C3["检查器 3：医疗/虚假功效宣称排查\n(如洗发水防脱生发、水果治病等)"]
        C4["检查器 4：资质与检测报告编号提取\n(3C编号/化妆品备案号/质检报告号)"]
        C5["检查器 5：卖点可读性与文案优化建议"]
    end

    PARALLEL --> C1 & C2 & C3 & C4 & C5
    C1 & C2 & C3 & C4 & C5 --> DIAG["diagnose_and_classify 节点:\n汇总各检查器结果与异常降级标志 fallback_used"]

    DIAG --> LAYER_JUDGE{"三层合规门禁判定逻辑\n(严禁用加权总分抵消违规项!)"}

    LAYER_JUDGE -->|"存在任意 1 项阻断性违规\n(极限词 / 违规功效 / 关键必填参数缺失)"| OUT_REJECT["结论: REJECTED_BLOCKING (一票否决)\n输出第一层: blocking_violations 强制整改清单"]

    LAYER_JUDGE -->|"无阻断项, 但存在待核实证书编号\n或任一检查器调用失败 (fallback_used==True)"| OUT_MANUAL["结论: PENDING_MANUAL_VERIFY (转人工核验)\n输出第二层: manual_verify_items (严禁拿默认分直接当通过!)"]

    LAYER_JUDGE -->|"无阻断项 且 无待人工核实/降级项"| OUT_PASS["结论: PASSED_WITH_SUGGESTIONS (审查通过)\n输出第三层: advisory_suggestions 文案优化建议"]

    OUT_REJECT & OUT_MANUAL & OUT_PASS --> SAVE_PG["save_results 节点:\n持久化三层结构化报告至 PostgreSQL listing_reviews 表"]
    SAVE_PG -.->|"可选联动: 提取该商品高频争议点"| SEED_GEN["生成客服演练场景种子卡 training_scenario_seed\n(供客服进入 cs_training 开展多轮人工对练)"]
```

### 3.8 【图 3-8：细分流程】`EcomOps` 金牌电商客服五阶段抗压对练 (`cs_training`) 与记忆摘要修复流程图

```mermaid
flowchart TD
    START(["客服发送一轮回复 POST /api/v1/training/sessions/{id}/chat/stream"]) --> LOAD["load_context 节点:\n从 AsyncPostgresSaver 加载多轮状态\n读取关联的 training_scenario_seed (商品痛点) 与历史摘要"]

    LOAD --> STAGE_CHK{"check_stage 节点:\n检查当前所处客诉实训阶段"}

    STAGE_CHK -->|"阶段 1: verify_order (核实身份与订单)\n阶段 2: empathy_listen (情绪安抚与倾听)\n阶段 3: policy_explain (基于 EcomRAG 解释规则)\n阶段 4: solution_negotiate (退换/阶梯补偿协商)"| EVAL["evaluate_answer 节点:\n1. 核对客服引用的售后规则是否符合 EcomRAG 条款\n2. 检查沟通礼貌度与安抚有效性 (不误杀'抱歉'等礼貌词)\n3. 判断当前阶段目标是否达成 (同一争议点最多追问 2 次)"]

    EVAL --> NEXT_DEC{"是否推进至下一阶段?"}
    NEXT_DEC -->|"未达成且追问 < 2 次"| KEEP_STAGE["保持当前阶段, 追问计数器 follow_up_count += 1"]
    NEXT_DEC -->|"已达成 或 追问已满 2 次"| ADV_STAGE["推进 stage 至下一阶段 (若第4阶段完成则置为 finished)"]

    KEEP_STAGE & ADV_STAGE --> IS_FIN_NOW{"推进后 stage == 'finished' ?"}
    IS_FIN_NOW -->|"否 (继续扮演挑剔买家对话)"| GEN_RESP["generate_response 节点:\n显式启用 get_llm('interview', streaming=True)\n按当前阶段扮演挑剔买家发起质询并逐 token 流式推送"]

    STAGE_CHK -->|"stage == 'finished'"| GEN_REP["generate_report 节点:\n汇总整场对练的规则准确度、情绪安抚力、方案合规度评分报告"]
    IS_FIN_NOW -->|"是"| GEN_REP
    GEN_REP --> SAVE_REP["save_report 节点:\n写入 PostgreSQL training_sessions 表"]

    GEN_RESP & SAVE_REP --> SAVE_MEM["save_memory 节点 (已修复原项目两大记忆缺陷):\n1. 先将本轮 AI 回复追加进完整 messages 列表\n2. 判断 len(messages) % (threshold * 2) == 0 (偶数轮精准触发摘要)\n3. 无论是否触发摘要, 每轮均执行数据库 UPSERT 保存最新状态"] --> END_TURN(["本轮 SSE 流结束, 等待客服下一轮输入"])
```

## 四、 规格三：跨系统数据库实体关系（ER 图）与核心接口契约

### 4.1 【图 4-1：数据模型总览】`EcomPilot` (MySQL) 与 `EcomOps` (PostgreSQL) 跨系统实体关系与边界图

```mermaid
erDiagram
    CATALOG_CATEGORIES ||--o{ PRODUCTS_SKU : "1:N 业务类目包含SKU"
    NLU_LABEL_TO_CATALOG_MAP }o--|| CATALOG_CATEGORIES : "软映射提示"
    PRODUCTS_SKU ||--o{ ORDER_ITEMS : "被购买"
    PRICE_QUOTES ||--o| ORDERS : "15分钟核价转化"
    ORDERS ||--|{ ORDER_ITEMS : "1:N 包含子项"
    ORDERS ||--o{ USER_COUPONS : "锁定/核销优惠券"
    ORDERS ||--o{ REFUND_EXECUTIONS : "1:N 幂等退款执行流水"

    CLAIM_TICKETS ||--o{ CLAIM_TRACK_RESULTS : "1:3 三轨审核明细"
    CLAIM_TICKETS ||--o{ CLAIM_APPROVALS : "1:N 主管审批记录"
    CLAIM_TICKETS ||--o{ OUTBOX_EVENTS : "触发跨系统可靠回调"

    ORDERS ||..o{ CLAIM_TICKETS : "跨系统 API 关联 (order_id)"
    OUTBOX_EVENTS ||..|| REFUND_EXECUTIONS : "跨系统幂等键对齐 (refund_idempotency_key)"
    KNOWLEDGE_PENDING_QUEUE ||..o| MILVUS_KNOWLEDGE_DOMAIN : "跨系统幂等发布 (chunk_id)"

    CATALOG_CATEGORIES {
        varchar catalog_category_id PK "如 HOME_APPLIANCE_HEATER"
        varchar parent_id
        varchar category_name
        enum item_type "PHYSICAL | SERVICE"
    }
    PRODUCTS_SKU {
        varchar sku_id PK "MySQL (EcomPilot SSOT)"
        varchar shop_id "店铺ID(支持跨店满减)"
        varchar catalog_category_id FK
        varchar brand_name "如 蒙牛 / 海尔"
        bigint unit_price_cents "单价(分)"
        int sellable_stock "可售库存"
        int defective_stock "售后次品仓"
        int version "乐观锁版本号"
        decimal positive_review_ratio "好评率"
    }
    ORDERS {
        varchar order_id PK "MySQL (EcomPilot SSOT)"
        varchar idempotency_key UK "下单防重唯一键"
        varchar quote_id FK
        varchar tenant_id
        varchar user_id "来自JWT可信身份"
        bigint total_original_cents
        bigint total_discount_cents
        bigint total_payable_cents
        enum order_status "CREATED|STOCK_LOCKED|PAID_SIMULATED|FULFILLED|AFTER_SALE_OPEN|REFUNDED_PARTIAL|REFUNDED_FULL|CANCELLED"
    }
    REFUND_EXECUTIONS {
        varchar refund_execution_id PK "MySQL (EcomPilot SSOT)"
        varchar refund_idempotency_key UK "跨系统退款唯一幂等键"
        varchar claim_no
        varchar order_id FK
        varchar order_item_id
        bigint refunded_cents "实际退还分值"
        enum restock_policy "SELLABLE_WAREHOUSE | DEFECTIVE_WAREHOUSE | NO_RESTOCK"
        varchar execution_status "SUCCESS | FAILED"
    }
    CLAIM_TICKETS {
        varchar claim_no PK "PostgreSQL (EcomOps SSOT)"
        varchar tenant_id
        varchar user_id
        varchar order_id "关联C端订单号"
        varchar order_item_id
        bigint max_refundable_cents "可退实付上限(分)"
        bigint approved_refund_cents "最终批准退赔(分)"
        varchar claim_status "SUBMITTED|AUTO_REJECTED|NEED_MORE_EVIDENCE|PENDING_SUPERVISOR_REVIEW|APPROVED_PENDING_CALLBACK|CALLBACK_RETRYING|COMPLETED"
        boolean fallback_used "任一轨是否触发降级"
    }
    KNOWLEDGE_PENDING_QUEUE {
        uuid queue_id PK "PostgreSQL (EcomOps SSOT)"
        varchar tenant_id
        varchar catalog_category_id
        text question_text
        text standard_answer_draft
        int version "递增版本号"
        varchar deterministic_chunk_id UK "sha256(queue_id:version)"
        enum publish_status "PENDING_REVIEW|DISCARDED|PUBLISHING|PUBLISHED|PUBLISH_FAILED"
    }
    MILVUS_KNOWLEDGE_DOMAIN {
        varchar chunk_id PK "Milvus (EcomRAG SSOT)"
        varchar tenant_id
        varchar catalog_category_id
        varchar rule_source "规则来源名称"
        bool is_demo_rule "沙盒演示规则标记"
        int64 effective_from
        int64 effective_to
        int32 version
    }
```

### 4.2 跨系统核心接口 JSON 契约

#### 1. `EcomPilot` 向 `EcomOps` 提交售后审核单：`POST /internal/v1/claims/submit`
```json
{
  "claim_no": "CLM_20261031_000182",
  "tenant_id": "SHOP_SELF_01",
  "user_id": "USR_99201",
  "order_id": "ORD_20261029_88310",
  "order_item_id": "ITEM_01",
  "sku_id": "SKU_HEATER_003",
  "catalog_category_id": "HOME_APPLIANCE_HEATER",
  "item_payable_cents": 112000,
  "max_refundable_cents": 112000,
  "days_since_delivery": 5,
  "claim_type": "QUALITY_REFUND",
  "buyer_reason_text": "这台热水器装好第二天就打不着火，底部还有点漏水，太坑了要求全额退货退款！",
  "evidence_urls": ["https://storage.example.invalid/evidence/evidence_01.jpg"],
  "nlu_signal": {
    "predicted_label": "热水器",
    "category_confidence": 0.98,
    "sentiment": "NEGATIVE",
    "sentiment_score": 0.96,
    "priority_boost": true
  }
}
```

#### 2. `EcomOps` 审批通过后回调 `EcomPilot` 执行模拟退款：`POST /internal/v1/refunds/execute`
```json
{
  "refund_idempotency_key": "REFUND_CLM_20261031_000182_V1",
  "claim_no": "CLM_20261031_000182",
  "order_id": "ORD_20261029_88310",
  "order_item_id": "ITEM_01",
  "approved_refund_cents": 112000,
  "restock_policy": "DEFECTIVE_WAREHOUSE",
  "approval_mode": "SUPERVISOR_CONFIRMED",
  "supervisor_user_id": "SUP_007",
  "approved_at": "2026-10-31T20:45:00+09:00"
}
```

#### 3. `EcomPilot` 退款执行响应与对账查询契约：`GET /internal/v1/refunds/status?refund_idempotency_key=...`
```json
{
  "refund_idempotency_key": "REFUND_CLM_20261031_000182_V1",
  "refund_execution_id": "RF_EXEC_77419",
  "order_id": "ORD_20261029_88310",
  "execution_status": "SUCCESS",
  "order_status_after": "REFUNDED_FULL",
  "refunded_cents": 112000,
  "idempotent_replayed": false,
  "executed_at": "2026-10-31T20:45:01+09:00"
}
```

## 五、 规格四：可判定验收用例与分阶段演进路线图

### 5.1 分阶段交付路线图（MVP 核心单闭环先行）

```mermaid
gantt
    title Ecom 四件套分阶段交付路线图 (MVP 单闭环先行)
    dateFormat  YYYY-MM-DD
    axisFormat  %m-%d
    section Milestone 1 (MVP)
    EcomNLU 分句预测与低置信拒识接口       :m1_1, 2026-11-01, 2d
    EcomRAG 售后规则库构建与混合检索服务   :m1_2, 2026-11-02, 3d
    EcomPilot 订单表与免单号候选确认/退款接口 :m1_3, 2026-11-04, 3d
    EcomOps 售后三轨审核图与主管Interrupt审批 :m1_4, 2026-11-06, 4d
    跨系统Outbox回调对账与AC-TX-04验收     :m1_5, 2026-11-09, 2d
    section Milestone 2
    EcomOps kb_qa 图与记忆摘要/缩进Bug修复 :m2_1, 2026-11-11, 3d
    知识待补充页与 EcomRAG 幂等Upsert飞轮  :m2_2, 2026-11-13, 3d
    section Milestone 3
    EcomPilot 乐观锁防超卖与受控凑单算价器 :m3_1, 2026-11-16, 4d
    环境适配/选品/小红书二级A2A种草联调    :m3_2, 2026-11-19, 3d
    section Milestone 4
    商品文案三层合规审查与真实MinIO存储    :m4_1, 2026-11-22, 3d
    客服五阶段抗压对练与全套离线指标评测   :m4_2, 2026-11-25, 3d
```

| 里程碑阶段 | 聚焦目标与核心范围 | 涉及模块 | 明确暂不纳入本阶段的内容（防范围蔓延） |
| :--- | :--- | :--- | :--- |
| **Milestone 1 (MVP)**<br>**跨系统售后审核与模拟退款闭环** | 完整跑通：**C 端订单查询与候选确认 → `EcomNLU` 辅助信号提取 → `EcomRAG` 条款混合检索 → `EcomOps` 三轨审核与条件分流 → 主管 Vue 3 `interrupt/resume` 审批 → `EcomPilot` 幂等模拟退款回写与对账** | 1. `EcomNLU` 预测与拒识接口<br>2. `EcomRAG` 售后规则检索接口<br>3. `EcomPilot` 订单表、售后申请与幂等退款接口<br>4. `EcomOps` 的 `claim_audit` 图、`AsyncPostgresSaver` 与主管审批页 | 暂不开发跨店凑单、小红书多图种草、酒店天气联动、商品 PDF 审查与客服多轮对练 |
| **Milestone 2**<br>**RAG 知识补录飞轮与修复基线** | 跑通：`EcomOps` 的 `kb_qa` 自适应问答 → 低分（`<0.75`）入队 `knowledge_pending_queue` → 运营在 Vue 3 补录发布 → 调用 `EcomRAG` 幂等写入 Milvus → C/B 两端立即检索生效；同时修复摘要奇数取模与落库缩进 Bug | 1. `EcomOps` 的 `kb_qa` 图与待补录页面<br>2. `EcomRAG` 的 `/internal/v1/kb/upsert` 幂等写入接口<br>3. `memory.py` 摘要触发与落库修复 | 暂不接入复杂的外部爬虫知识源 |
| **Milestone 3**<br>**C 端场景选品、受控凑单与模拟支付下单** | 跑通：`EcomPilot` 多租户会话隔离、带报价有效期的受控凑单算价器、乐观锁防超卖锁单（区分缺货与版本冲突）、模拟支付与未发货幂等取消、小红书种草笔记二级 A2A 生成 | `EcomPilot` 的 `selection_agent`、`pricing_order_agent`、`env_fit_agent`、`seeding_note_agent` 及前端购物车抽屉 | 不对接真实微信/支付宝资金通道 |
| **Milestone 4**<br>**B 端文案合规审查与客服多轮对练** | 跑通：`listing_compliance_review`（三层结构化合规输出：阻断项/待核实项/建议项 + MinIO 真实存储）与 `cs_training`（五阶段挑剔买家抗压对练 + 显式流式输出） | `EcomOps` 的其余两条 LangGraph 工作流及对应 Vue 3 页面 | 不承诺视觉排版美学打分与外部官方证书联网验真 |

### 5.2 可判定自动化验收用例清单（Verifiable Acceptance Criteria）

| 用例编号 | 测试场景描述 | 触发操作与注入故障 | 通过判定标准（Pass Criteria） |
| :--- | :--- | :--- | :--- |
| **AC-TX-01** | **下单并发重试幂等性** | 使用相同 `idempotency_key` 与 `quote_id` 并发发送 5 次 `POST /api/v1/orders/create` 请求 | 1. MySQL `orders` 表中仅生成 **1 条**订单记录；<br>2. SKU 的 `sellable_stock` 仅扣减 **1 次**采购数量；<br>3. 其余 4 次请求均返回 `200 OK` 及完全相同的 `order_id`。 |
| **AC-TX-02** | **乐观锁版本冲突与真实缺货区分** | SKU 库存设为 `10`；模拟 2 个不同用户同时购买数量 `2`（注入相同初始 `version` 制造冲突），以及第 3 个用户购买数量 `20` | 1. 两个买 `2` 件的请求经服务端内部退避重试后**均下单成功**，剩余库存为 `6`，不返回 `OUT_OF_STOCK`；<br>2. 买 `20` 件的请求立即返回 `INSUFFICIENT_STOCK`，库存保持 `6`。 |
| **AC-TX-03** | **重复取消订单防刷库存** | 对同一笔处于 `STOCK_LOCKED` 状态的订单连续调用 3 次取消接口 | 1. 第 1 次返回取消成功，订单状态变为 `CANCELLED`，库存加回 1 次；<br>2. 第 2、3 次返回幂等成功提示，**库存绝不重复增加**。 |
| **AC-TX-04** | **LangGraph 审批挂起后进程重启与单次退款保证** | 提交一笔理赔额 `112000` 分的热水器售后单使其挂起在 `supervisor_review: interrupt()`；**重启 `EcomOps` 后端进程**；随后主管并发点击 2 次“批准退款”，且首次回调 `EcomPilot` 时注入网络读取超时 | 1. 进程重启后，主管仍能从 `AsyncPostgresSaver` 正常读取挂起态并恢复工作流；<br>2. 对账重试机制最终将工单推进至 `COMPLETED`；<br>3. `EcomPilot` 的 `refund_executions` 表中仅存在 **1 条**退款记录，订单实退金额严格等于 `112000` 分。 |
| **AC-SEC-01** | **跨用户越权访问拦截与免单号反查确认** | 用户 A 携带自身 JWT，在请求参数中伪造用户 B 的 `order_id` 申请售后；以及用户 A 发起不带订单号的售后吐槽 | 1. 伪造他人 `order_id` 的请求直接返回 `403 Forbidden` 或 `404 Not Found`；<br>2. 免单号吐槽仅返回用户 A 自己的候选订单列表，`claim_tickets` 表中在用户二次确认前**不生成**任何记录。 |
| **AC-KB-01** | **知识补录发布失败重试无重复向量** | 运营发布一条待补录知识，首次写入 Milvus 成功后人为中断 PG 状态回写（使状态停留在 `PUBLISHING` / `PUBLISH_FAILED`），随后点击重试发布 | 1. PG 状态最终变为 `PUBLISHED`；<br>2. 在 Milvus `knowledge_domain` 中按该 `chunk_id` 查询，**仅存在 1 条记录**，无重复向量副本。 |
