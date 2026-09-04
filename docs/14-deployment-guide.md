# 部署指南

本文档说明如何在不同环境部署 Ariadne。

## 适用场景

Ariadne 当前适用于以下场景：

### ✅ 推荐场景

1. **可信代码自托管**
   - 用户自己的代码在自己的机器/私有云上运行
   - 开发与测试环境
   - 内部团队协作（同一组织内的可信用户）
   - 单租户部署

2. **开发与演示**
   - 本地开发环境（Docker Compose）
   - 功能演示与 PoC
   - 集成测试与 CI/CD

### ⚠️ 受限场景

3. **多租户 SaaS（需额外加固）**
   - 需要 Linux 环境 + gVisor/Firecracker 沙箱
   - 需要网络命名空间隔离
   - 需要 syscall 过滤
   - **当前 Windows 单平台无法提供等效隔离**

### ❌ 不适用场景

4. **不可信代码执行（当前不支持）**
   - 公开的代码执行服务
   - 用户上传任意代码并执行
   - **原因**：Windows 平台无法提供沙箱级隔离（无 gVisor/Firecracker、无网络命名空间、无 syscall 过滤）
   - **配置要求**：`ARIADNE_SANDBOX_ALLOW_UNTRUSTED_CODE` 必须保持 `false`

---

## 部署架构

### 组件清单

| 组件 | 用途 | 是否必需 |
|------|------|---------|
| **PostgreSQL** | 元数据、租户、RBAC、工作流定义 | 必需 |
| **ClickHouse** | Trace/Span 存储、成本聚合 | 必需 |
| **Redis** | Loop/Eval 队列、预算计数、租约 | 必需 |
| **API** | FastAPI 服务（支持横向扩展） | 必需 |
| **Worker** | Loop/Eval/Retention 后台任务 | 必需 |
| **Frontend** | React SPA（静态托管或 CDN） | 可选（仅 API 也可用）|
| **S3/MinIO** | 对象存储（产出物、冷数据） | 可选（本地文件系统降级）|
| **Prometheus** | 指标采集 | 可选 |
| **Grafana** | 可视化与告警 | 可选 |

### 最小部署（开发/单机）

```
┌─────────────┐
│  Frontend   │  (React SPA, 可选)
└─────────────┘
       │
       ▼
┌─────────────┐
│   FastAPI   │  (API + Worker 单进程)
└─────────────┘
   │   │   │
   ▼   ▼   ▼
 PG  CH  Redis
```

### 生产部署（多副本）

```
         ┌───────────┐
         │    CDN    │  (Frontend 静态资源)
         └───────────┘
               │
         ┌───────────┐
         │ Ingress / │
         │    LB     │
         └───────────┘
          │         │
    ┌─────┴───┐ ┌──┴──────┐
    │  API    │ │  API    │  (横向扩展)
    │ (Pod 1) │ │ (Pod 2) │
    └─────────┘ └─────────┘
          │         │
    ┌─────┴─────────┴────┐
    │                    │
    ▼                    ▼
┌─────────┐      ┌───────────┐
│ Worker  │      │  Worker   │  (独立伸缩)
│ (Loop)  │      │ (Eval)    │
└─────────┘      └───────────┘
    │                    │
    └────────┬───────────┘
             ▼
   ┌────────────────────┐
   │  PostgreSQL + RLS  │
   │  ClickHouse + MV   │
   │  Redis Streams     │
   │  S3 / MinIO        │
   └────────────────────┘
```

---

## 方式 1：Docker Compose（推荐用于开发/单机）

### 前置条件

- Windows 10/11
- Docker Desktop（WSL 2 后端）
- Python 3.11+（安装 `uv`）
- Node.js 18+（若需构建前端）

### 步骤

1. **克隆仓库并配置环境**

```powershell
git clone <repo-url> ariadne
cd ariadne

# 复制环境配置模板
cp .env.example .env

# 编辑 .env，设置必需的密钥
# - ARIADNE_API_JWT_SECRET（随机生成 32 字节）
# - ARIADNE_API_STATIC_API_KEY（开发用）
# - POSTGRES_PASSWORD（数据库密码）
```

2. **生成 JWT 密钥**

```powershell
# PowerShell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

3. **启动服务**

```powershell
docker compose up -d
```

服务端口：
- API: `http://localhost:8000`
- API 文档: `http://localhost:8000/docs`
- Frontend: `http://localhost:8000`（通过 API 静态托管）
- PostgreSQL: `localhost:5432`
- ClickHouse: `localhost:9000`（Native），`localhost:8123`（HTTP）
- Redis: `localhost:6379`

4. **运行数据库迁移**

```powershell
# 迁移在 compose 启动时自动运行（migrate 服务）
# 手动运行：
uv sync
uv run alembic upgrade head
```

5. **生成演示数据**

```powershell
uv run python examples/demo_rag.py
```

6. **访问前端**

打开 <http://localhost:8000>，使用默认 API Key：`ak_local_dev_key`

### 停止服务

```powershell
docker compose down        # 停止服务，保留数据
docker compose down -v     # 停止服务，删除数据卷
```

---

## 方式 2：Kubernetes + Helm（推荐用于生产）

### 前置条件

- Kubernetes 1.24+
- Helm 3.8+
- 已配置的 PostgreSQL、ClickHouse、Redis（或使用 Helm 部署）

### 步骤

1. **准备外部依赖**

```bash
# 选项 A：使用托管服务
# - AWS RDS (PostgreSQL)
# - AWS ElastiCache (Redis)
# - ClickHouse Cloud

# 选项 B：使用 Helm chart 部署
helm install postgresql bitnami/postgresql --set auth.database=ariadne
helm install redis bitnami/redis
helm install clickhouse altinity/clickhouse-operator
```

2. **配置 values.yaml**

```yaml
# deploy/helm/values.yaml
image:
  repository: your-registry/ariadne
  tag: "latest"

api:
  replicas: 2
  resources:
    requests:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 2000m
      memory: 4Gi

worker:
  loop:
    replicas: 2
  eval:
    replicas: 1
  retention:
    replicas: 1

postgresql:
  host: "postgres.example.com"
  port: 5432
  database: "ariadne"
  existingSecret: "ariadne-db-secret"  # 包含 password

clickhouse:
  host: "clickhouse.example.com"
  port: 9000
  database: "ariadne"

redis:
  host: "redis.example.com"
  port: 6379

config:
  jwtSecret:
    existingSecret: "ariadne-jwt-secret"  # 包含 jwt_secret
  staticApiKey:
    existingSecret: "ariadne-api-key"     # 包含 api_key

ingress:
  enabled: true
  className: "nginx"
  hosts:
    - host: ariadne.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: ariadne-tls
      hosts:
        - ariadne.example.com
```

3. **创建密钥**

```bash
# JWT Secret
kubectl create secret generic ariadne-jwt-secret \
  --from-literal=jwt_secret=$(openssl rand -base64 32)

# API Key
kubectl create secret generic ariadne-api-key \
  --from-literal=api_key="ak_prod_$(openssl rand -hex 16)"

# 数据库密码
kubectl create secret generic ariadne-db-secret \
  --from-literal=password="your-secure-password"
```

4. **安装 Helm Chart**

```bash
helm install ariadne ./deploy/helm -f values.yaml
```

5. **运行数据库迁移**

```bash
# 迁移作为 Job 自动运行
kubectl get jobs -l app.kubernetes.io/component=migration

# 手动触发迁移
kubectl create job --from=cronjob/ariadne-migration ariadne-migration-manual
```

6. **验证部署**

```bash
kubectl get pods -l app.kubernetes.io/name=ariadne
kubectl logs -l app.kubernetes.io/component=api --tail=50
kubectl logs -l app.kubernetes.io/component=worker-loop --tail=50

# 检查健康状态
curl https://ariadne.example.com/health
```

---

## 方式 3：手动部署（高级）

### API 服务

```powershell
# 安装依赖
uv sync --all-extras

# 设置环境变量
$env:ARIADNE_ENV = "production"
$env:ARIADNE_LOG_LEVEL = "INFO"
$env:ARIADNE_API_HOST = "0.0.0.0"
$env:ARIADNE_API_PORT = "8000"
$env:ARIADNE_POSTGRES_URL = "postgresql://user:pass@host:5432/ariadne"
$env:ARIADNE_CLICKHOUSE_HOST = "clickhouse-host"
$env:ARIADNE_REDIS_URL = "redis://redis-host:6379"
$env:ARIADNE_API_JWT_SECRET = "your-32-byte-secret"
$env:ARIADNE_API_STATIC_API_KEY = "ak_prod_key"

# 运行 API
uv run uvicorn ariadne.api.app:create_app --host 0.0.0.0 --port 8000 --factory
```

### Worker 服务

```powershell
# Loop Worker
uv run python -m ariadne.cli worker loop

# Eval Worker
uv run python -m ariadne.cli worker eval

# Retention Worker
uv run python -m ariadne.cli worker retention
```

### 前端构建

```powershell
cd web
npm install
npm run build

# 生成的 dist/ 可以托管到任意静态服务器或 CDN
# 确保设置 VITE_API_BASE_URL 指向 API 地址
```

---

## 配置项说明

### 核心配置

| 环境变量 | 说明 | 默认值 | 必需 |
|---------|------|--------|------|
| `ARIADNE_ENV` | 环境标识 | `development` | 否 |
| `ARIADNE_LOG_LEVEL` | 日志级别 | `INFO` | 否 |
| `ARIADNE_API_HOST` | API 监听地址 | `127.0.0.1` | 否 |
| `ARIADNE_API_PORT` | API 监听端口 | `8000` | 否 |

### 数据库

| 环境变量 | 说明 | 必需 |
|---------|------|------|
| `ARIADNE_POSTGRES_URL` | PostgreSQL 连接字符串 | 是 |
| `ARIADNE_CLICKHOUSE_HOST` | ClickHouse 主机 | 是 |
| `ARIADNE_CLICKHOUSE_PORT` | ClickHouse 端口（Native） | 否（默认 9000）|
| `ARIADNE_REDIS_URL` | Redis 连接字符串 | 是 |

### 安全

| 环境变量 | 说明 | 必需 |
|---------|------|------|
| `ARIADNE_API_JWT_SECRET` | JWT 签名密钥（≥32 字节） | 是 |
| `ARIADNE_API_STATIC_API_KEY` | 静态 API Key（开发用） | 是 |
| `ARIADNE_API_PREVIOUS_JWT_SECRETS` | 历史 JWT 密钥（逗号分隔） | 否 |
| `ARIADNE_API_REQUIRE_META_AUTH` | 健康端点是否需要认证 | 否（默认 false）|

### 沙箱（重要）

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ARIADNE_SANDBOX_ALLOW_UNTRUSTED_CODE` | **必须保持 `false`** | `false` |
| `ARIADNE_SANDBOX_PROFILE` | 执行档位（trusted/standard/strict） | `trusted` |

**⚠️ 安全警告**：
- Windows 平台无法提供沙箱级隔离
- `ALLOW_UNTRUSTED_CODE=true` 会带来严重安全风险
- 仅在 Linux + gVisor/Firecracker 环境下才考虑启用

### Worker

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ARIADNE_WORKER_LOOP_CONCURRENCY` | Loop 并发数 | `5` |
| `ARIADNE_WORKER_EVAL_CONCURRENCY` | Eval 并发数 | `3` |
| `ARIADNE_WORKER_RECONCILE_INTERVAL_SECONDS` | 补偿扫描间隔 | `60` |
| `ARIADNE_WORKER_RECONCILE_IDLE_SECONDS` | 判定空闲阈值 | `300` |
| `ARIADNE_WORKER_GRAPH_LEASE_DURATION_S` | Graph 租约时长（秒） | `300` |
| `ARIADNE_WORKER_GRAPH_LEASE_EXTEND_INTERVAL_S` | Graph 租约续期间隔（秒） | `120` |

### 对象存储（可选）

| 环境变量 | 说明 | 必需 |
|---------|------|------|
| `ARIADNE_S3_ENDPOINT` | S3 兼容端点 | 否 |
| `ARIADNE_S3_ACCESS_KEY` | 访问密钥 | 否 |
| `ARIADNE_S3_SECRET_KEY` | 密钥 | 否 |
| `ARIADNE_S3_BUCKET` | 存储桶名称 | 否 |

未配置时使用本地文件系统（`data/artifacts/`）。

---

## 数据库迁移

### 查看迁移状态

```powershell
uv run alembic current
uv run alembic history
```

### 升级到最新版本

```powershell
uv run alembic upgrade head
```

### 回滚迁移

```powershell
# 回滚一个版本
uv run alembic downgrade -1

# 回滚到特定版本
uv run alembic downgrade <revision>
```

### 生成新迁移

```powershell
uv run alembic revision -m "描述" --autogenerate
```

---

## 监控与可观测

### Prometheus 指标

API 和 Worker 暴露 Prometheus 指标：

```
http://localhost:8000/metrics
```

关键指标：
- `ariadne_loop_duration_seconds`：Loop 执行时长
- `ariadne_loop_terminal_total{state}`：Loop 终态计数
- `ariadne_graph_run_duration_seconds`：Graph 执行时长
- `ariadne_api_request_duration_seconds`：API 请求时长
- `ariadne_worker_queue_length`：队列深度

### 健康检查

```bash
# 基础健康检查
curl http://localhost:8000/health

# 详细状态（需认证，若启用 REQUIRE_META_AUTH）
curl -H "X-Ariadne-Key: $API_KEY" http://localhost:8000/v1/stats
```

### 日志

结构化 JSON 日志输出到 stdout：

```powershell
# API 日志
docker compose logs -f api

# Worker 日志
docker compose logs -f worker-loop
```

生产环境推荐使用日志聚合（ELK、Loki、CloudWatch）。

---

## 安全加固清单

### 部署前检查

- [ ] 替换所有默认密钥（JWT secret、API key、数据库密码）
- [ ] 确认 `ARIADNE_SANDBOX_ALLOW_UNTRUSTED_CODE=false`
- [ ] 启用 HTTPS（TLS 证书）
- [ ] 配置 CORS 白名单（`ARIADNE_API_CORS_ORIGINS`）
- [ ] 限制数据库访问（仅允许 API/Worker 访问）
- [ ] 配置防火墙规则（仅暴露 API 端口）
- [ ] 启用 PostgreSQL RLS（自动启用，验证 `ariadne_app` 角色）
- [ ] 配置日志聚合与告警
- [ ] 设置资源限制（CPU/内存）
- [ ] 配置备份策略（PostgreSQL + ClickHouse）

### 运行时监控

- [ ] 监控队列深度（Redis）
- [ ] 监控数据库连接池
- [ ] 监控 API 响应时间
- [ ] 监控 Worker 崩溃/重启
- [ ] 监控成本突增告警
- [ ] 设置 SLO 告警（错误率、延迟）

### 定期审计

- [ ] 审计 API Key 使用（audit_logs 表）
- [ ] 审计跨租户访问尝试
- [ ] 审计 Harness 规则拦截日志
- [ ] 审计密钥轮换（JWT secret）
- [ ] 审计数据删除任务（GDPR）

---

## 故障排查

### API 无法启动

```powershell
# 检查环境变量
uv run python -c "from ariadne.config import Settings; print(Settings())"

# 检查数据库连接
uv run python -c "from ariadne.storage.postgres import PostgresStore; import asyncio; asyncio.run(PostgresStore(...).ping())"

# 查看详细日志
$env:ARIADNE_LOG_LEVEL = "DEBUG"
uv run uvicorn ariadne.api.app:create_app --factory
```

### Worker 不消费队列

```powershell
# 检查 Redis 连接
redis-cli -h localhost -p 6379 ping

# 查看队列深度
redis-cli -h localhost -p 6379 XLEN ariadne:loops

# 检查 Worker 日志
docker compose logs worker-loop --tail=100
```

### 数据库迁移失败

```powershell
# 查看当前版本
uv run alembic current

# 强制标记为特定版本（谨慎使用）
uv run alembic stamp <revision>

# 手动执行 SQL
psql -h localhost -U ariadne -d ariadne -f deploy/alembic/versions/<file>.py
```

### 前端无法连接 API

```javascript
// 检查前端配置
console.log(import.meta.env.VITE_API_BASE_URL)

// 检查 CORS
// API 日志应显示 CORS 预检请求
// 设置 ARIADNE_API_CORS_ORIGINS="http://localhost:5173,https://your-domain.com"
```

---

## 升级指南

### 小版本升级（patch/minor）

```powershell
# 1. 备份数据库
pg_dump -h localhost -U ariadne ariadne > backup.sql

# 2. 拉取新镜像
docker compose pull

# 3. 运行迁移
docker compose up migrate

# 4. 重启服务
docker compose up -d api worker-loop worker-eval
```

### 大版本升级（major）

参考 CHANGELOG.md 的 Breaking Changes 节，可能需要：
- 配置项重命名或移除
- 数据库手动迁移
- API 响应格式变更
- 前端重新构建

---

## 性能调优

### API

- 增加副本数（`api.replicas`）
- 调整数据库连接池（`ARIADNE_POSTGRES_POOL_SIZE`）
- 启用 Redis 缓存（计划中）
- 配置 CDN（静态资源）

### Worker

- 根据负载独立扩展 Loop/Eval/Retention Worker
- 调整并发数（`WORKER_LOOP_CONCURRENCY`）
- 监控队列深度，动态调整副本数

### 数据库

- ClickHouse 分区策略（按日期）
- ClickHouse 物化视图预聚合
- PostgreSQL 连接池优化
- Redis 持久化策略（AOF vs RDB）

### 前端

- 启用 CDN
- 配置浏览器缓存（`Cache-Control`）
- 启用 gzip/brotli 压缩
- 路由级代码分割（已实现）

---

## 支持与反馈

- 文档：`docs/` 目录
- 问题反馈：GitHub Issues
- 安全问题：请私下报告

