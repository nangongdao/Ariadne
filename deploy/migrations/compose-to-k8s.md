# 从 Compose 迁移到 Kubernetes 指南

本文档指导从 `docker-compose` 单机部署迁移到 Helm + Kubernetes 集群部署。
M6 §9 验收项 #15：按迁移指南操作，数据不丢。

## 前置条件

| 组件 | 版本要求 | 验证 |
|---|---|---|
| Kubernetes | ≥ 1.27 | `kubectl version --short` |
| Helm | ≥ 3.12 | `helm version` |
| ClickHouse | ≥ 23.3 | 已有集群或新部署 |
| PostgreSQL | ≥ 15 | 已有实例或新部署 |
| Redis | ≥ 7.0 | 已有实例或新部署 |
| S3 / MinIO | — | 已有桶或新建 |

## 1. 数据备份（不跳过）

迁移前必须备份三处存储的数据。

```bash
# Postgres 全量备份
pg_dump -h <pg_host> -U ariadne -F c -f ariadne_pg.dump ariadne

# ClickHouse 备份（用 clickhouse-backup 或手动）
clickhouse-backup create ariadne_pre_migration

# S3 / MinIO 对象（如有数据）
aws s3 sync s3://ariadne-payloads ./ariadne_s3_backup --endpoint-url <endpoint>
```

## 2. 部署依赖（如尚未有 K8s 上的实例）

推荐用 Bitnami Helm charts 部署依赖：

```bash
# PostgreSQL
helm install postgres bitnami/postgresql \
  --set auth.username=ariadne \
  --set auth.password=ariadne \
  --set auth.database=ariadne

# Redis
helm install redis bitnami/redis \
  --set auth.password=ariadne

# ClickHouse
helm install clickhouse bitnami/clickhouse \
  --set auth.username=ariadne \
  --set auth.password=ariadne
```

## 3. 创建 Secret（存储凭证）

```bash
kubectl create secret generic ariadne-secrets \
  --from-literal=ARIADNE_CH_USERNAME=ariadne \
  --from-literal=ARIADNE_CH_PASSWORD=ariadne \
  --from-literal=ARIADNE_PG_PASSWORD=ariadne \
  --from-literal=ARIADNE_REDIS_PASSWORD=ariadne \
  --from-literal=AWS_ACCESS_KEY_ID=<key> \
  --from-literal=AWS_SECRET_ACCESS_KEY=<secret>
```

## 4. 运行迁移 Job（Helm pre-install hook 自动触发）

Helm chart 的 `migrate` Job 会在 `helm install` 前自动运行 ClickHouse DDL
+ Postgres Alembic 迁移。也可以手动触发：

```bash
kubectl create job ariadne-migrate-manual \
  --from=cronjob/ariadne-migrate
```

## 5. 恢复数据

### 5.1 Postgres

```bash
kubectl cp ariadne_pg.dump postgres-pod:/tmp/ariadne_pg.dump
kubectl exec -it postgres-pod -- pg_restore -U ariadne -d ariadne -1 /tmp/ariadne_pg.dump
```

### 5.2 ClickHouse

```bash
# 用 clickhouse-backup 恢复
kubectl exec -it clickhouse-pod -- clickhouse-backup restore ariadne_pre_migration
```

### 5.3 S3 / MinIO

```bash
aws s3 sync ./ariadne_s3_backup s3://ariadne-payloads/ --endpoint-url <endpoint>
```

## 6. 部署 Ariadne

```bash
# 创建 values override（按你的环境调整）
cat > ariadne-values.yaml << 'EOF'
global:
  clickhouse:
    url: "clickhouse://clickhouse:8123/ariadne"
    existingSecret: ariadne-secrets
  postgres:
    url: "postgresql+asyncpg://ariadne@postgres:5432/ariadne"
    existingSecret: ariadne-secrets
  redis:
    url: "redis://:ariadne@redis:6379/0"
    existingSecret: ariadne-secrets
  s3:
    bucket: ariadne-payloads
    endpoint: http://minio:9000
    existingSecret: ariadne-secrets

# 生产环境推荐副本数
collector:
  replicaCount: 3
  hpa:
    maxReplicas: 30

loop:
  replicaCount: 5

eval:
  replicaCount: 3
  hpa:
    maxReplicas: 15
EOF

# 安装
helm install ariadne ./deploy/helm -f ariadne-values.yaml

# 等待 API 就绪
kubectl rollout status deployment/ariadne-api
```

## 7. 验证

```bash
# 健康检查
kubectl port-forward svc/ariadne-api 8000:8000
curl http://localhost:8000/health

# Prometheus 指标
curl http://localhost:8000/metrics | grep ariadne_

# 队列状态
curl http://localhost:8000/v1/stats
```

## 8. 三类 Worker 独立伸缩

M6 §5 的核心设计：三类 Worker 按负载特征独立伸缩。

```bash
# 手动扩容 Collector（高吞吐期）
kubectl scale deployment ariadne-collector --replicas=10

# 手动扩容 Loop Worker（大量 Loop 排队）
kubectl scale deployment ariadne-loop --replicas=8

# 手动扩容 Eval Worker（评测高峰）
kubectl scale deployment ariadne-eval --replicas=6

# HPA 自动伸缩状态
kubectl get hpa
```

| Worker | 伸缩依据 | HPA 指标 |
|---|---|---|
| collector | span 速率 | CPU + 自定义指标 `collector_lag_seconds` |
| loop | 并发 Loop 数 | 自定义指标 `queue_pending{stream="q:loop"}` |
| eval | CPU 利用率 | CPU utilization（评测是 CPU 密集） |

## 9. 回滚

如果迁移后发现问题，回滚到 Compose：

```bash
# 卸载 Helm release
helm uninstall ariadne

# Compose 重新启动
docker compose up -d
```

数据不会丢 —— Compose 和 K8s 共用同一套存储（Postgres/ClickHouse/Redis/S3），
只是应用层从 Compose 切到 K8s。如果迁移时用了独立存储实例（如步骤 2 新建），
回滚前需将 K8s 上的数据导回 Compose 使用的存储。
