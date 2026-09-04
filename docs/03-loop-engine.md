# 03 Loop Engine（核心）

## 1. 设计原则

Loop Engineering 与 Prompt Engineering 的本质区别在于：**可靠性来源从"模型能力"转移到"工程化保障机制"**。落到设计上是四条铁律：

1. **目标必须可验证**。"把代码优化一下"不是目标，"所有 pytest 通过且 ruff 无 error"才是。系统在创建 Loop 时就拒绝不可验证的目标。
2. **不信任模型的自我评估**。模型输出"我完成了"不产生任何状态转移，只有 Verifier 的裁决才能。
3. **状态外置**。每轮结束落检查点，进程崩溃后从检查点续跑，绝不从零开始。
4. **成本必须有硬上限**。Loop 最大的风险是 Token 消耗失控，防护必须是熔断（强制终止）而非告警。

## 2. 状态机

```
                    ┌─────────┐
                    │ CREATED │
                    └────┬────┘
                         ▼
                   ┌──────────┐   目标不可验证
                   │ VALIDATE ├──────────────► REJECTED
                   └────┬─────┘
                         ▼
      ┌──────────► ┌──────────┐
      │            │ PLANNING │  构造本轮上下文与 Prompt
      │            └────┬─────┘
      │                 ▼
      │            ┌──────────┐   规则 block
      │            │ PRECHECK ├──────────────► BLOCKED
      │            └────┬─────┘   需人工审批
      │                 │      └─────────────► HUMAN_PENDING ──┐
      │                 ▼                                       │
      │            ┌───────────┐  执行 LLM/工具/代码            │
      │            │ EXECUTING │                                │
      │            └────┬──────┘                                │
      │                 ▼                                       │
      │            ┌────────────┐  多维打分 + 断言求值           │
      │            │ EVALUATING │                               │
      │            └────┬───────┘                               │
      │                 ▼                                       │
      │            ┌─────────┐  Ralph 外部裁决                  │
      │            │ JUDGING │                                  │
      │            └────┬────┘                                  │
      │                 ├── 全部断言通过 ────────► CONVERGED ✅  │
      │                 ├── 预算耗尽 ───────────► BUDGET_EXCEEDED│
      │                 ├── 轮次耗尽 ───────────► MAX_ITERATIONS │
      │                 ├── 收益递减 ───────────► STALLED        │
      │                 └── 需修正                               │
      │                     ▼                                    │
      │            ┌──────────┐                                  │
      └────────────┤ REVISING │◄─────────────────────────────────┘
        生成修正指令└──────────┘        人工批准后恢复
```

终态共 8 个：`CONVERGED`、`REJECTED`、`BLOCKED`、`BUDGET_EXCEEDED`、`MAX_ITERATIONS`、`STALLED`、`FAILED`、`CANCELLED`。区分这么细是为了让失败可归因 —— "撞预算"和"反馈无效导致原地打转"需要完全不同的处理动作。

## 3. 目标（Goal）与断言（Assertion）

Goal 不是一段自然语言，而是**断言集合 + 预算 + 策略**的结构化对象：

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

class AssertionKind(str, Enum):
    COMMAND = "command"        # 退出码 == 0
    SCHEMA = "schema"          # JSON Schema 校验
    METRIC = "metric"          # 数值指标比较
    REGEX = "regex"            # 正则匹配/不匹配
    HUMAN = "human"            # 人工审批

@dataclass(frozen=True)
class Assertion:
    id: str
    kind: AssertionKind
    # COMMAND: {"cmd": "pytest -q"}
    # METRIC:  {"name": "factuality", "op": ">=", "value": 85}
    spec: dict[str, Any]
    weight: float = 1.0
    blocking: bool = True      # False 表示"希望满足但不阻塞收敛"
    hint: str = ""             # 失败时给模型的定向提示

@dataclass(frozen=True)
class Budget:
    max_iterations: int = 10
    max_total_tokens: int = 200_000
    max_cost_usd: float = 1.0
    max_tokens_per_iteration: int = 32_000
    max_wall_clock_seconds: int = 900

@dataclass(frozen=True)
class Goal:
    task: str                              # 任务描述（给模型看）
    assertions: tuple[Assertion, ...]
    budget: Budget = field(default_factory=Budget)
    mode: Literal["retry", "quality", "verify_execute", "hitl"] = "quality"
    stall_threshold: float = 2.0           # 得分提升低于此值算无进展
    stall_patience: int = 2                # 连续 N 轮无进展则 STALLED
```

**创建时的可验证性校验**（`VALIDATE` 状态）会拒绝以下情况，直接返回 `REJECTED` 并附带原因：

- `assertions` 为空，或全部 `blocking=False` —— 没有任何硬性收敛条件。
- `METRIC` 断言引用了当前项目未配置的评估器。
- `COMMAND` 断言在无沙箱环境下声明（无法安全执行）。
- 预算低于单轮最小可行值（例如 `max_total_tokens < max_tokens_per_iteration`）。

这一步是整个设计的把关点：**把"模糊指令"挡在系统之外**，而不是等 Loop 跑 10 轮烧完预算才发现目标本身没法判定。

## 4. Ralph Verifier —— 外部强制验证

借鉴 Claude Code 社区 2025-2026 年形成的 Ralph Loop 范式：**不信任模型的自我评估，由外部机制强制判断任务是否真正完成。**

实现要点：

```python
@dataclass(frozen=True)
class Verdict:
    converged: bool
    passed: tuple[str, ...]          # 通过的断言 id
    failed: tuple[AssertionFailure, ...]
    score: float                     # 加权得分（仅用于趋势，不用于收敛判定）
    claimed_done: bool               # 模型是否自称完成（仅记录，用于统计假完成率）
```

三条硬性规则：

1. **收敛判定只看 `blocking=True` 的断言是否全过**。`score` 只用于画趋势图和 STALLED 检测，绝不作为收敛依据 —— 否则又回到了"分数可被讨好"的老问题。
2. **`claimed_done` 与 `converged` 分开记录**。两者的差集就是"假完成"，这是产品的关键指标（目标拦截率 ≥ 95%）。当模型自称完成但断言未过时，日志与前端明确标注 `FALSE_COMPLETION`。
3. **Verifier 的执行环境与被验证对象隔离**。`COMMAND` 类断言在沙箱中执行；`METRIC` 类断言若依赖 LLM-as-Judge，则 Judge 模型必须与生成模型是不同实例（配置上强制校验，见 [05 评测引擎](05-evaluation.md)）。

五类 Verifier 的注册与选择：

| kind | 实现 | 信号强度 | 典型用途 |
|---|---|---|---|
| `COMMAND` | 沙箱执行命令，取退出码 + stdout/stderr | ★★★★★ | pytest、tsc、ruff、build |
| `SCHEMA` | JSON Schema / Pydantic 校验 | ★★★★★ | 结构化输出、API 响应格式 |
| `REGEX` | 正则必含/必不含 | ★★★★ | 引用格式、禁用词、Markdown 结构 |
| `METRIC` | 评估器输出数值比较 | ★★★ | 事实性、指令遵循率、相似度 |
| `HUMAN` | 等待人工审批（转 HUMAN_PENDING） | ★★★★★ | 高敏感决策、发布前确认 |

信号强度决定了 Loop 的收敛效率。这也是为什么代码生成场景（可用 `COMMAND`）是首版标杆场景 —— 反馈信号是二值且无歧义的。

## 5. Critique Synthesizer —— 结构化修正指令

最常见的错误做法是把整个 eval 结果 JSON 原样塞回模型。这会导致三个问题：上下文膨胀、模型注意力被无关信息稀释、模型可能"讨好分数"而非解决问题。

Ariadne 的做法是从失败断言生成**结构化修正指令**：

```python
@dataclass(frozen=True)
class Critique:
    failures: tuple[str, ...]        # 具体失败项（人话描述，不是 assertion id）
    evidence: tuple[str, ...]        # 截断后的证据片段（如失败的测试名 + 报错首行）
    directives: tuple[str, ...]      # 明确的修正动作
    forbidden: tuple[str, ...]       # 上几轮试过且失败的方向，避免重复
```

生成规则：

- **证据必须截断**。stderr 只取前 20 行 + 后 5 行；超长 diff 只保留变更块。防止一个 stack trace 吃掉整个上下文预算。
- **directives 优先来自 `Assertion.hint`**（人工预设的定向提示），其次由模板生成，最后才回退到 LLM 生成 critique。人工提示的效果通常远好于模型自己总结。
- **`forbidden` 是防振荡的关键**。把历史失败尝试的摘要显式列出，告诉模型"这些路走过了"。

## 6. 上下文收敛策略

Loop 的 Token 消耗随轮次线性甚至超线性增长，根因是"把全量历史都塞进去"。收敛策略是把每轮上下文固定为四段：

```
[固定] 任务规格（task + 断言的人话描述）        ← 每轮不变，可被 provider 缓存
[最新] 上一轮输出（或其 diff）                  ← 代码类场景用 diff，内容类用全文
[聚焦] 本轮 Critique（失败项 + 证据 + 指令）    ← 结构化，长度可控
[压缩] 历史摘要（前 N-2 轮的失败签名列表）      ← 只保留"试过什么、为什么不行"
```

三条量化约束：

- 上下文总量不超过 `budget.max_tokens_per_iteration` 的 60%，剩余留给输出。
- 历史摘要**只保留失败签名**（每轮一行，如 `轮次2: 格式断言失败-缺少代码块`），不保留完整输出。
- 超过 5 轮时启用二次压缩：把前 3 轮摘要合并为一行趋势描述。

工程收益：把固定段放在最前面，可以命中各 provider 的 prompt 缓存，实测能显著压低多轮场景的实际计费成本。缓存命中的 Token 需在成本归因中按折扣价单独计量（见 [05 评测引擎](05-evaluation.md#4-成本归因)）。

## 7. 振荡与停滞检测

Loop 的第二大失效模式不是"改不好"，而是**原地打转**：第 3 轮改回了第 1 轮的错误。

检测机制基于两个指纹：

```python
output_fp  = sha256(normalize(output))          # 归一化后的输出哈希
failure_fp = sha256("|".join(sorted(failed_ids)))  # 失败断言集合的签名
```

处置策略按严重度递增：

| 触发条件 | 处置 |
|---|---|
| 连续 2 轮 `failure_fp` 相同 | 升级策略：提高 temperature / 换更强模型 / 强制换解法（在 critique 的 `forbidden` 中显式禁止上一轮路径） |
| 连续 3 轮 `failure_fp` 相同 | 判定 `STALLED`，终止并报告"反馈信号无效" |
| 出现历史 `output_fp` 重复 | 判定振荡，立即升级策略并在 `forbidden` 中加入该输出摘要 |
| 得分提升连续 `stall_patience` 轮 < `stall_threshold` | 判定收益递减，提前终止省成本 |

`STALLED` 是一个**有价值的失败**：它明确告诉用户"你的断言设计给不出有效反馈信号"，而不是默默烧完 10 轮预算。前端对 STALLED 会给出诊断建议（例如"failure_fp 恒定在 metric 类断言，建议补充 hint 或改用 command 类断言"）。

## 8. 三层预算熔断

| 层级 | 检查点 | 软失败（降级） | 硬失败（终止） |
|---|---|---|---|
| **轮次层** | 每轮开始 | —— | 超过 `max_iterations` → `MAX_ITERATIONS` |
| **累计层** | 每次 LLM 调用前 | 剩余预算 < 30% → 降级到更便宜模型 | 超过 `max_total_tokens` / `max_cost_usd` → `BUDGET_EXCEEDED` |
| **单轮层** | 每次 LLM 调用前 | 上下文超限 → 触发二次压缩 | 单轮超过 `max_tokens_per_iteration` → 本轮失败，进入下一轮 |

实现上，预算计数器放在 **Redis 原子计数**，而非进程内存：并行 Loop 共享同一个预算池时，进程内计数会导致超支。检查采用"预扣 + 结算"两阶段：调用前按 `estimate_tokens` 预扣，返回后按实际用量结算差额。

`max_wall_clock_seconds` 单独由 Worker 的看门狗计时器管理，防止 Loop 卡在某个不返回的外部调用上。

## 9. 检查点与恢复

每轮 `JUDGING` 结束后写入检查点（Postgres 事务）：

```python
@dataclass(frozen=True)
class Checkpoint:
    loop_id: str
    iteration: int
    state: str
    verdict: Verdict
    critique: Critique | None
    artifact_refs: tuple[str, ...]     # S3 对象键，不存内容本身
    cumulative_tokens: int
    cumulative_cost_usd: float
    output_fp: str
    failure_fp: str
    created_at: datetime
```

恢复语义：

- Worker 通过 Redis Streams 消费者组持有任务，**可见性超时**（默认 90s）内需续租；崩溃后任务自动回到 pending，由其他 Worker 接管。
- 接管方读取最新 Checkpoint，从 `iteration + 1` 继续，**不重跑已完成轮次**。
- 副作用操作（工具调用、文件写入）用 `idempotency_key = f"{loop_id}:{iteration}:{step}"` 去重，防止接管导致重复执行。
- 累计预算从 Checkpoint 恢复，防止重启后预算被重置（这是最容易出的账单事故）。

## 10. 四种 Loop 模式

模式是"断言类型 + 退出策略 + 重试节奏"的预设组合，通过 registry 注册：

| 模式 | 适用场景 | 反馈信号 | 默认断言 | 终止条件 | 特殊行为 |
|---|---|---|---|---|---|
| **Retry** | API 失败、格式错误 | 错误码 / 异常 | SCHEMA | 成功或达最大重试 | 指数退避 + 抖动；区分可重试/不可重试错误 |
| **Quality** | 内容生成质量不达标 | 质量评分 | METRIC | 评分 ≥ 阈值 | 每轮必带 critique；启用收益递减检测 |
| **Verify-Execute** | 代码生成、数据分析 | 测试 / 执行结果 | COMMAND | 全部验证通过 | 强制沙箱；输出用 diff 而非全文 |
| **HITL** | 高敏感决策 | 人工审批 | HUMAN | 批准或拒绝 | 转 `HUMAN_PENDING` 并持久化；支持超时自动拒绝 |

```python
# loop_module/__init__.py
LOOP_MODE_FACTORY: dict[str, type[BaseLoopMode]] = {}

def register_loop_mode(name: str):
    def decorator(cls: type[BaseLoopMode]) -> type[BaseLoopMode]:
        LOOP_MODE_FACTORY[name] = cls
        return cls
    return decorator

def LoopModeFactory(name: str) -> type[BaseLoopMode]:
    if name not in LOOP_MODE_FACTORY:
        raise ValueError(f"Unknown loop mode: {name}")
    return LOOP_MODE_FACTORY[name]
```

模式可组合：`Verify-Execute` 收敛后可级联一个 `HITL` 做发布前确认。组合通过 `Goal.mode` 的列表形式表达（M4 支持）。

## 11. 并行 Loop（批量场景）

批量场景（生成 N 篇文档、修 N 个测试）用 **pipeline 语义**而非 barrier 语义：每个 item 独立跑完自己的全部轮次，不等其他 item。这样墙钟时间等于"最慢单项"，而不是"每阶段最慢项之和"。

共享资源与隔离边界：

- **共享**：全局预算池（Redis 原子计数）、并发上限、Provider 速率限制令牌。
- **隔离**：每个 item 独立的上下文、检查点、沙箱实例、失败指纹。
- **失败处理**：单项失败不影响其他项；结果集中 `None` 表示该项失败，最终报告逐项列出终态。

并发度由 `min(配置并发上限, provider 速率限制余量, 沙箱池容量)` 动态决定，避免把上游 API 打到 429。

## 12. 可观测性钩子

Loop 的每一轮都产生一个 span（`ariadne.loop.iteration`），属性包括轮次号、状态、得分、Token、成本、`failure_fp`、是否假完成。这些数据直接驱动 [07 前端可视化](07-frontend-visualization.md) 的 Loop 进化视图，无需额外埋点。

