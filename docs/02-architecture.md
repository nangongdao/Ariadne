# 02 系统架构

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│  客户端层                                                             │
│  Python SDK  │  TS SDK  │  CLI (ariadne run/eval)  │  Web Console    │
└───────┬──────────────────────────────────────────────┬───────────────┘
        │ OTLP/HTTP + REST                             │ REST + SSE
        ▼                                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  接入层  API Gateway (FastAPI)                                        │
│  认证鉴权 │ 速率限制 │ 租户路由 │ OTLP 收敛 │ SSE 广播               │
└───────┬──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  编排层  Loop Engine  ← 核心差异化                                    │
│  状态机 │ Ralph Verifier │ Critique Synthesizer │ 预算熔断 │ 检查点   │
└───────┬──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  约束层  Harness Engine                                               │
│  规则求值（输入/输出/资源/工具）│ 5 个执行卡点 │ 动作裁决            │
└───────┬──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  执行层  Workflow Runtime                                             │
│  LLM Adapter │ Tool Registry │ RAG Retriever │ Sandbox Executor      │
└───────┬──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  评测层  Evaluation Engine                                            │
│  Deterministic │ Statistical │ LLM-as-Judge │ 成本归因 │ SLI 计算    │
└───────┬──────────────────────────────────────────────────────────────┘
        ▼   （断言结果回灌 Loop Engine，形成闭环）
┌──────────────────────────────────────────────────────────────────────┐
│  存储层                                                               │
│  ClickHouse(span/metric) │ Postgres(配置/结果) │ Redis(队列/计数)     │
│  S3/MinIO(大 payload、artifact)                                       │
└──────────────────────────────────────────────────────────────────────┘
```

关键结构决策：**Harness 在 Loop 之下、执行之上**。Loop 负责"改什么"，Harness 负责"什么绝不允许发生"。前者是软目标（可迭代逼近），后者是硬边界（越界直接拦截），两者不能合并 —— 否则模型可以通过"多试几轮"绕过安全约束。

## 2. 组件职责

| 组件 | 职责 | 关键约束 |
|---|---|---|
| **API Gateway** | 认证、鉴权、限流、OTLP 接收、SSE 广播 | 无业务逻辑，纯转发与校验 |
| **Loop Engine** | 状态机推进、轮次决策、上下文构造、检查点 | 有状态但不落业务数据，状态全部持久化在 Postgres |
| **Ralph Verifier** | 外部裁决任务是否真完成 | **不读取模型的自我评估文本**，只信 verifier 输出 |
| **Critique Synthesizer** | 把失败断言转成结构化修正指令 | 输出必须是结构化对象，不是自由文本 |
| **Harness Engine** | 规则求值与动作裁决 | 纯函数式求值，无副作用；超时必须 fail-closed |
| **Workflow Runtime** | 实际执行 LLM/工具/RAG/代码 | 所有出站调用必经 Harness 卡点 |
| **Sandbox Executor** | 隔离执行不可信代码 | 默认禁网、只读根文件系统、无宿主挂载 |
| **Evaluation Engine** | 打分、断言求值、成本归因 | 评测本身不得调用被评测的同一模型实例 |
| **Collector Worker** | span 落库、采样、脱敏、payload 外溢 | 幂等消费，支持重放 |

## 3. 三条关键数据流

### 3.1 遥测采集流（写多读少，高吞吐）

```
SDK 批量缓冲 → OTLP/HTTP → Gateway 校验租户 → Redis Stream
  → Collector Worker（尾采样 + PII 脱敏 + 大 payload 外溢 S3）
  → ClickHouse 批量 INSERT（每 200ms 或 5000 行触发）
```

同步路径只做"入队"，所有加工都在 Worker 侧异步完成，保证 SDK 侧开销 < 1ms。

### 3.2 Loop 执行流（低吞吐，长事务）

```
POST /v1/loops → 校验 Goal 可验证性 → 写 Postgres(loop_runs) → 入 Redis 任务队列
  → Loop Worker 拉取 → [ 轮次循环:
        构造上下文 → Harness 前置卡点 → Runtime 执行 → Harness 后置卡点
        → Evaluation 打分 → Ralph 裁决 → 写检查点 → SSE 推送
     ]
  → 终态写回 Postgres + 产物落 S3
```

每一轮结束就写检查点，Worker 崩溃后由另一 Worker 从检查点接管（Redis 可见性超时机制）。

### 3.3 查询流（读多写少，要求低延迟）

```
Console → REST → 查询路由:
  聚合/时序/全文 → ClickHouse（预聚合物化视图优先）
  配置/结果/权限 → Postgres
  大 payload → S3 预签名 URL 直读（不经过后端转发）
```

## 4. 技术栈选型与理由

### 4.1 后端

| 选择 | 理由 | 被否方案 |
|---|---|---|
| **Python 3.11+** | LLM 生态（OTel GenAI instrumentation、各家 SDK、eval 库）全部以 Python 为一等公民；团队心智负担最低 | Go：吞吐更好但要自己重写全部 LLM 适配与 eval 生态，代价远大于收益 |
| **FastAPI + Uvicorn** | 原生 async，SSE 支持简单；Pydantic 集成让 API schema 与配置模型共用一套定义 | Django：ORM 重、async 支持晚 |
| **Pydantic v2** | 配置、API schema、LLM 结构化输出校验三处复用同一套模型；Rust 核心性能足够 | dataclass + 手写校验：无法直接生成 JSON Schema 喂给模型 |
| **SQLAlchemy 2.x（async）** | 需要事务语义与迁移（Alembic），Loop 状态机对一致性敏感 | 裸 SQL：状态机 + RBAC 的关系查询会失控 |
| **Redis Streams** | 需要消费者组 + 可见性超时来实现 Loop Worker 故障接管；同时兼作预算计数器 | RabbitMQ/Kafka：M1-M3 阶段运维成本不划算，M5 起若吞吐不足再引入 Kafka |

Collector 热路径若成为瓶颈，**允许单独用 Rust/Go 重写 Collector Worker**（它是纯数据管道，接口边界清晰，替换代价可控）。其余部分保持 Python。

### 4.2 存储分工

| 存储 | 承载数据 | 选择理由 |
|---|---|---|
| **ClickHouse** | span、metric、eval 明细（append-only、高基数、时序聚合） | 列存 + 稀疏索引对"按时间范围聚合 + 高基数过滤"是数量级优势；行业已验证（Langfuse、Phoenix 均采用，Langfuse 2026-01 被 ClickHouse 收购进一步确认这条路径） |
| **PostgreSQL** | 项目、用户、RBAC、prompt 版本、dataset、loop_runs 状态机、断言定义 | 需要事务、外键、行级安全；数据量小但一致性要求高 |
| **Redis** | 任务队列、预算计数器、限流令牌、SSE pub/sub、幂等键 | 原子计数（预算熔断必须强一致且低延迟）；Streams 提供消费者组 |
| **S3 / MinIO** | 大 payload（>32KB 的 prompt/completion）、代码产物、diff 快照 | 把大对象踢出数据库，ClickHouse 只存引用；前端凭预签名 URL 直读 |

**不做的事**：不用向量数据库。Ariadne 自身不做 RAG，只观测用户的 RAG；语义相似度检索若需要，用 ClickHouse 的向量距离函数即可满足平台内部检索需求。

### 4.3 执行隔离

| 选择 | 理由 |
|---|---|
| **gVisor（默认）** | 用户态内核拦截 syscall，防逃逸强于纯 Docker，启动开销远低于完整 VM；适合"每轮迭代跑一次测试"的高频短任务 |
| **Firecracker microVM（高安全档）** | 真硬件虚拟化边界，用于执行完全不可信代码的多租户 SaaS 模式 |
| 被否：裸 Docker | 共享宿主内核，容器逃逸风险不可接受 |
| 被否：进程级隔离 | 无法可靠限制 syscall 与网络 |

### 4.4 前端

| 选择 | 理由 |
|---|---|
| **React 19 + TypeScript** | 生态成熟，React Flow / ECharts 都是一等支持 |
| **React Flow** | DAG 编排（拖拽 + 连线 + 自定义节点 + 运行时状态动画）现成能力最完整 |
| **ECharts** | Loop 得分趋势、Token 柱状、时间轴甘特图；大数据量下渲染性能优于 D3 手写 |
| **TanStack Query + Virtual** | 服务端状态缓存 + 万级 span 列表虚拟滚动 |
| **自研 Trace 树组件** | 现成库都不满足"嵌套 span + 折叠 + 按耗时/成本筛选 + 关键路径高亮"的组合需求 |

## 5. 部署形态

| 形态 | 场景 | 组成 |
|---|---|---|
| **单机 Compose（M1 起）** | 本地开发、个人使用 | 单容器 API + ClickHouse + Postgres + Redis + MinIO |
| **单租户自托管（M4 起）** | 企业内部署，数据不出域 | K8s Helm chart，API/Worker 分离部署，可横向扩 Worker |
| **多租户 SaaS（M6，待决策）** | 公有云服务 | 强制租户隔离 + Firecracker 沙箱 + 配额计费 |

Worker 分三类独立伸缩：`collector-worker`（吞吐型）、`loop-worker`（长任务型）、`eval-worker`（CPU/LLM 混合型）。三者的资源画像完全不同，混部会导致长任务饿死采集管道。

## 6. 代码结构

遵循小文件原则（200-400 行/文件），模块内用 registry + factory 组织可插拔实现：

```
src/
├── api/                      # FastAPI 接入层
│   ├── routers/              # 按资源拆分路由
│   ├── deps.py               # 认证/租户/DB 依赖注入
│   └── sse.py                # SSE 广播
├── loop_module/              # Loop 引擎
│   ├── __init__.py           # LoopModeFactory / register_loop_mode
│   ├── state_machine.py      # 状态转移（纯函数）
│   ├── engine.py             # 编排主循环
│   ├── verifier/             # Ralph Verifier 各实现
│   ├── critique.py           # 结构化修正指令生成
│   ├── context.py            # 上下文收敛策略
│   └── budget.py             # 三层熔断
├── harness_module/           # 约束引擎
│   ├── __init__.py           # RuleFactory / register_rule
│   ├── rules/                # 输入/输出/资源/工具四类规则
│   ├── evaluator.py          # 规则求值（CEL）
│   └── sandbox/              # gVisor / Firecracker 驱动
├── eval_module/              # 评测引擎
│   ├── __init__.py           # EvaluatorFactory / register_evaluator
│   ├── deterministic/        # 断言型
│   ├── statistical/          # 指标型
│   ├── judge/                # LLM-as-Judge + 元评测
│   └── sli.py                # 7 项 SLI 计算
├── runtime_module/           # 工作流执行
│   ├── llm/                  # 各 provider adapter
│   ├── tools/                # 工具注册表
│   └── dag.py                # DAG 拓扑执行器
├── telemetry_module/         # 可观测性
│   ├── semconv.py            # OTel GenAI 属性映射（版本锁定）
│   ├── adapters/             # OpenInference / OpenLLMetry 归一化
│   ├── sampling.py           # 头部 + 尾部采样
│   └── redaction.py          # PII 脱敏
├── storage/                  # 存储访问层
│   ├── clickhouse/
│   ├── postgres/
│   └── objectstore.py
└── utils/                    # 共享工具（logger、时间、ID）

web/                          # 前端
tests/                        # 单元 / 集成 / 端到端
deploy/                       # Compose / Helm / ClickHouse DDL
```

配置统一用 Pydantic Settings（环境变量 + YAML），所有超参禁止硬编码；`frozen=True` 保证配置对象不可变。

