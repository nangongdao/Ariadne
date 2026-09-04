-- 004: cost_rollup 补 cache_write_tokens / reasoning_tokens（审计 P1-9）
--
-- spans 表从 M1 起就记录 cache_write_tokens 与 reasoning_tokens，但成本
-- 聚合表与物化视图从未带上它们：成本 API 的 token 总量与缓存指标对带
-- 缓存写入或推理 Token 的模型系统性低估。
--
-- 语义约定（与 spans 的 total_tokens 表达式一致）：
--   total = input + output + cache_read + cache_write
--   reasoning 是 output 的**子集**（主流 provider 把推理 token 计入
--   output_tokens 计费），单独展示，不并入总量 —— 并了就是双重计数。
--
-- 历史聚合行的新列回填 0：迁移点之前的数据没按此维度聚合，无法追溯，
-- 也不该拿推理猜测值去填。ALTER ... IF NOT EXISTS 使重放安全。

ALTER TABLE ariadne.cost_rollup
    ADD COLUMN IF NOT EXISTS cache_write_tokens SimpleAggregateFunction(sum, UInt64) DEFAULT 0;

ALTER TABLE ariadne.cost_rollup
    ADD COLUMN IF NOT EXISTS reasoning_tokens SimpleAggregateFunction(sum, UInt64) DEFAULT 0;

-- AggregatingMergeTree 的物化视图不会自动写入新列 —— 必须重建。
-- 重建期间到分钟的聚合由下一次 span 写入补齐（MV 按插入块触发），
-- 已存在的 rollup 行保留（新列 0）。
DROP VIEW IF EXISTS ariadne.mv_cost_rollup;

CREATE MATERIALIZED VIEW ariadne.mv_cost_rollup
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
    sum(cache_write_tokens)     AS cache_write_tokens,
    sum(reasoning_tokens)       AS reasoning_tokens,
    sum(cost_usd)               AS cost_usd
FROM ariadne.spans
WHERE kind = 'llm'
GROUP BY project_id, minute, provider, model_request;
