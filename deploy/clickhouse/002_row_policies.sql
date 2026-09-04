-- M6 Week 2: ClickHouse 行级安全策略
-- 幂等：可重复执行（DROP IF EXISTS + CREATE）

-- ClickHouse row policy 使用 currentSetting('ariadne.project_id')
-- 与 Postgres RLS 的 current_setting('ariadne.project_id') 对应。
-- 查询前需在 session 级别 SET ariadne.project_id = '...'。

-- 为 spans / trace_rollup / cost_rollup 创建 row policy
-- 应用层已带 WHERE project_id = ...，此策略是 DB 级兜底：
-- 即使应用层漏写 WHERE，ClickHouse 也会按 project_id 过滤。

-- 注意：row policy 在 ClickHouse 中是叠加的（AND 语义），
-- 不是替换。DEFAULT POLICY 表示无其他 policy 时默认放行。
-- 这里用 restrictive 策略：只有 project_id 匹配的行可见。

DROP POLICY IF EXISTS tenant_isolation ON ariadne.spans;
CREATE ROW POLICY tenant_isolation ON ariadne.spans
    FOR SELECT
    USING (project_id = toUUID(currentSetting('ariadne.project_id', '')))
    AS RESTRICTIVE;

DROP POLICY IF EXISTS tenant_isolation ON ariadne.trace_rollup;
CREATE ROW POLICY tenant_isolation ON ariadne.trace_rollup
    FOR SELECT
    USING (project_id = toUUID(currentSetting('ariadne.project_id', '')))
    AS RESTRICTIVE;

DROP POLICY IF EXISTS tenant_isolation ON ariadne.cost_rollup;
CREATE ROW POLICY tenant_isolation ON ariadne.cost_rollup
    FOR SELECT
    USING (project_id = toUUID(currentSetting('ariadne.project_id', '')))
    AS RESTRICTIVE;
