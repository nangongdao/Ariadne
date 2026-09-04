# M1 实施规格：可观测骨架

## 1. 交付定义

一条**端到端可运行的垂直切片**：SDK 埋点 → HTTP 上报 → 队列 → Worker 加工 → ClickHouse → 查询 API → Trace 调用树。

M1 完成的判定标准：接入一个真实 LLM 应用，30 分钟内在界面上看到完整调用树与成本明细。

## 2. 范围边界

### 做

| 项 | 内容 |
|---|---|
| 内部 Span 契约 | `AriadneSpan`（Pydantic），与 ClickHouse 列一一对应，作为稳定契约 |
| 适配层 | OTLP/HTTP（JSON）+ OpenInference + 原生格式，全部归一化到 `AriadneSpan` |
| 成本引擎 | 计价表带生效时间区间；缓存 Token / 推理 Token 单独计量 |
| 脱敏 | PII 掩码，同一 trace 内实体映射到同一占位符 |
| Payload 分级 | ≤8KB 内联 / 8-32KB zstd 压缩 / >32KB 外溢（M1 用本地对象存储桩） |
| 采集管道 | API 入队 → Redis Streams → Collector Worker → ClickHouse 批量写 |
| 查询 API | trace 列表、trace 详情（树）、span 过滤、成本汇总 |
| Python SDK | 装饰器 + 上下文管理器 + OpenAI/Anthropic 自动埋点 + 后台批量上报 |
| 部署 | Docker Compose 一键起（ClickHouse + Redis + API + Worker） |
| 控制台 | Trace 列表 / 调用树 / 时间轴 / Span 过滤 / 成本归因 / 设置，由 API 托管 |
| 测试 | 单元（纯函数）+ 集成（真实容器，不 mock 数据库） |

### 不做（留给后续里程碑）

评估器、Loop、Harness 规则引擎、沙箱、DAG 编排 UI、多租户 RLS、TypeScript SDK、OTLP protobuf（M1 只做 JSON，protobuf 留 M2）、SSE 实时推送（M1 用轮询，Loop 视图才真正需要 SSE）。

M1 的租户模型简化为：**单 org + 单 project + 静态 API Key**（配置文件提供），完整 RBAC 留到 M6。这样可以先把数据链路打通，不被权限模型拖慢。

## 3. 核心契约：AriadneSpan

设计约束：**字段名是内部契约，不直接用 `gen_ai.*`**。上游 semconv 改名只改适配层映射。

```python
class AriadneSpan(BaseModel):
    # 身份
    project_id: UUID
    trace_id: str            # 32 hex
    span_id: str             # 16 hex
    parent_span_id: str = ""

    # 语义
    name: str
    kind: SpanKind           # llm/tool/rag/code/loop/harness/eval/internal
    operation: str = ""      # chat/embeddings/tool.execute/...
    provider: str = ""
    model_request: str = ""
    model_response: str = ""  # 实际响应版本，可能与请求不同

    # 时间与状态
    started_at: datetime
    duration_ms: int
    status: SpanStatus       # ok/error/blocked
    error_type: str = ""

    # 用量（成本由服务端计算，SDK 不算）
    usage: TokenUsage
    cost_usd: Decimal = Decimal("0")

    # Loop 关联（M1 预留字段，M3 启用）
    loop_id: str = ""
    iteration: int = 0
    failure_fp: str = ""

    # Payload
    input_preview: str = ""
    output_preview: str = ""
    input_ref: str = ""      # 外溢后的对象键
    output_ref: str = ""

    attributes: dict[str, str] = {}
    tags: list[str] = []
```

`cost_usd` **由服务端计算而非 SDK**：计价表变更不需要升级客户端，且防止客户端伪造成本数据。

## 4. 数据流与关键决策

```
业务线程 ──set_attributes──► Span 对象
    │ 结束时序列化入有界队列（< 1ms，永不阻塞）
    ▼
后台线程 ──批量(100 条 / 2s)──► POST /v1/ingest/spans
    ▼
API ──校验 API Key + schema──► XADD q:collect (Redis Stream)
    ▼
Collector Worker (消费者组) ─┬─► 成本计算（计价表 + 缓存折扣）
                             ├─► PII 脱敏（trace 内一致占位符）
                             ├─► Payload 分级（内联/压缩/外溢）
                             └─► 批量 INSERT ClickHouse（200ms 或 5000 行）
    ▼
查询 API ──► ClickHouse ──► Trace 树 / 成本汇总
```

四个关键决策：

1. **同步路径只入队**。SDK 侧与 API 侧都不做加工，全部推给 Worker。这是 < 1ms 开销与 5000 spans/s 吞吐的前提。
2. **Redis Streams 而非 List**。需要消费者组 + ACK + 可见性超时，Worker 崩溃后消息不丢（`XAUTOCLAIM` 回收）。
3. **幂等消费**。以 `(trace_id, span_id)` 为幂等键；ClickHouse 用 `ReplacingMergeTree` 去重，重放不产生重复行。
4. **SDK 异常永不外抛**。所有失败内部捕获并计数，业务代码不会因为观测组件挂掉而失败。

## 5. 模块清单

```
src/ariadne/                      # 服务端
├── config.py                     # Pydantic Settings（环境变量 + .env）
├── telemetry/
│   ├── models.py                 # AriadneSpan / TokenUsage / 枚举
│   ├── adapters/
│   │   ├── __init__.py           # AdapterFactory + register_adapter
│   │   ├── native.py             # 自有 JSON 格式
│   │   ├── otlp.py               # OTLP/HTTP JSON → AriadneSpan
│   │   └── openinference.py      # OpenInference → AriadneSpan
│   ├── pricing.py                # 计价表 + 成本计算
│   └── redaction.py              # PII 检测与脱敏
├── storage/
│   ├── clickhouse.py             # 连接 + 批量写 + 查询
│   ├── queue.py                  # Redis Streams 封装
│   └── objectstore.py            # 大 payload 外溢（本地/S3）
├── api/
│   ├── app.py                    # FastAPI 装配
│   ├── deps.py                   # API Key 认证 + 依赖注入
│   ├── errors.py                 # RFC 9457 Problem Details
│   └── routers/
│       ├── ingest.py             # POST /v1/ingest/spans, /v1/traces
│       ├── traces.py             # GET /v1/traces, /v1/traces/{id}
│       └── costs.py              # GET /v1/costs
├── worker/
│   ├── collector.py              # 消费循环
│   └── processor.py              # 加工管线（成本/脱敏/分级）
└── utils/
    ├── logging.py                # 结构化 JSON 日志
    └── ids.py                    # trace/span id 生成与校验

src/ariadne_sdk/                  # 客户端（独立包，最小依赖）
├── client.py                     # Ariadne 客户端
├── span.py                       # Span 与上下文传播（contextvars）
├── exporter.py                   # 有界队列 + 后台线程 + 退避重试
├── decorators.py                 # @trace
└── instrument/
    ├── openai.py                 # OpenAI 自动埋点
    └── anthropic.py              # Anthropic 自动埋点
```

单文件控制在 200-400 行；可插拔部分（适配器）用 registry + factory。

## 6. 验收清单

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | SDK 同步开销 < 1ms | 基准测试，1000 次 span 创建取 p99 |
| 2 | Worker 吞吐 ≥ 5000 spans/s | 压测脚本灌数据，测端到端速率 |
| 3 | 幂等：重放不产生重复 | 同一批消息投递两次，查行数不变 |
| 4 | 成本正确：缓存 Token 走折扣价 | 单元测试覆盖含缓存的用量组合 |
| 5 | 脱敏：trace 内占位符一致 | 单元测试同一实体出现两次 → 同一占位符 |
| 6 | 大 payload 外溢 | >32KB 输入 → ClickHouse 只存引用 + 预览 |
| 7 | Trace 树正确嵌套 | 集成测试构造三层嵌套，校验树结构与耗时聚合 |
| 8 | SDK 异常不外抛 | 故意让上报地址不可达，业务函数仍正常返回 |
| 9 | Compose 一键起 | 全新环境 `docker compose up` 后跑通 demo |

## 7. 本地开发命令

```bash
uv sync --all-extras              # 装依赖
docker compose up -d              # 起 ClickHouse + Redis
uv run ariadne-migrate            # 建表
uv run ariadne-api                # 起 API
uv run ariadne-worker             # 起 Worker
uv run pytest -m "not integration"   # 单元测试
uv run pytest -m integration      # 集成测试（需容器）
uv run ruff check . && uv run mypy src/
```

