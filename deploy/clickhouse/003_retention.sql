-- M6 Week 3: 冷热分层 + TTL
-- 幂等：可重复执行（MODIFY TTL / MODIFY SETTING 设成相同值都是空操作）。
--
-- 两级 TTL（M6 §3 + 08-data-model.md §2.1）：
--   14 天 → TO VOLUME 'cold'
--   90 天 → DELETE
-- 聚合表 trace_rollup / cost_rollup 保留 2 年（10-security.md §3.3）。
--
-- 前置条件：服务端必须已加载 deploy/clickhouse/config.d/storage.xml 定义的
-- tiered 策略。ClickHouse 没有 CREATE STORAGE POLICY 这种 DDL —— 卷和磁盘只能
-- 在配置文件里声明，纯 SQL 交付不了分层。策略不存在时下面第 1 条会直接报错，
-- 这是故意的：静默退回单层意味着热存储无上限增长，账单上才发现。

-- 1. 切到分层策略。tiered 的热卷名必须是 default（见 storage.xml 注释）。
ALTER TABLE ariadne.spans
    MODIFY SETTING storage_policy = 'tiered';

-- 2. spans 两级 TTL：TO VOLUME 用卷名 'cold'，不是磁盘名
ALTER TABLE ariadne.spans
    MODIFY TTL
        toDateTime(started_at) + INTERVAL 14 DAY TO VOLUME 'cold',
        toDateTime(started_at) + INTERVAL 90 DAY DELETE;

-- 3. trace_rollup 保留 2 年（聚合指标，低频查询）
ALTER TABLE ariadne.trace_rollup
    MODIFY TTL toDateTime(started_at) + INTERVAL 730 DAY DELETE;

-- 4. cost_rollup 保留 2 年
ALTER TABLE ariadne.cost_rollup
    MODIFY TTL toDateTime(minute) + INTERVAL 730 DAY DELETE;

-- 对象存储的生命周期规则不在这里：payloads/ 前缀 14 天转 IA、90 天删除，
-- 由 S3 侧 put-bucket-lifecycle-configuration 配置，与上面的 TTL 对齐。
