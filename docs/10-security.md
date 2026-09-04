# 10 安全与多租户

## 1. 威胁模型

Ariadne 的特殊风险面：它**代表用户执行不可信代码、调用外部 API、并存储可能含敏感数据的 prompt**。主要威胁：

| 威胁 | 攻击面 | 缓解 |
|---|---|---|
| 跨租户数据泄漏 | 查询未带租户过滤 | 强制 `project_id` 过滤 + Postgres RLS |
| 沙箱逃逸 | 执行用户代码 | gVisor/Firecracker + seccomp + 无挂载 |
| Prompt 注入导致工具滥用 | 用户输入进入 LLM 后触发工具调用 | `ExecPolicy` argv[0] 白名单（M3）+ Harness `pre_tool` 卡点（已接线，见 docs/04 第 3 节） |
| 凭证泄漏 | prompt/日志中出现 API Key | 双层脱敏；SDK 运行时密钥不落库 |
| 预算耗尽（经济型 DoS） | 恶意触发大量 Loop | 三层熔断 + 限流 + 配额 |
| SSRF | 工具发起任意网络请求 | 域名白名单 + 禁云元数据端点 |
| 规则引擎滥用 | 租户上传的规则表达式 | CEL 沙箱（无副作用、有求值上界） |

## 2. 多租户隔离

**设计目标是四层防护、任一层单独失效不导致越权。当前实现只有应用层真正生效** ——
第 2 层（RLS）在现行部署下被静默绕过，原因见下方"RLS 当前不生效"。

1. **应用层**：所有查询强制经过带 `project_id` 的 repository 层，禁止裸 SQL 拼接。代码评审重点检查这一条。已生效。
2. **数据库层**：Postgres 行级安全（RLS）作为兜底 —— 即使应用层漏了过滤，数据库也拒绝返回。**策略已建但当前不生效**。

```sql
ALTER TABLE loop_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON loop_runs
    USING (project_id = current_setting('ariadne.project_id')::uuid);
```

3. **ClickHouse**：`ORDER BY` 以 `project_id` 起头，查询构造器强制注入过滤条件；同时用 ClickHouse 的 row policy 兜底。
4. **对象存储**：S3 key 以 `{project_id}/` 为前缀，预签名 URL 的有效期 ≤ 5 分钟且限定单对象。

**沙箱层隔离**：每个 Loop 的沙箱实例用完即毁、不复用，避免跨租户状态残留；`strict` 档用 Firecracker 提供硬件级边界。

### 2.1 RLS 当前不生效

策略 SQL 已经在迁移里建好，`set_tenant_context` 也会设 GUC，但 PG 的 RLS 有三条
静默绕过路径，现行部署同时踩中两条：

| 绕过路径 | 现状 |
|---|---|
| 超级用户 | `docker-compose` 里 app 与 migrate 共用 `ariadne` 用户，该用户是 superuser |
| `BYPASSRLS` 属性 | 未显式授予，不构成问题 |
| 表 owner 且未 `FORCE ROW LEVEL SECURITY` | `ariadne` 是所有表的 owner，迁移里未设 FORCE |

后果：策略存在但从不参与判定，**应用层过滤是唯一防线**。若某个 repository
漏写 `project_id` 过滤，会直接跨租户返回数据，不会被数据库拦下。

修复方向（待办 #15/#18）：应用连接改用只有 DML 权限的非 owner 角色（`ariadne_app`），
迁移专用角色（`ariadne`）保留 DDL 权限但只在 Alembic 里用。这样 API / Worker 连接时
的身份不满足任何绕过条件，RLS 真正生效。

**进展**（2026-09-02/03）：
- ✅ 迁移增加 `ariadne_app` 角色（非 owner，非 superuser）
- ✅ `PostgresSettings.app_dsn()` 返回 `ariadne_app` 凭据（owner 作回退）
- ✅ `verify_rls` 自检（api/app.py:59），`ariadne_app` 未配置时告警
- ⚠️ **Compose 与本机环境仍用 owner 角色** —— `.env` 未配 `ARIADNE_PG_APP_USER`，
  app_dsn 回退到 owner，RLS 依然不生效。本机验证靠自检告警提示，生产部署必须配齐双凭据。

## 3. 健康/统计/指标端点认证（P2-11）

**默认行为**：`/health`、`/v1/stats`、`/metrics` **不要求认证**，便于 Prometheus 抓取和 K8s 探针。

**安全考量**：
- 这些端点暴露依赖状态（ClickHouse/Redis/Postgres 可达性）和队列深度，属运维信息。
- Prometheus 通常无法带 API Key 抓取，要求认证会导致监控失效。
- 推荐做法：**反向代理网络隔离**（只允许内网 IP 访问），而非在应用层加认证。

**可选认证**（2026-09-03 新增）：

若要在应用层强制认证（例如暴露公网但无反向代理时），设置：

```bash
ARIADNE_API_REQUIRE_META_AUTH=true
```

启用后三个端点均要求 API Key（`Authorization: Bearer <key>` 或 `X-Ariadne-Key: <key>`）。

**运维决策矩阵**：

| 场景 | require_meta_auth | 网络隔离 | 说明 |
|------|-------------------|----------|------|
| 内网部署 + Prometheus | False（默认） | 推荐 | Prometheus 可直接抓 /metrics，K8s 探针无障碍 |
| 公网暴露 + 反向代理 | False | 必须 | Nginx/Cloudflare 限制 /health、/stats、/metrics 只允许内网/监控 IP |
| 公网暴露 + 无代理 | **True** | 不适用 | 强制认证，但 Prometheus 需配置 bearer_token |
| 开发/测试环境 | False | 可选 | 便于调试 |

**测试覆盖**：`tests/test_meta_endpoints_auth.py` 验证启用/禁用两种模式。

## 4. API Key 存储与认证

### 4.1 存储

- **DB 模式**（M6，`auth_backend=db`）：Argon2id 哈希存 `api_keys` 表，慢哈希 + 盐，防彩虹表。
- **Static 模式**（M1 兼容，`auth_backend=static`）：配置文件单 key，开发/测试用，生产禁用。

### 4.2 密钥轮换

见审计报告 P2-10 的修复：
- JWT secret 轮换支持 `previous_jwt_secrets` 清单，旧密文继续可解。
- API key 用 JWT secret 派生加密密钥（Fernet），重保存即迁到新密钥。
- 生产建议：季度轮换，轮换期保留旧密钥 1 个迁移周期（约 1 周）后清空。

## 5. 沙箱执行（Windows 限制）

**当前状态**：Windows 上只有受限子进程（命令白名单 + Job Object 资源限制）+ Harness 规则，**不是安全沙箱**。

**Linux 路径**（路线图 R5）：
- gVisor（用户态内核）或 Firecracker（轻量 VM）隔离
- seccomp 过滤系统调用
- network namespace 禁网（阻断 SSRF 和元数据端点）

**生产要求**：
- ✅ 可信代码（团队自己的测试）：Windows 受限执行可用
- ❌ 不可信代码（租户上传）：**必须 Linux + gVisor/Firecracker**

见 docs/04-harness-and-sandbox.md 第 6 节完整设计。
