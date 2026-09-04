# 08 数据模型

## 1. 存储分工

| 存储 | 数据特征 | 承载内容 |
|---|---|---|
| ClickHouse | append-only、高吞吐写、时序聚合读 | span、eval 明细、loop 轮次记录、SLI 物化视图 |
| PostgreSQL | 低量、强一致、关系复杂 | 租户/项目/用户/RBAC、prompt 版本、dataset、loop 状态机与检查点、规则集、审批 |
| Redis | 高频原子操作、临时 | 任务队列、预算计数、限流、SSE pub/sub、幂等键、沙箱池 |
| S3 / MinIO | 大对象、低频读 | 大 payload、代码产物、diff 快照、数据集导出 |

判断依据：**需要事务和外键的进 Postgres，需要按时间聚合的进 ClickHouse。** 混淆这条会导致两边都难用 —— 在 ClickHouse 里做状态机更新是灾难（无事务、UPDATE 昂贵），在 Postgres 里扫亿级 span 同样是灾难。

## 2. ClickHouse

### 2.1 spans 主表

```sql
CREATE TABLE spans (
    -- 分区与主键相关
    project_id      UUID,
    started_at      DateTime64(6, 'UTC'),
    trace_id        String,
    span_id         String,
    parent_span_id  String,

    -- 语义（内部字段名，非 gen_ai.* 直接映射）
    name            LowCardinality(String),
    kind            LowCardinality(String),    -- llm / tool / rag / code / loop / harness / eval
    operation       LowCardinality(String),
    provider        LowCardinality(String),
    model_request   LowCardinality(String),
    model_response  LowCardinality(String),

    -- 状态
    status          LowCardinality(String),    -- ok / error / blocked
    error_type      LowCardinality(String),
    duration_ms     UInt32,

    -- 用量与成本
    input_tokens        UInt32,
    output_tokens       UInt32,
    cache_read_tokens   UInt32,
    cache_write_tokens  UInt32,
    reasoning_tokens    UInt32,
    cost_usd            Decimal(12, 8),

    -- Loop 关联
    loop_id         String,
    iteration       UInt16,
    failure_fp      String,

    -- Payload：小的内联，大的存引用
    input_preview   String,
    output_preview  String,
    input_ref       String,      -- S3 key
    output_ref      String,

    -- 扩展属性
    attributes      Map(LowCardinality(String), String),
    tags            Array(LowCardinality(String)),

    INDEX idx_trace   trace_id   TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_loop    loop_id    TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_model   model_request TYPE set(100)        GRANULARITY 4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(started_at)
ORDER BY (project_id, started_at, trace_id, span_id)
TTL started_at + INTERVAL 90 DAY DELETE,
    started_at + INTERVAL 14 DAY TO VOLUME 'cold'
SETTINGS index_granularity = 8192;
```

设计说明：

- `ORDER BY` 以 `project_id` 开头 —— 所有查询都带租户过滤，这让多租户查询天然只扫自己的数据块。
- `LowCardinality` 用于枚举型字段，压缩率与过滤性能都显著提升。
- `trace_id` / `loop_id` 用 bloom filter 跳数索引：这两个是"精确查单条"的主要入口，但不在排序键中。
- TTL 分两级：14 天后转冷存储（对象存储卷），90 天删除。热数据保留期与查询习惯匹配。
- `attributes` 用 Map 而非 JSON 字符串：ClickHouse 对 Map 有原生索引与提取优化。

### 2.2 其他 ClickHouse 表

```sql
-- Loop 轮次明细（驱动进化视图）
CREATE TABLE loop_iterations (
    project_id      UUID,
    loop_id         String,
    iteration       UInt16,
    started_at      DateTime64(3, 'UTC'),
    duration_ms     UInt32,
    state           LowCardinality(String),
    score           Float32,
    converged       UInt8,
    claimed_done    UInt8,
    false_completion UInt8,
    passed_ids      Array(LowCardinality(String)),
    failed_ids      Array(LowCardinality(String)),
    failure_fp      String,
    output_fp       String,
    tokens          UInt32,
    cost_usd        Decimal(12, 8),
    critique_ref    String       -- S3 key
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(started_at)
ORDER BY (project_id, loop_id, iteration);

-- Loop 终态（SLI 计算源）
CREATE TABLE loop_outcomes (
    project_id      UUID,
    loop_id         String,
    mode            LowCardinality(String),
    final_state     LowCardinality(String),
    iteration_count UInt16,
    finished_at     DateTime64(3, 'UTC'),
    total_duration_ms UInt32,
    total_tokens    UInt32,
    total_cost_usd  Decimal(12, 8),
    first_try_pass  UInt8,
    stalled_reason  LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(finished_at)
ORDER BY (project_id, finished_at, loop_id);

-- 评测明细
CREATE TABLE eval_results (
    project_id      UUID,
    run_id          String,       -- experiment run 或 loop iteration
    item_id         String,
    evaluator       LowCardinality(String),
    score           Float32,
    passed          UInt8,
    evidence        String,
    judge_model     LowCardinality(String),
    created_at      DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (project_id, run_id, item_id, evaluator);
```

物化视图（预聚合，服务 SLI 面板）：`mv_loop_outcomes`（终态分布）、`mv_cost_by_model`（成本归因）、`mv_latency_quantiles`（延迟分位，用 `quantilesState`）、`mv_eval_score_dist`（分数分布）。全部按分钟粒度聚合，查询时二次聚合。

## 3. PostgreSQL

```sql
-- 租户与项目
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    settings    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, slug)
);

-- API 密钥（只存哈希）
CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,              -- 前 8 位，用于展示与定位
    key_hash    TEXT NOT NULL,              -- Argon2id
    scopes      TEXT[] NOT NULL DEFAULT '{ingest}',
    last_used_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON api_keys (key_prefix) WHERE revoked_at IS NULL;
```

```sql
-- Loop 状态机
CREATE TABLE loop_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    spec_id         UUID REFERENCES specs(id),
    mode            TEXT NOT NULL,
    goal            JSONB NOT NULL,            -- Goal 的序列化
    state           TEXT NOT NULL,
    iteration       INT  NOT NULL DEFAULT 0,
    cumulative_tokens BIGINT NOT NULL DEFAULT 0,
    cumulative_cost_usd NUMERIC(12,8) NOT NULL DEFAULT 0,
    final_state     TEXT,
    worker_id       TEXT,                      -- 当前持有者
    lease_expires_at TIMESTAMPTZ,              -- 可见性超时
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    CONSTRAINT valid_state CHECK (state IN (
        'CREATED','VALIDATE','PLANNING','PRECHECK','EXECUTING','EVALUATING',
        'JUDGING','REVISING','HUMAN_PENDING','CONVERGED','REJECTED','BLOCKED',
        'BUDGET_EXCEEDED','MAX_ITERATIONS','STALLED','FAILED','CANCELLED'))
);
CREATE INDEX ON loop_runs (project_id, created_at DESC);
CREATE INDEX ON loop_runs (state) WHERE final_state IS NULL;   -- 活跃任务扫描
CREATE INDEX ON loop_runs (lease_expires_at) WHERE final_state IS NULL;

-- 检查点（每轮一条，不可变）
CREATE TABLE loop_checkpoints (
    loop_id         UUID NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    iteration       INT  NOT NULL,
    state           TEXT NOT NULL,
    verdict         JSONB NOT NULL,
    critique        JSONB,
    artifact_refs   TEXT[] NOT NULL DEFAULT '{}',
    cumulative_tokens BIGINT NOT NULL,
    cumulative_cost_usd NUMERIC(12,8) NOT NULL,
    output_fp       TEXT NOT NULL,
    failure_fp      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (loop_id, iteration)
);

-- Prompt 版本
CREATE TABLE prompt_versions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    version     INT  NOT NULL,
    template    TEXT NOT NULL,
    variables   JSONB NOT NULL DEFAULT '[]',
    labels      TEXT[] NOT NULL DEFAULT '{}',   -- production / staging
    content_hash TEXT NOT NULL,
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name, version)
);

-- 数据集
CREATE TABLE datasets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    version     INT  NOT NULL,
    content_hash TEXT NOT NULL,
    item_count  INT  NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name, version)
);

-- 人工审批
CREATE TABLE approvals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    loop_id     UUID NOT NULL REFERENCES loop_runs(id) ON DELETE CASCADE,
    iteration   INT  NOT NULL,
    reason      TEXT NOT NULL,
    payload     JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/expired
    decided_by  UUID REFERENCES users(id),
    decided_at  TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 审计（只追加）
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    project_id  UUID,
    actor_type  TEXT NOT NULL,    -- user / api_key / system
    actor_id    TEXT,
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}',
    ip          INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
REVOKE UPDATE, DELETE ON audit_log FROM app_role;
```

其余表：`users`、`memberships`（用户-组织-角色）、`specs`、`rule_sets`、`experiments`、`experiment_items`、`model_pricing`（带 `effective_from` / `effective_to` 区间）。

## 4. Redis Key 设计

| Key 模式 | 类型 | TTL | 用途 |
|---|---|---|---|
| `q:loop` | Stream | —— | Loop 任务队列（消费者组 `loop-workers`） |
| `q:collect` | Stream | —— | span 采集队列（消费者组 `collector-workers`） |
| `budget:{loop_id}:tokens` | String(int) | 24h | 预扣/结算的原子计数 |
| `budget:{loop_id}:cost` | String(int) | 24h | 成本以微美分整数存储，避免浮点误差 |
| `budget:pool:{project_id}` | String(int) | 1h | 并行 Loop 共享预算池 |
| `rate:{key_id}:{window}` | String(int) | 窗口长度 | 限流令牌 |
| `sse:{loop_id}` | Pub/Sub | —— | 事件广播 |
| `idem:{key}` | String | 24h | 幂等去重 |
| `sandbox:pool:{profile}` | List | —— | 预热实例句柄 |
| `lock:{resource}` | String | 30s | 分布式锁（SET NX PX） |

成本用**整数微美分**存储：Redis 的 `INCRBYFLOAT` 有精度问题，预算判定必须精确。

## 5. S3 布局

```
s3://ariadne-{env}/
├── payloads/{project_id}/{yyyy}/{mm}/{dd}/{span_id}.{in|out}.zst
├── artifacts/{project_id}/{loop_id}/{iteration}/{filename}
├── critiques/{project_id}/{loop_id}/{iteration}.json
├── diffs/{project_id}/{loop_id}/{iteration}.patch
└── exports/{project_id}/{dataset_id}-v{n}.jsonl
```

全部对象强制服务端加密（SSE-KMS）；生命周期规则与 ClickHouse TTL 对齐（14 天转 IA，90 天删除）；`payloads/` 前缀按日期分片避免热分区。

## 6. 迁移与演进

- Postgres 用 Alembic，迁移脚本入库版本控制，**禁止手工改线上 schema**。
- ClickHouse 加列走 `ALTER TABLE ADD COLUMN`（廉价）；改排序键必须新建表 + 后台回填 + 原子交换，不允许在线改。
- 内部 span 字段名一旦发布即视为契约，**上游 semconv 改名只改适配层映射**，不迁移历史数据。

