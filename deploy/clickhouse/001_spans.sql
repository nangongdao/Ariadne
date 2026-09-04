-- M1 建表脚本。幂等：可重复执行。
CREATE DATABASE IF NOT EXISTS ariadne;

-- ReplacingMergeTree 而非 MergeTree：采集管道是至少一次投递，
-- 重放必然产生重复行，靠 (trace_id, span_id) 去重保证幂等。
CREATE TABLE IF NOT EXISTS ariadne.spans
(
    project_id          UUID,
    trace_id            String,
    span_id             String,
    parent_span_id      String DEFAULT '',

    name                String,
    kind                LowCardinality(String),
    operation           LowCardinality(String) DEFAULT '',
    provider            LowCardinality(String) DEFAULT '',
    model_request       LowCardinality(String) DEFAULT '',
    model_response      LowCardinality(String) DEFAULT '',

    started_at          DateTime64(6, 'UTC'),
    duration_ms         UInt32 DEFAULT 0,
    status              LowCardinality(String) DEFAULT 'ok',
    error_type          String DEFAULT '',

    input_tokens        UInt32 DEFAULT 0,
    output_tokens       UInt32 DEFAULT 0,
    cache_read_tokens   UInt32 DEFAULT 0,
    cache_write_tokens  UInt32 DEFAULT 0,
    reasoning_tokens    UInt32 DEFAULT 0,
    cost_usd            Decimal(18, 8) DEFAULT 0,

    loop_id             String DEFAULT '',
    iteration           UInt16 DEFAULT 0,
    failure_fp          String DEFAULT '',

    input_preview       String DEFAULT '',
    output_preview      String DEFAULT '',
    input_ref           String DEFAULT '',
    output_ref          String DEFAULT '',

    attributes          Map(LowCardinality(String), String),
    tags                Array(LowCardinality(String)),

    ingested_at         DateTime64(3, 'UTC') DEFAULT now64(3),

    INDEX idx_trace  trace_id      TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_loop   loop_id       TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_model  model_request TYPE set(100)           GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (project_id, trace_id, span_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;

-- 按 trace 聚合的物化视图：trace 列表页是最高频查询，
-- 实时扫 spans 在亿级数据量下无法满足 500ms p95。
CREATE TABLE IF NOT EXISTS ariadne.trace_rollup
(
    project_id      UUID,
    trace_id        String,
    started_at      SimpleAggregateFunction(min, DateTime64(6, 'UTC')),
    last_at         SimpleAggregateFunction(max, DateTime64(6, 'UTC')),
    span_count      SimpleAggregateFunction(sum, UInt64),
    error_count     SimpleAggregateFunction(sum, UInt64),
    total_tokens    SimpleAggregateFunction(sum, UInt64),
    -- Decimal(38, 8) 而非源列的 Decimal(18, 8)：sum(Decimal(P, S)) 为防溢出
    -- 一律返回 Decimal(38, S)，而 SimpleAggregateFunction(sum, T) 要求 T 恰好
    -- 等于该返回类型，写 18 会被拒（BAD_ARGUMENTS: Incompatible data types）。
    total_cost_usd  SimpleAggregateFunction(sum, Decimal(38, 8)),
    root_name       SimpleAggregateFunction(any, String),
    models          SimpleAggregateFunction(groupUniqArrayArray, Array(String))
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(started_at)
ORDER BY (project_id, trace_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS ariadne.mv_trace_rollup
TO ariadne.trace_rollup AS
SELECT
    project_id,
    trace_id,
    -- 必须写 spans.started_at：第一列的别名 started_at 会遮蔽同名源列，
    -- 后面裸写 started_at 会解析到那个别名，报 ILLEGAL_AGGREGATION（聚合套聚合）。
    min(spans.started_at)                               AS started_at,
    max(spans.started_at)                               AS last_at,
    sum(1)                                              AS span_count,
    sum(status = 'error')                               AS error_count,
    sum(input_tokens + output_tokens
        + cache_read_tokens + cache_write_tokens)       AS total_tokens,
    sum(cost_usd)                                       AS total_cost_usd,
    anyIf(name, parent_span_id = '')                    AS root_name,
    groupUniqArrayArray([model_request])                AS models
FROM ariadne.spans
GROUP BY project_id, trace_id;

-- 成本归因：按分钟 × 模型预聚合
CREATE TABLE IF NOT EXISTS ariadne.cost_rollup
(
    project_id      UUID,
    minute          DateTime('UTC'),
    provider        LowCardinality(String),
    model_request   LowCardinality(String),
    span_count      SimpleAggregateFunction(sum, UInt64),
    input_tokens    SimpleAggregateFunction(sum, UInt64),
    output_tokens   SimpleAggregateFunction(sum, UInt64),
    cache_read_tokens SimpleAggregateFunction(sum, UInt64),
    cost_usd        SimpleAggregateFunction(sum, Decimal(38, 8))  -- 同上：sum 的返回类型
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(minute)
ORDER BY (project_id, minute, provider, model_request);

CREATE MATERIALIZED VIEW IF NOT EXISTS ariadne.mv_cost_rollup
TO ariadne.cost_rollup AS
SELECT
    project_id,
    toStartOfMinute(started_at) AS minute,
    provider,
    model_request,
    sum(1)                      AS span_count,
    sum(input_tokens)           AS input_tokens,
    sum(output_tokens)          AS output_tokens,
    sum(cache_read_tokens)      AS cache_read_tokens,
    sum(cost_usd)               AS cost_usd
FROM ariadne.spans
WHERE kind = 'llm'
GROUP BY project_id, minute, provider, model_request;
