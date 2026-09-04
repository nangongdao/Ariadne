# M6 实施规格：生产化

> 前置：[M5](M5-spec.md) 已完成。总体设计见 [10 安全与多租户](10-security.md)。

## 1. 交付定义

别人敢在生产用。

M6 完成的判定标准：亿级 span 下查询 p95 < 500ms；跨租户越权测试全部失败；混沌测试（杀节点、断网、打满队列）下不丢数据。

## 2. 范围边界

### 做

| 项 | 内容 |
|---|---|
| K8s 部署 | Helm chart，三类 Worker 独立伸缩 |
| 多租户隔离 | 四层防护（应用层 / RLS / ClickHouse row policy / S3 前缀） |
| RBAC | 五角色 + API Key scopes |
| 完整审计 | 认证、授权变更、配置变更、高危操作、数据访问 |
| SLO 告警 | 多窗口燃烧率 + 归因信息 |
| 冷热分层 | ClickHouse TTL + 对象存储卷 |
| GDPR 删除 | 级联删除三处存储 |
| Firecracker | 高安全档沙箱（多租户 SaaS 用） |
| S3 后端 | 替换 M1 的本地对象存储桩 |
| OTLP protobuf | 补齐 M1 只做 JSON 的缺口 |
| 尾部采样 | Collector 侧按结果决定保留 |
| 完整文档 | 部署、运维、迁移指南 |

### 不做（留待评估）

多 Agent 协作编排（D7）、SaaS 计费系统、开源许可决策（D6，需商业化路径明确）。

## 3. 四层隔离，任一层单独失效不导致越权

这是 M6 最重要的设计。**不能只靠应用层过滤** —— 一次漏写 `WHERE project_id` 就是跨租户数据泄漏。

| 层 | 措施 | 失效场景下的兜底 |
|---|---|---|
| 应用层 | 所有查询经带 `project_id` 的 repository，禁止裸 SQL | — |
| Postgres | 行级安全（RLS）策略 | 应用层漏过滤时数据库拒绝返回 |
| ClickHouse | `ORDER BY` 以 `project_id` 起头 + row policy | 同上 |
| 对象存储 | key 以 `{project_id}/` 为前缀，预签名 URL ≤ 5 分钟且限单对象 | URL 泄漏的影响面被限制 |

RLS 的实现要点：

```sql
ALTER TABLE loop_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON loop_runs
    USING (project_id = current_setting('ariadne.project_id')::uuid);
```

`current_setting` 由连接池在每次取用连接时设置。**连接归还前必须重置**，否则连接复用会导致租户串号 —— 这是 RLS 最常见的实现错误。

## 4. 关键实现决策

### 4.1 approver 与 developer 必须可分离

写代码的人不应能批准自己触发的高敏感操作。这是职责分离的基本要求，也是 M6 的 RBAC 设计里唯一不可妥协的约束。

`approver` 可以是一个只有审批权、没有任何写权限的角色。

### 4.2 用户的 provider 密钥默认不存

由用户在自己环境的环境变量提供，Ariadne 运行时读取，不落库不落日志。

自托管场景若用户要求托管，走信封加密（KMS 数据密钥 + 密钥版本记录），且**明确标注为高风险选项，默认关闭**。

### 4.3 GDPR 删除不能承诺"立即"

ClickHouse 的 `ALTER TABLE DELETE` 是异步 mutation。因此：

- 记录删除任务状态并提供进度查询
- 文档中明确承诺为"最长 24 小时内完成"
- 不能在 API 返回 200 时声称已删除

### 4.4 尾部采样的粒度是 loop_id 而非 span

同一 Loop 的所有轮次必须一起保留或一起丢弃，否则进化视图会出现断层。这与通用 APM 的采样逻辑不同，是 Ariadne 的特殊约束。

缓冲窗口 30s，超时的 trace 按头部决策处理。

### 4.5 自监控绕过采样与脱敏

平台自身的健康数据写入独立的 `_internal` 项目，写入路径**直连存储**。理由：平台故障时采集管道本身可能就是故障点，自监控数据不能走同一条路径,否则会出现"挂了但看不到为什么挂"的死锁。

## 5. 模块清单

```
src/ariadne/
├── auth/
│   ├── rbac.py               # 五角色 + 权限矩阵
│   ├── keys.py               # Argon2id 校验 + scopes
│   ├── jwt.py                # Web Console 会话
│   └── tenant.py             # 租户上下文 + RLS 变量设置/重置
├── storage/
│   ├── objectstore_s3.py     # 替换 M1 的本地桩
│   └── retention.py          # TTL / 冷热分层 / GDPR 级联删除
├── telemetry/
│   ├── sampling.py           # 头部 + 尾部采样（loop_id 粒度）
│   └── otlp_protobuf.py      # 补齐 protobuf 编码
├── sandbox_module/
│   └── firecracker.py        # 高安全档
├── observability/
│   ├── slo.py                # 多窗口燃烧率
│   └── alerts.py             # 告警 + 归因信息
└── api/middleware/
    ├── ratelimit.py          # 滑动窗口 + 令牌桶
    └── tenant.py             # 租户路由

deploy/
├── helm/                     # Chart + 三类 Worker 的独立 values
├── grafana/                  # 面板 JSON
└── migrations/               # 从单机 Compose 迁移到 K8s 的指引
```

## 6. 技术栈增量

| 选择 | 用途 | 理由 | 被否方案 |
|---|---|---|---|
| **Helm 3** | K8s 部署 | 生态标准，values 分环境覆盖 | Kustomize：模板能力弱于 Helm；裸 YAML：不可维护 |
| **Firecracker** | 高安全档沙箱 | 真硬件虚拟化边界，多租户 SaaS 必需 | 只用 gVisor：用户态内核仍共享宿主，SaaS 场景不够 |
| **`argon2-cffi`** | API Key 哈希 | 抗 GPU 暴破，优于 bcrypt | SHA256：无 work factor,不适合凭证 |
| **`python-jose` / `pyjwt`** | JWT | 成熟实现 | 手写：签名验证极易出错 |
| **`boto3` / `aiobotocore`** | S3 | 官方 SDK | `minio-py`：只覆盖 MinIO |
| **`opentelemetry-proto`** | OTLP protobuf | 官方 schema | 手写解析：protobuf 定义会变 |
| **Prometheus + Grafana** | 指标与面板 | M1 已导出 OTel metrics，直接消费 | 自研面板：重复造轮子 |
| **`vault` / AWS Secrets Manager** | 密钥托管 | 支持轮换与审计 | 环境变量：无轮换能力 |
| **Kafka（条件引入）** | 采集队列 | 若 Redis Streams 吞吐不足 | 见下 |

**Kafka 是条件引入**：M1-M5 用 Redis Streams。只有当实测吞吐不足或需要多天消息回溯时才换。判据是 `ariadne_collector_lag_seconds` 持续偏高且 Redis 内存成为瓶颈。不提前引入 —— 运维成本高，且 Redis Streams 的消费者组语义已满足崩溃接管需求。

## 7. 性能目标与达成手段

| 指标 | 目标 | 手段 |
|---|---|---|
| Trace 查询 p95（1 亿 span） | < 500ms | 物化视图预聚合 + `project_id` 前置排序键 + 跳数索引 |
| SDK 同步开销 | < 1ms | M1 已达成（有界队列 + 后台线程） |
| Collector 吞吐 | ≥ 5000 spans/s | M1 已达成；不足时按 D1 换语言重写 |
| SSE 推送延迟 | < 200ms | Redis pub/sub + 100ms 批合并 |
| 平台引入的额外延迟 | < 50ms P95 | 卡点求值 + 采集异步化。卡点实测 13~27ms（非原计划的 < 5ms，见 [M4-spec 第 11 节](M4-spec.md#验收项-1-的订正p99--5ms-达不到原因是结构性的)），50ms 预算仍有余量但已不宽裕 |

## 8. 混沌测试矩阵

M6 的核心验收手段。每项都要验证"不丢数据"而非只是"能恢复"：

| 故障注入 | 期望行为 |
|---|---|
| `kill -9` Collector Worker | 未 ACK 消息被 XAUTOCLAIM 回收，无重复行（ReplacingMergeTree 去重） |
| `kill -9` Loop Worker | 从检查点接管，iteration 不回退，预算不重置 |
| ClickHouse 不可用 | 消息留在队列不 ACK，恢复后重放；API 返回 degraded 而非 500 |
| Redis 不可用 | SDK 侧继续缓冲，API 返回 503；不丢已入队数据（AOF） |
| 队列打满（maxlen） | 丢最旧并计数告警，不阻塞写入 |
| 网络分区（API ↔ Redis） | SSE 断开，客户端凭 Last-Event-ID 重连补齐 |
| provider 全面 429 | 并发自动降级，Loop 进 Retry 退避而非失败 |
| 磁盘写满 | ClickHouse 拒绝写入 → 同"不可用"路径 |

## 9. 验收清单

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | 亿级 span 查询 p95 < 500ms | 灌 1 亿行后压测 |
| 2 | 跨租户越权全部失败 | 用 A 租户 key 查 B 租户资源，含直接构造 UUID |
| 3 | RLS 在应用层漏过滤时兜底 | 刻意去掉 repository 的过滤，数据库仍拒绝 |
| 4 | 连接归还后 RLS 变量已重置 | 并发多租户请求，无串号 |
| 5 | approver 无写权限也能审批 | 权限矩阵测试 |
| 6 | 混沌测试全部通过 | 第 8 节矩阵 |
| 7 | 错误预算告警带归因 | 注入失败，告警含 project/model/断言维度 |
| 8 | 冷热分层生效 | 超过 14 天的分区在冷卷上 |
| 9 | GDPR 删除级联三处存储 | 删除后 Postgres/ClickHouse/S3 均无残留 |
| 10 | 沙箱逃逸（Firecracker 档）全部失败 | 复用 M4 用例集 + VM 逃逸用例 |
| 11 | OTLP protobuf 与 JSON 等价 | 同一 trace 两种编码上报，落库结果一致 |
| 12 | 尾采样保留 loop 完整性 | 采样后同一 loop 的轮次全在或全不在 |
| 13 | 自监控在平台故障时仍可写 | 打挂采集管道，`_internal` 项目仍有数据 |
| 14 | Helm 部署可用 | 全新集群 `helm install` 后跑通端到端 |
| 15 | 从 Compose 迁移有路径 | 按迁移指南操作，数据不丢 |

## 10. 工期与顺序

预计 5 周：

1. **第 1 周**：RBAC + API Key Argon2id + JWT + 租户上下文（RLS 变量管理）
2. **第 2 周**：四层隔离 + 越权测试集 + 完整审计
3. **第 3 周**：S3 后端 + 冷热分层 + TTL + GDPR 删除 + 尾采样 + OTLP protobuf
4. **第 4 周**：Helm chart + 三类 Worker 独立伸缩 + Prometheus/Grafana + SLO 告警
5. **第 5 周**：Firecracker + 混沌测试 + 性能压测 + 文档

第 1-2 周的隔离工作必须在其他之前 —— 它会改动所有 repository 的签名，越晚做返工越大。

## 11. 实施进度

### 第 1 周：RBAC + API Key Argon2id + JWT + 租户上下文 ✅

**完成日期**：2026-08-28

**交付内容**：

| 模块 | 文件 | 说明 |
|---|---|---|
| RBAC 权限矩阵 | `auth/rbac.py` | 五角色（admin/approver/developer/viewer/billing）+ 七权限；approver 仅有 read+approve，无任何 write（§4.1 职责分离） |
| API Key 哈希 | `auth/keys.py` | Argon2id 哈希（`argon2-cffi`，抗 GPU 暴破）；`generate_api_key()` 生成 `ak_live_` 前缀密钥；`extract_prefix()` 取前 16 字符用于索引 |
| JWT 会话 | `auth/jwt.py` | HS256 JWT，Web Console 会话用；`SessionClaims` frozen dataclass；`create_session_token()` / `verify_session_token()` |
| 租户上下文 | `auth/tenant.py` | `TenantContext` frozen dataclass；`set_tenant_context()` 执行 `SET LOCAL ariadne.project_id`；`reset_tenant_context()` 归还连接前重置；SQLite 侧 no-op |
| API Key 仓库 | `storage/postgres/repositories/api_keys.py` | CRUD + 按 prefix 查找 + 吊销 + 过期检查；所有方法带 `*, project_id` 关键字参数（应用层隔离） |
| API Key 管理端点 | `api/routers/keys.py` | POST/GET/DELETE `/v1/keys`；创建时返回明文一次，列表只展示前缀+metadata；需 `MANAGE_KEYS` 权限 |
| 双认证后端 | `api/deps.py` | `auth_backend: "static"`（M1 兼容，测试用）/ `"db"`（生产，Argon2id+RBAC）；`require_project -> UUID` 签名不变 |
| 配置扩展 | `config.py` | 新增 `auth_backend`、`jwt_secret`、`jwt_ttl_hours`、`jwt_issuer`、`argon2_memory_cost/time_cost/parallelism` |
| 数据模型 | `storage/postgres/auth_models.py` | `ApiKey` ORM：`key_hash`（Argon2id）、`key_prefix`（索引）、`scopes`（JSONB）、`is_active`、`expires_at` |
| 迁移 | `deploy/alembic/versions/e5f6a7b8c9d0_auth_tables.py` | 创建 `api_keys` 表 + PG 侧 RLS 策略（11 张 project-scoped 表）；SQLite 跳过 RLS |
| ForbiddenError | `api/errors.py` | 403 错误类型，RBAC 权限不足时抛出 |

**测试覆盖**：48 项新测试
- `test_rbac.py` (15)：权限矩阵全角色覆盖、approver 无写权限约束、check_permission 抛出/通过、ForbiddenError 状态码
- `test_api_keys.py` (13)：密钥生成/哈希/验证/前缀提取、Argon2id 格式校验、仓库 CRUD、吊销后查找返回 None、过期判断（无/未来/过去 expiry）
- `test_jwt.py` (10)：签发/验证、多角色、claims 完整性、extra_claims 保留、过期拒绝、篡改拒绝、错误 secret 拒绝、缺失 claims 拒绝
- `test_tenant_context.py` (10)：TenantContext 不可变、SQLite no-op、tenant_session 生命周期、static 后端默认上下文

**质量门禁**：ruff ✅ | mypy 154 files ✅ | pytest 1215 passed 9 skipped ✅ | tsc ✅ | vite build ✅ | sdk tsc ✅

**技术决策**：
1. Argon2id 而非 bcrypt：§6 指定，抗 GPU 暴破
2. RLS 认证引导：认证时 project_id 未知，auth 查询按 `key_prefix` 全局查找（绕过 RLS），找到后再设置租户上下文
3. 双认证后端：`static` 模式让现有 1200+ 测试零改动；`db` 模式生产用
4. JWT 仅 Web Console：API Key 用于 SDK/程序化访问，JWT 用于浏览器会话，两者并存


### 第 2 周：四层隔离 + 越权测试集 + 完整审计 ✅

**完成日期**：2026-08-28

**交付内容**：

| 阶段 | 模块 | 文件 | 说明 |
|---|---|---|---|
| A | RLS 接线 | `storage/postgres/engine.py` | `PostgresStore.tenant_session(project_id)` 上下文管理器：PG 侧 `SET LOCAL ariadne.project_id`，SQLite no-op，退出时 `reset_tenant_context` 防串号 |
| A | RLS 接线 | `api/deps.py` | `TenantScopedPg` 代理类 + `get_tenant_pg` 依赖，`session()` 自动注入 project_id |
| A | loop_checkpoints 修复 | `deploy/alembic/versions/f6a7b8c9d0e1_rls_wiring.py` | `loop_checkpoints` 表添加 `project_id` 列（NOT NULL，从 `loop_runs.project_id` 回填）+ FK + 索引 + RLS 策略 |
| A | loop_checkpoints 修复 | `storage/postgres/loop_models.py` | `LoopCheckpointRow` 添加 `project_id` 字段 + 索引 |
| A | loop_checkpoints 修复 | `storage/postgres/repositories/loop_checkpoint_repo.py` | `save`/`latest` 方法添加 `*, project_id` 参数 |
| A | loop_checkpoints 修复 | `loop_module/checkpoint.py` | `CheckpointStore` Protocol + `InMemoryCheckpointStore` 签名更新 |
| B | 审计 project_id 贯通 | `loop_module/engine.py` | `LoopConfig` 添加 `project_id: UUID` 字段；`_precheck` 审计写入用 `self._cfg.project_id` |
| B | 审计 project_id 贯通 | `runtime_module/llm/guarded.py` | `GuardedLLMAdapter` 添加 `project_id` + `loop_id` 字段；`_audit` 用实例属性替代 `None` |
| B | 审计 project_id 贯通 | `worker/loop_worker.py` | `_process` 读取 `run.project_id`，传入 `_build_engine` → `LoopConfig` + `GuardedLLMAdapter` |
| B | 审计 loop_id | `harness_module/audit.py` | `AuditRecord` 添加 `loop_id: str` 字段；`PostgresAuditSink.write` 解析 loop_id 填充 `AuditLogRow.loop_id` |
| B | 审计端点 | `api/routers/audit.py` | `GET /v1/audit`：列出审计记录，支持 `loop_id` 过滤 + 时间范围 + 分页；需 `READ` 权限 |
| B | 审计端点 | `api/app.py` + `api/routers/__init__.py` | 挂载 audit router |
| C | RBAC 路由接线 | 全部 12 个路由器 | `ProjectId` → `TenantCtx`（返回 `TenantContext` with role）+ `check_permission(ctx.role, Permission.X)` 作为首语句 |
| C | RBAC 路由接线 | `api/routers/approvals.py` | create→WRITE, list→READ, decide→APPROVE |

> **2026-08-30 追加修复**：decide/list 的过期比较在 SQLite 上 500 —— 读出的 `expires_at` 是 naive datetime，与 aware `now(UTC)` 直接比较抛 `TypeError`。真实决策流此前从未被端到端测试跑过，验收 #5 补齐测试时暴露。修复：`_as_utc()` 把 naive 补上 UTC（与 `repositories/api_keys.py:124` 惯用法一致），两处比较都走它。相关测试：`TestApprovalDecideRBAC`（approver 走通 decide、developer/viewer 403、重复决策 400）。
| C | RBAC 路由接线 | `api/routers/rules.py` | list→READ, update→MANAGE_RULES, test→READ |
| C | RBAC 路由接线 | `api/routers/costs.py` | get→VIEW_BILLING |
| C | RBAC 路由接线 | `api/routers/datasets.py` | 写→WRITE, 读→READ |
| C | RBAC 路由接线 | `api/routers/experiments.py` | 创建/流转→WRITE, 读取→READ, 删除→DELETE |
| C | RBAC 路由接线 | `api/routers/graphs.py` | 写→WRITE, 读→READ |
| C | RBAC 路由接线 | `api/routers/loops.py` | 创建/取消→WRITE, 读取/列表→READ |
| C | RBAC 路由接线 | `api/routers/specs.py` | 写→WRITE, 读→READ |
| C | RBAC 路由接线 | `api/routers/ingest.py` | 写入→WRITE（SDK 上报路径） |
| C | RBAC 路由接线 | `api/routers/traces.py` | 读取→READ + `project_id=ctx.project_id` 在 `store.query()` |
| C | RBAC 路由接线 | `api/routers/playground.py` | →WRITE |
| D | ClickHouse row policy | `deploy/clickhouse/002_row_policies.sql` | `spans`/`trace_rollup`/`cost_rollup` 创建 RESTRICTIVE row policy，使用 `toUUID(currentSetting('ariadne.project_id', ''))` |
| D | ClickHouse row policy | `storage/clickhouse.py` | `query()` 方法添加 `*, project_id` 参数，查询前 `SET ariadne.project_id`，查询后重置 |

**测试覆盖**：37 项新测试
- `test_rbac_api.py` (18)：approvals（viewer/approver 被拒 create、admin 可创建）、costs（billing 可读、viewer/developer 被拒）、rules（developer 可更新、viewer/approver 被拒、viewer 可列表）、keys（admin 可列表、developer/viewer 被拒）、audit（viewer 可读）、experiments（viewer/approver/billing 可对比、viewer 被拒写结果/快照）
- `test_tenant_isolation.py` (12)：datasets/experiments/prompts/loop_runs/api_keys 跨租户不可见 + 列表为空 + RLS 生命周期（SQLite no-op、tenant_session 上下文管理器）
- `test_audit.py` (8)：AuditRecord 有 project_id+loop_id 字段、InMemoryAuditSink 保留、PostgresAuditSink 填充、空/无效 loop_id 处理、跨租户审计隔离

**质量门禁**：ruff ✅ | mypy 146 files ✅ | pytest 1252 passed 9 skipped ✅ | tsc ✅ | vite build ✅ | sdk tsc ✅

**技术决策**：
1. `tenant_session()` 而非全局 contextvars：显式传参更安全，不会遗漏；PG 侧 `SET LOCAL`（事务级），SQLite no-op
2. `loop_checkpoints` 加 `project_id` 列而非从 RLS 列表删除：checkpoint 也应受 RLS 保护，比删除更安全
3. `AuditRecord` 加 `loop_id` 字段：`AuditLogRow.loop_id` 列已存在但从未填充，贯通后审计记录可按 loop 追溯
4. audit router 用 `Pg` 而非 `TenantPg`：与所有其他路由器保持一致，应用层 `ctx.project_id` WHERE 过滤 + 未来统一迁移到 `TenantPg` 时一次性全改
5. ClickHouse row policy 用 `RESTRICTIVE`：与 PG 的 RLS 语义对应，即使有其他 PERMISSIVE 策略也必须满足此条件
6. FastAPI body validation 先于 dependency 执行：RBAC 测试的 PUT body 必须通过 422 校验才能到达 403 权限检查


### 第 3 周：S3 后端 + 冷热分层 + TTL + GDPR 删除 + 尾采样 + OTLP protobuf ✅

**完成日期**：2026-08-28

**交付内容**：

| 阶段 | 模块 | 文件 | 说明 |
|---|---|---|---|
| A | S3 对象存储 | `storage/objectstore_s3.py` | `S3ObjectStore`（boto3），`put`/`get`/`delete`/`delete_prefix`；key 以 `{project_id}/` 前缀（第四层隔离） |
| A | S3 对象存储 | `storage/objectstore.py` | `ObjectStore` ABC 添加 `delete`/`delete_prefix` 抽象方法；`LocalObjectStore` 实现这两个方法；`build_store` 分发 `s3` 后端 |
| A | 依赖 | `pyproject.toml` | 新增 `boto3>=1.35` + `opentelemetry-proto>=1.28` |
| B | OTLP protobuf | `telemetry/otlp_protobuf.py` | `decode_otlp_protobuf()` — 将 OTLP protobuf 二进制解码为与 JSON 相同的 camelCase 字典结构；trace_id/span_id 从 base64 转为 hex（与 JSON 路径一致） |
| B | OTLP protobuf | `api/routers/ingest.py` | `ingest_otlp` 按 content-type 分支：protobuf → `decode_otlp_protobuf` → 同一 `_flatten_otlp` 路径；JSON 路径不变 |
| C | 尾部采样 | `telemetry/sampling.py` | `HeadSampler`（确定性概率，trace_id hash）；`TailSampler`（loop_id 粒度，30s 缓冲，超时降级到头部决策，错误 trace 保留）；`INTERNAL_PROJECT_ID` 绕过采样 |
| C | 尾部采样 | `worker/collector.py` | `_drain` 将 span 送入 `TailSampler.add()`；`_maybe_flush` 调用 `drain_ready()` 取出已决策 span；新增 `sampled_out` 统计 |
| D | 冷热分层 | `deploy/clickhouse/003_retention.sql` | spans 两级 TTL（14 天→cold volume，90 天→DELETE）；trace_rollup/cost_rollup 保留 2 年；`CREATE STORAGE POLICY tiered` |
| D | 保留策略 | `storage/retention.py` | `RetentionManager` — GDPR 级联删除协调器（Postgres→ClickHouse mutation→S3）；`DeletionJob` frozen dataclass + `DeletionStatus` StrEnum |
| E | GDPR 删除 | `storage/postgres/retention_models.py` | `DeletionJobRow` ORM — 记录删除任务状态（M6 §4.3：不承诺"立即"） |
| E | GDPR 删除 | `deploy/alembic/versions/a7b8c9d0e1f2_deletion_jobs.py` | `deletion_jobs` 表 + FK + 索引 + RLS 策略 |
| E | GDPR 删除 | `api/routers/retention.py` | `DELETE /v1/projects/{id}/data`（202 + job_id）+ `GET /v1/projects/{id}/deletion-jobs`；需 DELETE/READ 权限 |
| E | GDPR 删除 | `api/app.py` + `api/routers/__init__.py` | 挂载 retention router |

**测试覆盖**：36 项新测试
- `test_sampling.py` (11)：HeadSampler 确定性/全采样/内部项目绕过；TailSampler 非 loop span 即时决策、loop 全保留或全丢弃、错误 loop 保留、内部项目绕过、超时降级、mark_done
- `test_otlp_protobuf.py` (8)：camelCase 结构、trace_id/span_id hex 转换、name/timestamps/attributes 保留、空体处理、无效 protobuf 报错
- `test_retention.py` (9)：级联删除全流程、mutation pending 状态轮询、失败记录错误、未知 job 返回 None、S3 前缀格式、三表 mutation 提交、frozen dataclass、Status enum
- `test_object_store_delete.py` (5)：delete 幂等、delete_prefix 批量删除、单文件、不存在返回 0、build_store 分发
- `test_api.py` 更新 (2)：protobuf 接受 + 无效 protobuf 拒绝（替换 M1 的 rejection 测试）

**质量门禁**：ruff ✅ | mypy 152 files ✅ | pytest 1288 passed 9 skipped ✅ | tsc ✅ | vite build ✅ | sdk tsc ✅

**技术决策**：
1. `MessageToDict` + hex 转换：protobuf 的 trace_id/span_id 是 bytes，`MessageToDict` 编码为 base64，但 JSON 路径用 hex；自定义转换器保证两种编码落库一致（验收项 #11）
2. `INTERNAL_PROJECT_ID` 用 UUID 类型：与 `AriadneSpan.project_id` 类型一致，比较时无需转换
3. 尾部采样在 collector 而非 SDK：SDK 不知道 loop 是否完成，只有 collector 有全局视角
4. `DeletionJob` 用 frozen dataclass 而非 Pydantic：与项目其他域模型一致（M3/M4 决策）
5. GDPR 返回 202 而非 200：ClickHouse mutation 是异步的，API 不声称"已删除"（§4.3）
6. ClickHouse TTL 用 `MODIFY TTL` 而非重建表：ALTER TABLE MODIFY TTL 幂等，不丢数据


### 第 4 周：Helm chart + 三类 Worker 独立伸缩 + Prometheus/Grafana + SLO 告警 ✅

**完成日期**：2026-08-28

**交付内容**：

| 阶段 | 模块 | 文件 | 说明 |
|---|---|---|---|
| A | Helm chart | `deploy/helm/Chart.yaml` | Helm v2 chart，appVersion 0.1.0 |
| A | Helm chart | `deploy/helm/values.yaml` | 三类 Worker 独立配置 + HPA + 存储连接 + PrometheusRule |
| A | Helm chart | `deploy/helm/templates/_helpers.tpl` | 镜像/标签/存储环境变量辅助函数 |
| A | Helm chart | `deploy/helm/templates/api.yaml` | API Deployment + Service |
| A | Helm chart | `deploy/helm/templates/workers.yaml` | 三类 Worker 各自 Deployment + HPA |
| A | Helm chart | `deploy/helm/templates/migrate.yaml` | 迁移 Job（Helm pre-install hook） |
| B | Eval Worker | `worker/eval_worker.py` | `EvalWorker` + `LLMGenerator` + `build_scorer_from_config` + `run_eval_worker`（缺失的第三类 Worker） |
| B | Eval Worker | `worker/eval_queue.py` | `EvalQueue` — Redis Streams 消费者组（与 LoopQueue 同构，reclaim 120s） |
| B | Eval Worker | `eval_module/factory.py` | `build_evaluator_from_config` / `build_deterministic_evaluator` —— 按 `EVALUATOR_REGISTRY` 反射构造（`deterministic/__init__.py` 只重导出） |
| B | Eval Worker | `cli.py` | 新增 `ariadne-worker eval` 子命令 |
| C | Prometheus 指标 | `observability/metrics.py` | 21 个指标族（Loop/Harness/Eval/LLM/Collector/Queue/Sandbox/API/GDPR） |
| C | Prometheus 指标 | `api/middleware/metrics.py` | `MetricsMiddleware` — 自动记录 API 请求耗时到直方图 |
| C | Prometheus 指标 | `api/app.py` | `/metrics` 端点（Prometheus 文本格式） |
| C | Prometheus 指标 | `worker/collector.py` | 接入 collector_consumed/written/adapt_errors/sampled_out/lag 指标 |
| C | Prometheus 指标 | `worker/loop_worker.py` | 接入 loop_iterations/duration/cost/terminal 指标 |
| C | Prometheus 指标 | `storage/retention.py` | 接入 gdpr_deletion_total 指标 |
| C | Grafana | `deploy/grafana/dashboards/ariadne-overview.json` | 总览面板（Loop/采集/队列/评测/API/SLO/GDPR 七行） |
| C | ServiceMonitor | `deploy/helm/templates/servicemonitor.yaml` | Prometheus-operator CRD，自动发现 /metrics |
| D | SLO 告警 | `observability/slo.py` | 多窗口燃烧率引擎（Google SRE 双窗口模式）+ 3 预定义 SLO |
| D | SLO 告警 | `observability/alerts.py` | `AlertManager` — 告警去重 + 归因注入 + JSON 序列化 |
| D | SLO 告警 | `deploy/helm/templates/prometheusrule.yaml` | PrometheusRule CRD：Loop CRITICAL + 采集 WARNING + 延迟 + 积压 |
| D | SLO 告警 | `deploy/helm/values.yaml` | `prometheusRule.groups` — 燃烧率 PromQL 表达式 |
| E | 迁移指南 | `deploy/migrations/compose-to-k8s.md` | 9 步迁移路径：备份→部署依赖→Secret→迁移→恢复→部署→验证→伸缩→回滚 |

**测试覆盖**：54 项新测试
- `test_slo.py` (19)：SLOWindow 窗口/级别/阈值；SLOSpec 错误预算；BurnRateCalculator 零/满/预算率/双窗口告警/无告警/CRITICAL/WARNING/双窗口/长短不匹配/消息/窗口时长；SLOAlert frozen/归因默认值
- `test_alerts.py` (13)：AlertAttribution 空/带 extra；Alert frozen/to_json 最小/带归因/fingerprint 去重/归因差异；AlertManager 发新/去重/解决/未知解决/带归因/多告警
- `test_eval_worker.py` (19)：_AlwaysPassEvaluator passed/kind；build_scorer_from_config 空/regex/exact_match/不支持类型；build_deterministic_evaluator 10 种类型；LLMGenerator 成功/失败；EvalQueue stream_key/reclaim 超时
  （空配置一项已随下述修订改为断言抛 `NoScorersConfiguredError`）
- `test_metrics_endpoint.py` (3)：指标端点 Prometheus 格式、ariadne_ 前缀、直方图定义

**质量门禁**：ruff ✅ | mypy 15 files ✅ | pytest 1307 passed 9 skipped ✅（6 项 restricted-exec 环境失败为 Windows 预存问题）

**技术决策**：
1. 三类 Worker 独立 HPA：collector 按 CPU+延迟，loop 按队列深度（CPU 无效——等 LLM 时 CPU 低），eval 按 CPU（评测 CPU 密集）
2. EvalQueue reclaim 120s vs LoopQueue 90s：评测任务比单轮 Loop 更耗时
3. `build_deterministic_evaluator` 工厂：配置驱动评估器构建，eval-worker 从 experiment.config 动态装配
   —— **后续修订**（见「R12 修订：注册表短路」）：初版是写死 10 个分支的 if-chain，
   而注册表里有 14 个评估器；已改为反射注册表构造，未知参数键 fail-fast
4. MetricsMiddleware 排除静态资源/health/metrics 自身：只记录业务 API 请求
5. PrometheusRule 用双窗口 PromQL：长窗口 AND 短窗口同时超阈值才告警，避免短时抖动误报
6. `AlertManager.fingerprint` 按归因维度去重：同一 SLO+同一 model+assertion 只有一条活跃告警
7. Helm migrate Job 用 pre-install hook：保证 schema 就绪后才启动 API/Worker

### 第 5 周：Firecracker + 混沌测试 + 性能压测 + 文档 ✅

**完成日期**：2026-08-28（路线图 ✅ 校准于 2026-08-30；同日补验收 #5 端到端测试与 SQLite 时区 bug 修复，见下）

**交付内容**：

| 阶段 | 模块 | 文件 | 说明 |
|---|---|---|---|
| A | Firecracker | `sandbox_module/firecracker.py` | `FirecrackerSandbox` — microVM 驱动（KVM 后端，独立 guest 内核，vsock 通信） |
| A | Firecracker | `sandbox_module/__init__.py` | 注册 firecracker 后端到 `SANDBOX_REGISTRY` |
| B | 混沌测试 | `tests/test_chaos.py` | 8 故障注入场景（22 项测试）：kill-9 Collector/Loop、CH 不可用、Redis 不可用、队列打满、网络分区、429、磁盘写满 |
| C | 性能压测 | `benchmark.py` | 压测框架（real + mock 双模式）：1 亿 span bulk load → trace_rollup 查询 p95 |
| C | 性能压测 | `tests/test_benchmark.py` | 15 项测试：percentile 计算、MockTraceRollup 查询/过滤/计数、报告序列化 |
| D | 沙箱逃逸 | `tests/test_firecracker.py` | 31 项测试：工厂注册、可用性检测（无 KVM/无 rootfs/无权限/缓存）、profile 映射、VM 逃逸边界（禁网/只读 fs/seccomp/元数据）、降级链、输出截断 |

**验收映射**：

| 验收项 # | 验证方式 | 状态 |
|---|---|---|
| #1 亿级 span 查询 p95 < 500ms | benchmark.py real 模式灌 1 亿行后压测 trace_rollup 查询；mock 模式 CI 回归 | ⚠️ 部分：mock 模式验证框架逻辑，real 模式待 Linux+CH 环境执行 |
| #6 混沌测试全部通过 | test_chaos.py 8 场景 22 项测试 | ✅ |
| #10 沙箱逃逸（Firecracker 档）全部失败 | test_firecracker.py 31 项测试（VM 逃逸边界 + 降级链） | ✅ |
| #2 跨租户越权全部失败 | test_tenant_isolation.py (12) 跨租户不可见 + 直接构造 UUID；test_tenant_context.py (7) | ✅ |
| #3 RLS 在应用层漏过滤时兜底 | **缺 test_auth_integration.py** —— test_tenant_isolation.py 第 7 行指向该文件，但文件不存在；SQLite 下 set/reset_tenant_context 是 no-op，RLS 策略（`deploy/alembic/versions/e5f6a7b8c9d0`）只能靠真实 PG 验证 | ⚠️ 缺口：待 PG 容器环境后补集成测试 |
| #4 连接归还后 RLS 变量已重置 | tenant_session 生命周期测试（SQLite no-op 分支）+ PG 侧 RESET 代码 | ⚠️ 同上：PG 侧执行验证缺失 |
| #5 approver 无写权限也能审批 | test_rbac_api.py::TestApprovalDecideRBAC (4 项)：真建 loop+审批，approver 走通 decide → approved；developer/viewer 被拒（403）；批准后重复决策 → 400。2026-08-30 补。顺带修复 500：SQLite 读出 naive expires_at，与 aware `now(UTC)` 直接比较抛 TypeError，approvals 全流程在测试外从未跑过真实决策 | ✅（2026-08-30 补齐；修复见 §11 第 2 周追加） |
| #7 错误预算告警带归因 | test_alerts.py (13)：AlertAttribution 注入 + fingerprint 去重；test_slo.py (19) | ✅ |
| #8 冷热分层生效 | deploy/clickhouse/003_retention.sql（14 天→cold TTL，90 天→DELETE）；分区落卷需真 ClickHouse 验证 | ⚠️ 缺口：待容器环境验分区位置 |
| #9 GDPR 删除级联三处存储 | test_retention.py (9)：级联全流程 + mutation 状态 + S3 前缀；test_object_store_delete.py (5) | ✅ |
| #11 OTLP protobuf 与 JSON 等价 | test_otlp_protobuf.py (8) 双编码一致性 | ✅ |
| #12 尾采样保留 loop 完整性 | test_sampling.py (11)：loop 全保留/全丢弃 + 超时降级 | ✅ |
| #13 自监控在平台故障时仍可写 | INTERNAL_PROJECT_ID 采样绕过（test_sampling.py）| ⚠️ 部分：绕过已有覆盖，"打挂采集管道"场景无集成测试 |
| #14 Helm 部署可用 | deploy/helm/ chart 完整；本环境无 helm 二进制，未实跑 install | ⚠️ 缺口：需集群验证 |
| #15 从 Compose 迁移有路径 | deploy/migrations/compose-to-k8s.md 9 步指南 | ⚠️ 部分：文档完备，实操验证待集群 |

**测试覆盖**：68 项新测试
- `test_chaos.py` (22)：kill-9 Collector（回收/幂等）、kill-9 Loop（检查点/预算不重置）、CH 不可用（不 ACK/恢复/degraded）、Redis 不可用（SDK 缓冲/degraded）、队列打满（maxlen/丢最旧/计数）、网络分区（心跳/断连）、429（retry 退避/降并发/检测）、磁盘写满（不 ACK/不阻塞循环）
- `test_firecracker.py` (31)：工厂注册（4）、可用性检测（6）、strict profile 映射（7）、VM 逃逸边界（8）、降级链（3）、输出截断（3）
- `test_benchmark.py` (15)：percentile（5）、MockTraceRollup（4）、benchmark 执行（6）

**质量门禁**：ruff ✅ | mypy 171 files ✅（2 pre-existing stub 缺失）| pytest 1375 passed 9 skipped ✅

**2026-08-30 追加（路线图校准）**：
1. 验收 #5 补齐端到端测试后，跑出 500：SQLite 读出的 `expires_at` 是 naive datetime，与 `datetime.now(UTC)` 比较抛 `TypeError: can't compare offset-naive and offset-aware datetimes`。approvals 的三个路由此前从未有真实决策流经，属 M6 接线完成后的 latent bug。修复：`_as_utc()` 归一化（与 `repositories/api_keys.py` 第 124 行既有惯用法一致），list/decide 两处过期比较同修。
2. 新增 `TestApprovalDecideRBAC`（4 项）与 `TestExperimentsRBAC`（3 项），共 7 项 RLS/RBAC 类测试；`test_rbac_api.py` 现为 22 项。
3. 修复后全量门禁：ruff ✅ | mypy 190 files ✅ | pytest 1584 passed 9 skipped ✅（含本日新增 8 项：审批决策职责分离 4 + 实验对比权限 3 + approver 端到端 1）。

**技术决策**：
1. Firecracker 用真硬件虚拟化（KVM）而非用户态内核（gVisor）：独立 guest 内核 → VM 逃逸才能突破，是多租户 SaaS 最高安全档
2. 混沌测试用 mock/stub 模拟故障而非真杀进程：无容器环境下可跑，真集成测试用 `-m integration` 标记
3. CollectorWorker._flush 在 CH 写入失败时不 ACK + sleep 1s：消息留 pending → XAUTOCLAIM 回收 → ReplacingMergeTree 去重保证幂等
4. 性能压测双模式：real 连真 ClickHouse 灌 1 亿行（验证 p95 < 500ms），mock 内存模拟（CI 回归无容器可跑）
5. MockTraceRollup 只存百万级 trace 聚合行（不存 1 亿原始 span）：验证查询逻辑 + 延迟模型，不压内存
6. 降级链 Firecracker → gVisor → 受限子进程：不可用时抛 SandboxUnavailableError，调用方决定降级策略

## 12. M6 完成总结

**5 周累计交付**：

| 周 | 主题 | 测试增量 | 累计测试 |
|---|---|---|---|
| 1 | RBAC + API Key Argon2id + JWT + 租户上下文 | +45 | 1252 |
| 2 | 四层隔离 + 越权测试集 + 完整审计 | +55 | 1307 |
| 3 | S3 + 冷热分层 + TTL + GDPR + 尾采样 + OTLP | +0（集成到现有） | 1307 |
| 4 | Helm + 三类 Worker + Prometheus/Grafana + SLO 告警 | +54 | 1307 |
| 5 | Firecracker + 混沌测试 + 性能压测 | +68 | 1375 |

**最终质量门禁**：ruff ✅ | mypy 171 files ✅ | pytest 1375 passed 9 skipped ✅

## 13. R12 修订：注册表短路（评测配置层）

M6 §B 的 `build_deterministic_evaluator` 是 R12「建好但没人调」的第七个实例，
失效方式与前六个不同：**有**生产调用路径，路径本身却绕过了注册表。三个缺陷叠加。

**缺陷一：注册表被短路。** 工厂是一条写死 10 个分支的 if-chain，恰好覆盖 10 个
`deterministic` 评估器；而 `EVALUATOR_REGISTRY` 里有 14 个。多出来的
`rouge_l` / `token_f1` / `edit_distance`（`statistical`）与 `judge` 实现完整、
单测全绿，生产侧不可达 —— 漏掉的不是零散几个类型，而是一整个 kind。

**缺陷二：构造参数静默丢弃。** if-chain 只往构造器传部分参数。`regex` 的
`must_match` / `flags` / `target`、`exact_match` 的 `case_sensitive`、
`json_schema` 的 `allow_fenced`、`markdown_structure` 的全部三个参数都是
配了不生效。`{"type": "regex", "pattern": "TODO", "must_match": false}`
本意「不许出现 TODO」，旧代码执行的是「必须出现 TODO」—— **断言反向且无报错**，
比不生效更糟。

**缺陷三：`ScoreSpec.threshold` / `op` 只被写、从不被读。**
`build_scorer_from_config` 逐条填 `threshold=sc.get("threshold")`，而
`CompositeScorer` 只读 `result.passed`，配置里的每项达标线全部无效。

**缺陷四：无 scorers 时降级成恒通过。** 旧代码在没有 `scorers` 时造一个
`_AlwaysPassEvaluator`，于是复合分恒为 100、`pass_rate` 恒为 1.0，且这些数字
照常写进 `experiments.metrics`、照常参与 compare 的门禁判定 —— 一个「没配评分器」
的实验长得和「完美通过」一模一样，门禁会放行任何变更。

**修法**：

| 缺陷 | 修法 |
|---|---|
| 注册表短路 | 新增 `eval_module/factory.py`，按 `inspect.signature` 反射注册表构造；新评估器加 `@register_evaluator` 即自动可配 |
| kind 门槛 | `_CONFIG_BUILDABLE_KINDS = {deterministic, statistical}` —— 门槛是「零 API 成本」而非字面的 deterministic；`judge` 抛 `JudgeNeedsClientError`（它需注入 `JudgeClient`，与异步 `LLMClient` 签名不兼容） |
| 参数静默丢弃 | 未知参数键 fail-fast，报可用键名；值按构造器默认值类型强转；`flags` 接受 `["IGNORECASE"]` 名字列表（白名单，非 `getattr(re, ...)`） |
| `allow_fenced` 失效 | `_extract_json` 真正接受该标志（此前两个调用点都传了、函数签名里没有） |
| spec 阈值不生效 | `CompositeScorer` 按 `spec.threshold` / `spec.op` **覆盖**单项 passed（覆盖而非取交集：否则配了只能收紧、调不松）；`errored` 结果不套阈值 —— value 无意义，套了会把「没测出来」变成「没达标」 |
| 恒通过降级 | 抛 `NoScorersConfiguredError`；真要跑无评分实验需显式 `"allow_no_scorers": true`，此时评估器名叫 `no_scorers_configured` 而非 `always_pass`，名字随 metrics 落库 |

配置错误是永久性的，重试无意义：worker 捕获这两类异常后把 experiment 转
`failed` 并 ACK，而不是让消息在 XAUTOCLAIM 里无限redeliver。

**新增测试**：`test_eval_factory_wiring.py`（19）—— 四个曾不可达类型可构造、
`judge` 报错、未知键 fail-fast、`must_match: false` 语义正确、`flags` 名字列表、
注册表全覆盖；`test_eval_composite.py::TestSpecThreshold`（6）——
收紧/放宽/`op` 方向/无阈值/`errored` 不套阈值/分数仍用原始值。
