# 06 可观测性

## 1. 一个必须正视的前提：GenAI 语义约定还没稳定

**截至 2026 年，OpenTelemetry GenAI 语义约定中没有任何一个 `gen_ai.*` 属性达到 Stable 状态**，全部仍处于 Development / Experimental；相关约定已从主 semconv 仓库拆分到独立的 `semantic-conventions-genai` 仓库演进。同期生态里还并存着 OpenInference（Arize 系）与 OpenLLMetry（Traceloop 系）两套事实标准，OTel Collector 甚至专门出了 `gen_ai_normalizer` processor 来做归一化。

这意味着裸依赖 GenAI semconv 是明确的技术风险。Ariadne 的应对是**三层隔离**：

```
外部埋点（OTel GenAI / OpenInference / OpenLLMetry / 原生 SDK）
        ↓
[ 适配层 adapters/ ]  ← 归一化到内部规范
        ↓
[ 内部规范 AriadneSpan ]  ← 稳定契约，存储与前端只认这一层
        ↓
[ 导出适配 ]  ← 需要时再转回 OTel 格式供外部消费
```

三条纪律：

1. **semconv 版本在配置中显式锁定**（如 `genai_semconv_version: "1.37.0"`），升级是一次带迁移脚本的显式操作，不随依赖漂移。
2. **存储层用内部字段名**，不直接把 `gen_ai.*` 作为列名。上游改名只需改适配层映射，不需要迁移亿级数据。
3. **同时兼容三套输入**。用户已经在用 OpenInference 或 OpenLLMetry 埋点时，接入 Ariadne 不需要重写埋点代码。

## 2. 属性映射表

| 内部字段 | OTel GenAI | OpenInference | 说明 |
|---|---|---|---|
| `operation` | `gen_ai.operation.name` | `openinference.span.kind` | chat / embeddings / tool / agent |
| `provider` | `gen_ai.provider.name` | `llm.provider` | openai / anthropic / azure / bedrock |
| `model_request` | `gen_ai.request.model` | `llm.model_name` | 请求的模型名 |
| `model_response` | `gen_ai.response.model` | — | **实际响应的模型版本**，与请求可能不同 |
| `input_tokens` | `gen_ai.usage.input_tokens` | `llm.token_count.prompt` | |
| `output_tokens` | `gen_ai.usage.output_tokens` | `llm.token_count.completion` | |
| `cache_read_tokens` | （非标准，自定义） | — | 缓存命中，折扣计价 |
| `reasoning_tokens` | （非标准，自定义） | — | 推理 Token 单独计量 |
| `temperature` | `gen_ai.request.temperature` | `llm.invocation_parameters` | |
| `max_tokens` | `gen_ai.request.max_tokens` | 同上 | |
| `finish_reasons` | `gen_ai.response.finish_reasons` | — | stop / length / tool_calls |
| `prompt` / `completion` | 事件或日志承载 | `llm.input_messages` 等 | 大 payload 外溢 S3 |

**`model_response` 必须单独记录**：provider 侧的别名解析（如 `gpt-4o` → 具体日期版本）会影响可复现性，只记请求模型名会导致"同样配置得到不同结果"无法归因。

## 3. Ariadne 自有命名空间

平台特有语义无法用 GenAI semconv 表达，放在独立命名空间 `ariadne.*`，与上游解耦：

| 属性 | 类型 | 说明 |
|---|---|---|
| `ariadne.loop.id` | string | Loop 实例 id |
| `ariadne.loop.iteration` | int | 轮次号（从 1 开始） |
| `ariadne.loop.mode` | string | retry / quality / verify_execute / hitl |
| `ariadne.loop.state` | string | 状态机当前状态 |
| `ariadne.loop.failure_fp` | string | 失败签名（用于振荡检测与聚类） |
| `ariadne.loop.claimed_done` | bool | 模型是否自称完成 |
| `ariadne.loop.false_completion` | bool | `claimed_done && !converged` |
| `ariadne.harness.rule_id` | string | 命中的规则 |
| `ariadne.harness.action` | string | 裁决动作 |
| `ariadne.eval.name` | string | 评估器名 |
| `ariadne.eval.score` | double | 得分 |
| `ariadne.eval.passed` | bool | 断言是否通过 |
| `ariadne.cost.usd` | double | 本 span 归属成本 |

Span 命名约定：`ariadne.loop.iteration`、`ariadne.harness.check`、`ariadne.eval.run`、`gen_ai.chat`（LLM 调用沿用上游名以便外部工具识别）。

## 4. 采样策略

Loop 场景下采样有个特殊约束：**同一个 Loop 的所有轮次必须一起保留或一起丢弃**，否则进化视图会出现断层。因此采样决策的粒度是 `loop_id` 而非单个 span。

### 4.1 两级采样

**头部采样**（SDK 侧，减少上报量）：

| 场景 | 采样率 |
|---|---|
| Loop 相关 span | 100%（永不采样丢弃） |
| 生产环境普通调用 | 可配置，默认 10% |
| 开发环境 | 100% |

**尾部采样**（Collector 侧，按结果决定保留）：

```python
KEEP_ALWAYS = [
    "有 error 或 exception",
    "Harness 命中 block / require_approval",
    "eval 分数低于阈值",
    "成本高于 P99",
    "延迟高于 P99",
    "false_completion == true",
    "final_state 为失败类终态",
]
```

尾部采样需要缓冲完整 trace 才能决策，缓冲窗口 30s（超时的 trace 按头部决策处理）。这是"有意义的数据全留、无聊的成功请求抽样"的关键 —— 排障需要的恰恰是异常样本。

### 4.2 大 Payload 处理

| 大小 | 处理 |
|---|---|
| ≤ 8 KB | 内联存 ClickHouse |
| 8 KB – 32 KB | 内联但压缩（zstd） |
| > 32 KB | 外溢 S3，ClickHouse 只存对象键 + 前 512 字节预览 |

前端展开大 payload 时用**预签名 URL 直读 S3**，不经过后端转发 —— 否则一个 10MB 的 prompt 会占满 API 的连接与内存。

## 5. PII 脱敏

**双层执行**，两层都不可关闭：

| 层 | 位置 | 作用 |
|---|---|---|
| L1 | SDK 侧 | 敏感字段在离开用户进程前就脱敏，最小化传输风险 |
| L2 | Collector 侧 | 兜底（防 SDK 版本过旧或配置错误），落库前再扫一遍 |

脱敏方式按类型区分：

| 类型 | 策略 | 示例 |
|---|---|---|
| 邮箱 / 手机 / 身份证 / 银行卡 | 掩码保留结构 | `a***@example.com` |
| API Key / Token | 完全替换 | `[REDACTED:api_key]` |
| 人名 / 地址 | 占位符替换 | `[PERSON_1]`、`[ADDRESS_1]` |
| 自定义正则 | 项目级配置 | 内部工号、客户编号 |

同一实体在一次 trace 内映射到**同一占位符**（`[PERSON_1]` 始终指同一人），否则脱敏后的输出会丧失可读性，无法用于排障。

## 6. 指标（Metrics）

除 span 外，导出标准 OTel metrics 供 Prometheus / Grafana 消费：

| 指标 | 类型 | 标签 |
|---|---|---|
| `ariadne_loop_iterations` | Histogram | project, mode, final_state |
| `ariadne_loop_duration_seconds` | Histogram | project, mode |
| `ariadne_loop_cost_usd` | Histogram | project, mode, model |
| `ariadne_loop_terminal_total` | Counter | project, final_state |
| `ariadne_false_completion_total` | Counter | project, model |
| `ariadne_harness_action_total` | Counter | project, rule_id, action |
| `ariadne_eval_score` | Histogram | project, evaluator |
| `ariadne_llm_errors_total` | Counter | provider, model, error_code |
| `ariadne_collector_lag_seconds` | Gauge | —— |
| `ariadne_sandbox_pool_available` | Gauge | profile |

`ariadne_collector_lag_seconds` 是自监控的核心指标：采集管道积压意味着前端看到的是过期数据，必须告警。

## 7. 日志

结构化 JSON 日志，强制携带 `trace_id` / `span_id` / `loop_id` / `project_id`，实现日志与 trace 双向跳转。

```python
import logging

logger = logging.getLogger(__name__)

logger.info(
    "loop iteration finished",
    extra={
        "loop_id": loop_id,
        "iteration": iteration,
        "state": state,
        "score": verdict.score,
        "converged": verdict.converged,
        "cost_usd": cost,
    },
)
```

日志级别约定：`DEBUG` 记录 span 属性与上下文构造细节；`INFO` 记录轮次进展与终态；`WARNING` 记录降级（模型切换、上下文二次压缩、缓存失效）；`ERROR` 记录 provider 失败与沙箱异常；`CRITICAL` 记录数据管道中断。

禁止 `print()`。禁止在日志中输出完整 prompt/completion（体积 + 隐私），只输出哈希与长度，内容通过 span payload 引用。

## 8. 与外部 APM 打通

Ariadne 不做通用 APM，但必须能与既有系统关联：

- **接收上游 trace context**：SDK 遵循 W3C Trace Context，用户 HTTP 请求的 `traceparent` 会被继承，因此在 Datadog/Grafana 里能用同一个 trace_id 找到 AI 环节。
- **导出到外部**：支持配置第二个 OTLP exporter，把 span 同时发给用户既有的 APM（双写）。
- **不接管基础设施指标**：DB、缓存、HTTP 中间件的埋点仍归用户既有 APM。

## 9. 自监控

平台自身的健康度用同一套机制观测（吃自己的狗粮）：Collector 吞吐与积压、ClickHouse 写入延迟与合并压力、Redis 队列深度、Loop Worker 存活与续租失败率、沙箱池可用数、S3 上传失败率。

自监控数据写入独立的 `_internal` 项目，避免与用户数据混在一起、以及避免"平台故障时自监控也写不进去"的死锁 —— 该项目的写入路径绕过采样与脱敏，直连存储。

