# 09 API 与 SDK

## 1. REST API

版本前缀 `/v1`，全部返回 JSON，错误遵循 RFC 9457 Problem Details。

### 1.1 Loop

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/loops` | 创建并启动 Loop（同步返回 id，异步执行） |
| GET | `/v1/loops` | 列表（游标分页，支持按 state/mode/时间过滤） |
| GET | `/v1/loops/{id}` | 详情（含当前状态、累计用量） |
| GET | `/v1/loops/{id}/iterations` | 全部轮次记录 |
| GET | `/v1/loops/{id}/stream` | SSE 实时事件流 |
| POST | `/v1/loops/{id}/cancel` | 取消（转 `CANCELLED`） |
| POST | `/v1/loops/{id}/resume` | 从最近检查点恢复 |
| POST | `/v1/loops/{id}/approve` | HITL 审批（body: `{approved, comment}`） |
| POST | `/v1/loops/batch` | 批量创建（并行 Loop 池） |

创建请求示例：

```json
POST /v1/loops
{
  "task": "为 utils/parser.py 补充单元测试，覆盖率不低于 90%",
  "mode": "verify_execute",
  "assertions": [
    {"id": "tests-pass", "kind": "command",
     "spec": {"cmd": "pytest -q"}, "hint": "先修失败用例再补覆盖"},
    {"id": "coverage", "kind": "command",
     "spec": {"cmd": "pytest --cov=utils --cov-fail-under=90"}},
    {"id": "lint", "kind": "command",
     "spec": {"cmd": "ruff check ."}, "blocking": false}
  ],
  "budget": {"max_iterations": 6, "max_cost_usd": 0.5},
  "sandbox": {"profile": "standard"},
  "artifacts": [{"path": "utils/parser.py", "ref": "s3://.../parser.py"}]
}
```

响应 `202 Accepted`：`{"loop_id": "...", "state": "VALIDATE", "stream_url": "/v1/loops/.../stream"}`

目标不可验证时返回 `422` 并说明原因：

```json
{
  "type": "https://ariadne.dev/errors/unverifiable-goal",
  "title": "目标不可验证",
  "status": 422,
  "detail": "所有断言的 blocking 均为 false，缺少硬性收敛条件",
  "assertions_rejected": [{"id": "lint", "reason": "blocking=false"}]
}
```

### 1.2 其他资源

| 资源 | 端点 |
|---|---|
| Trace / Span | `GET /v1/traces`、`GET /v1/traces/{trace_id}`、`GET /v1/spans`（多维过滤 + 全文搜索） |
| 评测 | `POST /v1/evaluations`（单次打分）、`POST /v1/experiments`（批量实验）、`GET /v1/experiments/{id}/compare?baseline=` |
| 数据集 | `POST/GET /v1/datasets`、`POST /v1/datasets/{id}/items`、`POST /v1/datasets/{id}/versions` |
| Prompt | `POST/GET /v1/prompts`、`POST /v1/prompts/{name}/versions`、`PUT /v1/prompts/{name}/labels` |
| Spec | `POST/GET/PUT /v1/specs`、`POST /v1/specs/validate` |
| 规则 | `GET/PUT /v1/rules`、`POST /v1/rules/test`（用样例载荷试跑规则） |
| SLI | `GET /v1/sli?window=24h&group_by=model` |
| 成本 | `GET /v1/costs?group_by=loop_id,model&from=&to=` |

`POST /v1/rules/test` 是重要的开发体验设计：规则写错的代价很高（可能全量拦截），必须能在应用前用样例载荷验证。

### 1.3 遥测接入

| 端点 | 协议 | 说明 |
|---|---|---|
| `POST /v1/traces` (OTLP) | OTLP/HTTP protobuf 或 JSON | 标准 OTel exporter 可直接指向此处 |
| `POST /v1/ingest/spans` | 自有 JSON 批量格式 | 简化接入，无需 OTel 依赖 |
| `POST /v1/ingest/openinference` | OpenInference 格式 | 兼容 Arize 系埋点 |

三个端点最终都归一化为内部 `AriadneSpan`（见 [06 可观测性](06-observability.md#1-一个必须正视的前提genai-语义约定还没稳定)）。

### 1.4 认证与限流

**两种凭证**：

| 凭证 | 格式 | 用途 | 作用域 |
|---|---|---|---|
| API Key | `ak_live_{prefix}_{secret}` | SDK / CI / 服务端调用 | 项目级 + scopes(`ingest`/`read`/`loop:write`/`admin`) |
| JWT | Bearer token | Web Console | 用户级，权限由 RBAC 决定 |

API Key 只在创建时返回明文，服务端存 Argon2id 哈希；校验时先用 `key_prefix` 定位记录再验哈希（避免全表扫描）。

**限流**：滑动窗口 + 令牌桶双层。

| 端点类别 | 默认限额 |
|---|---|
| 遥测写入 | 10,000 req/min per project |
| Loop 创建 | 60 req/min per project |
| 查询 | 600 req/min per key |
| 实验 / 批量 | 10 req/min per project |

超限返回 `429` 并带 `Retry-After` 与 `X-RateLimit-*` 头。遥测写入的限额远高于其他 —— 它是热路径，且丢数据的代价低于阻塞业务。

## 2. Python SDK

### 2.1 装饰器（最常用）

```python
from ariadne import trace, Ariadne

client = Ariadne(api_key=os.environ["ARIADNE_API_KEY"], project="my-app")

@trace(name="answer_question", kind="agent")
def answer(question: str) -> str:
    docs = retrieve(question)
    return generate(question, docs)
```

### 2.2 上下文管理器（细粒度）

```python
with client.span("rag_retrieve", kind="rag") as span:
    docs = retriever.search(query, top_k=5)
    span.set_attributes({"top_k": 5, "hit_count": len(docs)})
    span.set_output_preview(f"{len(docs)} docs")
```

### 2.3 自动埋点

```python
from ariadne.instrument import instrument_all

instrument_all()   # 自动挂载 OpenAI / Anthropic / LangChain / LangGraph
```

首版只保证这四条链路。自动埋点通过各库的官方 callback / hook 机制实现，不做 monkey patch 私有方法（版本升级即碎）。

### 2.4 Loop API

```python
from ariadne import Goal, Assertion, Budget

result = client.loop.run(
    goal=Goal(
        task="为 parser.py 补充单元测试，覆盖率 ≥ 90%",
        assertions=(
            Assertion(id="tests", kind="command", spec={"cmd": "pytest -q"}),
            Assertion(id="cov", kind="command",
                      spec={"cmd": "pytest --cov=utils --cov-fail-under=90"}),
        ),
        budget=Budget(max_iterations=6, max_cost_usd=0.5),
        mode="verify_execute",
    ),
    artifacts={"utils/parser.py": Path("utils/parser.py")},
)

print(result.final_state)        # CONVERGED
print(result.iterations)         # 3
print(result.total_cost_usd)     # 0.18
for it in result.history:
    print(it.iteration, it.score, it.failed_ids)
```

`run()` 阻塞直到终态；`submit()` 返回句柄用于异步轮询或订阅 SSE。

### 2.5 SDK 设计约束

- **绝不阻塞业务**：上报走后台线程 + 有界队列（默认 10,000），队列满时丢弃最旧并计数告警，**不反压业务线程**。
- 同步路径只做序列化入队，目标 < 1ms。
- 进程退出时 `atexit` flush，超时上限 5s。
- 网络失败按指数退避重试 3 次，仍失败则落本地磁盘缓冲（可选）。
- SDK 自身异常**永不向业务抛出**，全部内部捕获并计入自监控。

## 3. TypeScript SDK

对等能力，API 风格适配 TS 习惯：

```typescript
import { Ariadne, trace } from "@ariadne/sdk";

const client = new Ariadne({ apiKey: process.env.ARIADNE_API_KEY!, project: "my-app" });

const answer = trace({ name: "answerQuestion", kind: "agent" }, async (q: string) => {
  const docs = await retrieve(q);
  return generate(q, docs);
});

const result = await client.loop.run({
  task: "生成产品发布公告，质量分 ≥ 85",
  mode: "quality",
  assertions: [
    { id: "quality", kind: "metric", spec: { name: "composite_quality", op: ">=", value: 85 } },
    { id: "length", kind: "metric", spec: { name: "word_count", op: "<=", value: 800 } },
  ],
  budget: { maxIterations: 5, maxCostUsd: 0.2 },
});
```

## 4. CLI

**当前可用命令**：

```bash
# API 服务器
ariadne-api

# Worker 进程（collector / loop / eval / retention）
ariadne-worker                        # 默认启动 collector
ariadne-worker loop                   # Loop Worker
ariadne-worker eval                   # Eval Worker
ariadne-worker retention              # Retention Worker

# 数据库迁移
ariadne-migrate [ddl_dir]             # ClickHouse + Postgres 迁移

# 评测对比与门禁
ariadne-eval compare --baseline base.json --current cur.json [--rules gate.yaml]
ariadne-eval show --result cur.json

# 规则集离线试跑与压测
ariadne-rules test --rules <文件|目录> --hook pre_tool [--context '{"tool": {"cmd": "..."}}']
ariadne-rules bench --rules <文件|目录> --hook pre_tool [--iterations 1000]
```

**退出码语义**：
- `ariadne-eval compare`：0 通过 / 1 有退化 / 2 配置或数据问题
- `ariadne-rules`：0 成功（BLOCK 也是成功 —— 试跑看裁决，不是跑门禁）/ 2 配置或加载错误
- 其他命令：0 成功 / 非零失败

`ariadne-eval compare` 用于 [05 评测引擎](05-evaluation.md#33-ci-回归门禁) 描述的 CI 回归门禁。

`ariadne-rules test` 是 `POST /v1/rules/test` 的离线版：直接加载本地
YAML 规则集 + 给定 hook/context 试跑，不改库、无需起服务，规则作者改
YAML 后本地立即验证。`ariadne-rules bench` 输出求值延迟分位数（p50/p99），
规则改动后看有没有量级退化（绝对毫秒受机器负载影响，跨机对比用两条规则
互比而非绝对值）。

**计划实现**（见 [11 路线图](11-roadmap.md)）：
- `ariadne init` - 生成 spec.yaml 模板（M3 本地 CLI）
- `ariadne spec validate` - 校验 spec 可验证性（M3）
- `ariadne run` - 本地执行 Loop（M3）
- `ariadne trace tail` - 实时跟随 trace（可观测性增强）
- `ariadne cost report` - 成本报表（可观测性增强）

