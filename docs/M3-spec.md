# M3 实施规格：Loop Engine

> 前置：[M1](M1-spec.md) + [M2](M2-spec.md) 已完成。总体设计见 [03 Loop 引擎](03-loop-engine.md)。
>
> **这是项目的核心里程碑。** M1-M2 是任何观测平台都有的能力，M3 才是 Ariadne 的差异化。

## 1. 交付定义

不达标能自动改到达标。

M3 完成的判定标准（缺一不可）：

- 代码生成场景闭环达标率 ≥ 85%，平均 ≤ 3 轮
- 假完成拦截率 ≥ 95%（模型自称完成但断言未过时被拦住）
- `kill -9` Worker 后从检查点续跑，不重复已完成轮次、预算不重置

## 2. 范围边界

### 做

| 项 | 内容 |
|---|---|
| 状态机 | 17 状态 / 8 终态，纯函数转移 + 穷尽测试 |
| Ralph Verifier | 五类实现 + `claimed_done` 与 `converged` 分开记录 |
| Critique Synthesizer | 结构化修正指令，非自由文本 |
| 上下文收敛 | 四段固定结构 + 二次压缩 |
| 振荡/停滞检测 | 双指纹 + 策略升级 + `STALLED` 终态 |
| 三层预算熔断 | Redis 原子计数 + 预扣结算 |
| 检查点与恢复 | Redis 可见性超时接管 + 幂等副作用 |
| 四种 Loop 模式 | Retry / Quality / Verify-Execute / HITL |
| SSE 实时推送 | 轮次进展流式推送前端 |
| Loop 进化视图 | 趋势图 + 成本柱状 + 轮次 diff + 失败签名时间线 |
| 受限代码执行 | Verify-Execute 需要跑测试。M4 前用**受限子进程**（见第 5 节） |

### 不做

Harness 规则引擎（M4，M3 用硬编码的最小安全默认值）、gVisor/Firecracker 沙箱（M4）、DAG 编排 UI（M5）、并行 Loop 池（M5）、多 Loop 模式组合（M4）。

## 3. 为什么这些设计不能妥协

四条铁律，任一放弃都会让 Loop 退化成"多试几次的 prompt engineering"：

| 铁律 | 放弃的后果 |
|---|---|
| 目标必须可验证 | 烧完 10 轮预算才发现目标无法判定 |
| 不信任模型自评 | 回到 AutoGPT 的"模型说完成了就停"，任务远未达标 |
| 状态外置 | 进程崩溃从零开始，长任务永远跑不完 |
| 预算硬熔断（非告警） | 账单事故。这是 Loop 最大的现实风险 |

## 4. 核心契约

`Goal` / `Assertion` / `Budget` / `Verdict` / `Critique` / `Checkpoint` 的完整定义见 [03 Loop 引擎](03-loop-engine.md#3-目标goal与断言assertion)。这里只记 M3 实现中容易做错的部分。

### 4.1 收敛判定只看 blocking 断言

```python
def is_converged(verdict: Verdict, goal: Goal) -> bool:
    """score 只用于趋势与 STALLED 检测，绝不作为收敛依据。

    用 score 判收敛会让模型有"讨好分数"的空间，回到分数可被 gaming 的老问题。
    """
    blocking_ids = {a.id for a in goal.assertions if a.blocking}
    return blocking_ids.issubset(set(verdict.passed))
```

### 4.2 假完成必须单独记录

```python
false_completion = verdict.claimed_done and not verdict.converged
```

这是产品的关键指标（目标拦截率 ≥ 95%），写入 span 属性 `ariadne.loop.false_completion` 并在前端明确标注。

## 5. 一个必须显式面对的取舍：M3 阶段的代码执行

Verify-Execute 模式需要跑测试（`pytest -q`），但完整沙箱要到 M4。三个选项：

| 方案 | 评价 |
|---|---|
| 等 M4 做完沙箱再做 Loop | 否。M3 是核心里程碑，不该被基础设施阻塞 |
| M3 直接跑裸子进程 | 否。任意代码执行 + 无资源限制，本机开发都危险 |
| **受限子进程（选定）** | 可接受的中间态，边界明确且到 M4 平滑替换 |

受限子进程的具体约束（`runtime_module/exec/restricted.py`）：

- 独立临时工作目录，用完删除；不挂载项目其他路径
- `subprocess` 显式 `env={}`（不继承环境变量，避免泄漏 API key）
- 硬超时（默认 30s）+ 输出上限 1MB，超出杀进程组
- 非 Windows 平台用 `resource.setrlimit` 限制 CPU 时间、内存、进程数
- 命令走**白名单**：只允许 `pytest` / `ruff` / `mypy` / `node` / `tsc` / `npm test`，不接受任意 shell
- **默认禁网**做不到（子进程无法可靠隔离网络），因此配置项 `exec.allow_untrusted_code` 默认 `false`，为 true 时启动日志打 WARNING

代码里必须留明确标记，避免这个中间态被当成最终方案：

```python
# TODO(M4): 替换为 sandbox_module 的 gVisor 驱动。
# 受限子进程无法隔离网络，也无法防止内核层逃逸 ——
# 仅适用于"用户自己的代码在自己的机器上跑"这一场景。
```

**文档与配置里都要写明**：M3 阶段的 Verify-Execute 不适用于多租户或不可信代码。

## 6. 关键实现决策

### 6.1 状态机是纯函数

```python
def next_state(current: LoopState, event: LoopEvent) -> LoopState:
    """纯函数转移，无副作用、无 IO。

    这样才能穷尽测试所有 (状态 × 事件) 组合 —— 状态机是预算安全的地基，
    转移错误会导致 Loop 卡死或绕过熔断。
    """
```

测试要求：遍历全部 17 状态 × 全部事件，断言无非法转移、无不可达状态、终态不再转移。

### 6.2 预算用 Redis 原子计数，不用进程内存

```python
async def reserve(self, loop_id: str, estimated: int) -> bool:
    """预扣。返回 False 表示超预算，调用方应终止。

    进程内计数在并行 Loop 共享预算池时必然超支。
    成本用整数微美分：INCRBYFLOAT 有精度问题，预算判定必须精确。
    """
    new_total = await self._redis.incrby(f"budget:{loop_id}:tokens", estimated)
    if new_total > self._limit:
        await self._redis.decrby(f"budget:{loop_id}:tokens", estimated)  # 回滚
        return False
    return True
```

预扣 → 调用 → 按实际用量结算差额。**从检查点恢复时预算从 Checkpoint 读取**，不重置 —— 这是最容易出的账单事故。

### 6.3 副作用幂等

Worker 接管后重跑可能导致工具重复执行（重复发邮件、重复写文件）：

```python
idempotency_key = f"{loop_id}:{iteration}:{step_index}"
```

Redis `SET NX` 24h TTL。key 已存在则跳过执行并复用上次结果。

### 6.4 上下文四段结构

```python
def build_context(goal, last_output, critique, history) -> Messages:
    """固定段放最前面以命中 provider 的 prompt 缓存。

    实测能显著压低多轮场景实际计费；缓存命中的 Token 按折扣价计量
    （M1 的 pricing 已支持），否则预算熔断会误触发。
    """
```

总量不超过 `max_tokens_per_iteration` 的 60%，剩余留给输出。超 5 轮启用二次压缩。

### 6.5 SSE 从 Redis pub/sub 广播

多 API 实例都要能推给自己的连接。事件同时写 Postgres，供 `Last-Event-ID` 断线续传。高频节点更新在服务端合并为 100ms 一批。

## 7. 模块清单

```
src/ariadne/
├── loop_module/
│   ├── __init__.py           # LoopModeFactory + register_loop_mode
│   ├── state_machine.py      # 纯函数转移（~200 行，穷尽测试）
│   ├── engine.py             # 编排主循环
│   ├── goal.py               # Goal/Assertion/Budget + 可验证性校验
│   ├── verifier/
│   │   ├── __init__.py       # VerifierFactory
│   │   ├── command.py        # 退出码
│   │   ├── schema.py         # JSON Schema
│   │   ├── regex.py
│   │   ├── metric.py         # 复用 M2 的评估器
│   │   └── human.py          # 转 HUMAN_PENDING
│   ├── critique.py           # 结构化修正指令
│   ├── context.py            # 四段收敛 + 二次压缩
│   ├── fingerprint.py        # output_fp / failure_fp + 振荡检测
│   ├── budget.py             # 三层熔断（Redis 原子计数）
│   ├── checkpoint.py         # 检查点读写
│   └── modes/
│       ├── retry.py
│       ├── quality.py
│       ├── verify_execute.py
│       └── hitl.py
├── runtime_module/
│   ├── llm/                  # provider adapter（复用 M1 的 pricing）
│   ├── tools/                # 工具注册表 + 幂等键
│   └── exec/restricted.py    # M3 的受限子进程（M4 替换）
├── worker/
│   └── loop_worker.py        # Loop 任务消费 + 租约续期
└── api/
    ├── routers/loops.py      # 9 个端点
    └── sse.py                # Redis pub/sub → SSE
```

前端新增：Loop 列表页、Loop 进化视图（趋势 + 成本 + diff + 失败签名时间线）、审批页（HITL）、Loop 创建表单（含目标可验证性即时校验）。

## 8. 技术栈增量

| 选择 | 用途 | 理由 | 被否方案 |
|---|---|---|---|
| **Redis Streams 消费者组** | Loop 任务队列 | 需要可见性超时实现崩溃接管，M1 已引入 | Celery：额外依赖，且对长任务的租约控制不够直接 |
| **`asyncio` 单进程多协程** | Loop 并发 | Loop 是 IO 密集（等 LLM），协程足够；共享预算计数器更简单 | 多进程：预算计数跨进程要额外协调 |
| **`sse-starlette`** | SSE 推送 | FastAPI 生态标准，处理好了心跳与断连 | 手写 StreamingResponse：要自己管心跳与背压 |
| **`diff-match-patch`（前端）** | 轮次 diff | 文本 diff 成熟实现 | 手写 LCS：边界情况多 |
| **`Shiki`（前端）** | 代码 diff 高亮 | 语法感知，代码类 Loop 的 diff 可读性关键 | Prism：语法支持不如 Shiki |
| **`psutil`** | 子进程资源监控 | 跨平台读取子进程内存/CPU，Windows 无 setrlimit 时的兜底 | 只用 setrlimit：Windows 不支持 |

**不引入 LangGraph**：它的状态机与检查点是为通用 agent 编排设计的，而 Ariadne 需要的是"外部强制验证 + 预算熔断"这套特定语义。用它会被它的抽象绑住。M5 会做**导入 LangGraph 图定义**的兼容（见待决策项 D2），但执行器自研。

## 9. 数据库增量

Postgres：`loop_runs`（17 状态 CHECK 约束 + 租约字段）、`loop_checkpoints`（每轮一条不可变）、`approvals`、`specs`。

ClickHouse：`loop_iterations`（驱动进化视图）、`loop_outcomes`（SLI 源）、`mv_loop_outcomes`。DDL 见 [08 数据模型](08-data-model.md#22-其他-clickhouse-表)。

Redis：`q:loop`、`budget:{loop_id}:tokens`、`budget:{loop_id}:cost`、`budget:pool:{project_id}`、`sse:{loop_id}`、`idem:{key}`。

## 10. 验收清单

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | 状态机无非法转移 | 穷尽 17×N 组合的单元测试 |
| 2 | 不可验证目标被拒 | 空断言 / 全 non-blocking / 未知评估器 → `REJECTED` |
| 3 | 收敛只看 blocking 断言 | 高 score 但 blocking 未过 → 不收敛 |
| 4 | 假完成拦截率 ≥ 95% | 构造模型自称完成但断言失败的样本集 |
| 5 | 闭环达标率 ≥ 85%（代码场景） | 真实用例集：故意写坏的测试让 Loop 修 |
| 6 | 平均 ≤ 3 轮 | 同上，统计 `CONVERGED` 的 iteration 均值 || 7 | 振荡被检测 | 构造反复输出同一错误的 mock 模型 → `STALLED` |
| 8 | 预算硬熔断 | 设极小预算 → `BUDGET_EXCEEDED`，且实际用量不超限 |
| 9 | 崩溃恢复不重跑 | `kill -9` Worker，另一 Worker 接管，校验 iteration 不回退 |
| 10 | 恢复后预算不重置 | 崩溃前已用 80% 预算，恢复后仍只剩 20% |
| 11 | 副作用不重复 | 带副作用的工具在接管后不二次执行 |
| 12 | SSE 断线续传 | 断开后带 `Last-Event-ID` 重连，无事件丢失 |
| 13 | 上下文不随轮次线性膨胀 | 10 轮 Loop 的单轮上下文量应基本持平 |

第 5、6 项需要**真实用例集**作为外部依赖：一组故意有缺陷的代码仓库快照 + 对应的测试。建议 20-30 个用例，覆盖语法错误、逻辑错误、缺失边界处理、性能问题四类。

## 11. 实施进度

### 已完成

| 模块 | 内容 | 测试 |
|---|---|---|
| `state_machine.py` | 17 状态 / 8 终态，显式转移表，纯函数 | 52 |
| `goal.py` | Goal/Assertion/Budget + 可验证性校验（error 与 warning 分级） | 35 |
| `budget.py` | 三层熔断 + 预扣结算 + 检查点恢复 | 30 |
| `redis_counter.py` | 生产用原子计数器 + 项目级共享池 | —（需容器） |
| `fingerprint.py` | 双指纹 + 振荡/停滞/收益递减检测 | 33 |
| `verifier/base.py` | Verdict + 假完成判定 + 加权得分 | 30 |
| `verifier/builtin.py` | SCHEMA / REGEX / METRIC / HUMAN 四类 | （同上） |
| `verifier/command.py` | COMMAND 类 + Runner 抽象 | （同上） |
| `verifier/restricted_exec.py` | 受限子进程（M3 过渡方案） | 35 |
| `critique.py` | 结构化修正指令 + 历史压缩 | 26 |
| `context.py` | 四段收敛 + 二次压缩 + diff 模式 | （同上） |
| `checkpoint.py` | Checkpoint + CheckpointStore Protocol + InMemory 实现 | 19 |
| `modes/` | 四模式（retry/quality/verify_execute/hitl）+ registry/factory | （含上） |
| `engine.py` | 状态机驱动主循环；IO 全 Protocol，纯桩可测全路径 | 19 |
| `loop_models.py` | Postgres 表：loop_runs + loop_checkpoints（DDL 见 docs/08） | — |
| `loop_checkpoint_repo.py` | CheckpointStore 的 Postgres 实现 + 序列化往返 + 幂等写 | 8 |
| `repositories/loop_runs.py` | loop_runs 仓储：租约生命周期 + 终态落库 + 接管扫描 | 15 |
| `worker/loop_queue.py` | Redis Streams 消费者组任务队列（XREADGROUP + XCLAIM 回收） | — |
| `worker/loop_worker.py` | Loop Worker：认领 → 租约 → engine 执行 → 终态落库 + Redis pub/sub 事件 | 8 |
| `api/routers/loops.py` | 8/9 个 Loop 端点（创建/列表/详情/迭代/取消/恢复/审批/批量） | 17 |
| `api/sse.py` | SSE 实时事件流（Redis pub/sub → StreamingResponse + 心跳） | — |
| `runtime_module/llm/` | Anthropic Messages API 适配器（httpx 直连，无 SDK 依赖）+ 工厂 | 13 |
| `web Loop 页面` | LoopListPage（列表 + 创建表单 + SSE 实时刷新 + 取消/审批）与 LoopDetailPage（得分趋势 + Token 柱状 + 轮次表 + 终态诊断），ECharts 进化视图 | — |

累计 **690 项测试通过**（+53），ruff 全绿，mypy 新代码零错误（既有 harness_module
的 celpy 类型告警为 M4 范畴）。前端 `tsc --noEmit` + `vite build` 全过，SPA
路由 `/loops` 与 `/loops/:id` 已挂载（API 静态服务 dist）。
Alembic 迁移 `a1b2c3d4e5f6` 加 loop 两表，`b2c3d4e5f6a7` 加 error 列。

**SSE 读取修复（真实 bug，2026-08-26）**：`_event_stream` 原用 `get_message(timeout=5.0)`
轮询。实测发现 Windows Redis 5.0 的 PUBLISH→SUBSCRIBE 传播有亚秒延迟，轮询会让
事件延迟 0-5s（前端进化视图实时性打折），且消息若恰好落在 timeout 边界会漏。
已改为 `listen()` 阻塞读取 + `asyncio.wait_for` 心跳超时：事件零延迟，空闲时
每 15s 发心跳。新增 3 个集成测试（事件接收/连续事件/优雅取消，连真实 Redis，
连不上自动 skip）。

**HTTP 全链路端到端（2026-08-26）**：真实 Postgres(5433) + Redis5(6380) +
API(8000) + Worker 桩 LLM，经 HTTP 完整验证：
`POST /v1/loops`(202) → Worker 消费 Redis Streams → engine 执行 → RedisEventSink
广播 → SSE 端点实时推送 → `GET /v1/loops/{id}` CONVERGED + iterations score=100。
SSE 事件顺序完整：START → GOAL_VALID → CONTEXT_READY → RULES_PASSED →
EXECUTION_DONE → EVALUATION_DONE → CONVERGED。
**队列接管验证**：Worker A 认领但不 ACK（模拟崩溃）→ Worker B 在可见性内取不到
（防双跑）→ XCLAIM 强制接管 → ACK 后 pending 归零。

累计 **693 项测试通过**（+3 SSE 集成），ruff + mypy strict 全绿。

**模型名修复（真实 bug）**：modes 的 `base_model` 是占位符（`quality-default`），
若生产装配不传模型名，engine 会用占位名请求真实 API → 404 或 0 成本。已在
`LoopConfig` 加 `model`/`degraded_model` 字段，Worker 从 `settings.llm` 传入
真实模型名；`model_for()` 优先用配置，degrade 时用 `degraded_model`。

累计 **677 项测试通过**，ruff + mypy strict 全绿（新增文件零错误；既有 harness_module 的 celpy 类型告警为 M4 范畴）。
Alembic 迁移 `a1b2c3d4e5f6` 加 loop 两表，`b2c3d4e5f6a7` 加 error 列。

**端到端验证（2026-08-26，无 Docker 环境）**：Docker/WSL 不可用（磁盘损坏），改用
本机组件验证——Postgres 18 独立实例（D:\ariadne-pg，端口 5433）+ Redis 5.0
（D:\redis-5.0，端口 6380，tporadowski Windows 移植版）。API 创建 → Worker 从
真实 Redis Streams 入队/认领 → 桩 LLM 执行 → CONVERGED 落库 → API 查询终态，
全链路通过。SSE pub/sub 事件流验证通过（START → GOAL_VALID → CONVERGED 顺序
到达）。AnthropicLLMClient（mock HTTP 层）驱动真实 engine 全路径：模型名正确、
成本按 PricingTable 结算（$0.002475 两轮）、收敛。
崩溃接管验证：Worker A 执行后落检查点（iteration=1, JUDGING）→ 租约过期 →
Worker B 从检查点续跑 → CONVERGED，iteration=2 不回退、tokens 累计（600）不重置。
振荡检测真实存储验证：连续相同失败输出 → 4 轮后 STALLED 终态（不烧满预算）。

累计 **642 项测试通过**，ruff + mypy strict 全绿（98 文件）。
Alembic 迁移 `a1b2c3d4e5f6` 加 loop 两表，upgrade/downgrade SQL 已验证。

**端到端验收（验收项 5/6）**：`tests/test_loop_benchmark.py` 用 COMMAND 断言
真跑 pytest 完成验收，数据见第 11 节。`test_loop_engine.py::TestEndToEndConvergence`
里那组 REGEX 代理用例保留为快速回归 —— 它当初的理由"受限子进程在无 venv
环境跑不了 pytest"其实指向一个真实缺陷：`restricted_exec` 把裸 `python`
交给 Popen 自己解析，而传给子进程的 `env["PATH"]` 并不影响 Windows 上
CreateProcess 的搜索，于是解析到系统安装而非项目 venv。现已改为用受控 PATH
显式解析（`_resolve_executable`）。

**穷尽测试的落实**：状态机遍历全部 17×21 = 357 个 (状态×事件) 组合，断言无非法转移被静默忽略、无不可达状态、终态不再转移、任何流转态都可取消。

**Engine 验收（M3-spec 第 10 节可单测项）**：收敛只看 blocking、假完成拦截、预算硬熔断、振荡 STALLED、崩溃恢复不重跑、恢复后预算不重置、上下文不线性膨胀，均用纯桩（ScriptedLLM + FakeClock + InMemoryCheckpointStore）验证，不依赖真实 provider 或容器。

### 关键设计的验证方式

| 铁律 | 对应测试 |
|---|---|
| 目标必须可验证 | 空断言 / 全 non-blocking / 未知指标 / 无沙箱声明 command 均被拒 |
| 不信任模型自评 | `claimed_done=True` 且断言未过 → 不收敛且标记 `false_completion`；高分不能掩盖 blocking 失败 |
| 状态外置 | 预算与振荡历史各有"不恢复就失效"的反证测试；每轮落检查点 + 恢复后 iteration 不回退 |
| 预算硬熔断 | 预扣被拒时回滚、失败释放不吃预算、恢复后不重置、墙钟超时终止 |

### 开发中抓到的真 bug

7. **Windows 引号路径破坏白名单校验**。`shlex.split(posix=False)` 保留引号字符，取出的文件名成了 `python.exe"`，而带空格的路径在 Windows 上必须加引号 —— 任何装在 `Program Files` 下的解释器都会被白名单拒绝。已在 `_executable_name` 与 `validate_command` 两处剥引号。
8. **执行失败走 EVALUATING 会崩**。`_step` 最初在 EXECUTION_FAILED → JUDGING 后仍走 EVALUATING，但执行失败时没有新输出、`_last_outcomes` 为空，`_judging` 访问 `None.claimed_done` 崩溃。已拆出 `_judge_execution_failure` 路径：不跑 Verifier，合成失败裁决 + 执行错误 critique，直接 NEEDS_REVISION。
9. **恢复后状态停在 JUDGING 无法续跑**。检查点落盘时状态是 JUDGING，恢复后若直接进主循环，`_planning` 从 JUDGING 发 CONTEXT_READY 是非法转移。`_restore` 现把 JUDGING 映射为 REVISING，由 `_planning` 先发 REVISION_READY 回到 PLANNING。
10. **完全相同输出反复只升级不终止**。`OscillationDetector.record` 原先 output_fp 一重复就返回 OSCILLATING（升级），永不到 STALLED，模型卡死时 Loop 会烧满预算。已改为：重复达 `stall_after` 次后判 STALLED，1-2 次才升级。

### 受限子进程的边界（已写成测试固化）

**能防**：命令白名单（拒 shell 与 `python -c`）、不继承环境变量（真实子进程验证密钥不泄漏）、硬超时杀进程树、输出上限、路径穿越拒绝、注入无效（不走 shell）。

**防不住**：禁网、工作目录外的文件读取、syscall 滥用；Windows 上还缺 CPU/内存限制。这三项写成了 `TestDocumentedLimitations`，避免边界被误解成"已经安全了"。

### 待完成

| 项 | 阻塞原因 |
|---|---|
| 浏览器级 UI 实测 | 前端构建/类型全过、API 契约对齐，但本机 preview 工具不可用，交互未在真实浏览器点击验证 |
| 闭环达标率的**能力**验收 | 机制验收已完成（见下），但"真实 LLM 能修好多少缺陷"需要 provider key + 一个未经调参的缺陷集 |

### 验收项 4/5/6 —— 2026-09-01 实测

用例集 `tests/loop_cases.py`（20 例：17 可修复 / 3 不可修复，按缺陷类型枚举），
断言层 `tests/test_loop_benchmark.py`。**产出物真落盘、pytest 真执行、
收敛判定走真实 Verifier** —— COMMAND 断言第一次进入闭环验收。

| 验收项 | 门槛 | 实测 | 性质 |
|---|---|---|---|
| 4 假完成拦截率 | ≥ 95% | **27/27 = 100%** | 真验收 |
| 5 闭环达标率 | ≥ 85% | **17/17 = 100%** | 机制验收 |
| 6 平均轮次 | ≤ 3 | **2.00** | 机制验收 |

三个指标的可信度不同，不要混着读。验收项 4 的被测对象是 Ralph 机制本身，
与模型智能无关，因此是真验收。5/6 的模型是脚本化的，测的是"模型在第 N 轮
给出正确实现时闭环能否恰好在那一轮识别并停止"，**不等于**"闭环能修好 85%
的真实缺陷"。

拦截率的真值刻意**不取自 Verifier**：拿 Verifier 的判定同时当分子和分母，
拦截率会恒等于 100%，是个永远不会红的装饰。真值由用例集声明
（`expected_iterations=N` ⇒ 第 1..N-1 轮的实现是错的），因此 Verifier 只要
把失败当成功哪怕一轮，指标就会掉下去。

**这三项此前无法验收的真正原因**（不是"缺 API key"）：代码生成闭环缺了
一整环 —— 模型输出从不落盘，COMMAND 断言每轮跑的都是同一份没变过的文件。
实测过：缺陷版 `add` + 第 2 轮就给出正确实现的脚本模型，终态
`MAX_ITERATIONS`、磁盘文件仍是缺陷版。补齐 `loop_module/artifact.py` 后
同一场景 2 轮 `CONVERGED`。详见 [11-roadmap.md](11-roadmap.md) 的 R12。

## 12. 工期与顺序

预计 6 周：

1. **第 1 周**：状态机 + Goal 可验证性校验 + 穷尽测试（地基，必须先做对）
2. **第 2 周**：预算三层熔断 + 检查点与恢复（第二地基，账单安全）
3. **第 3 周**：Ralph Verifier 五类 + 假完成统计
4. **第 4 周**：Critique Synthesizer + 上下文收敛 + 振荡检测
5. **第 5 周**：四种模式 + 受限子进程 + SSE
6. **第 6 周**：Loop 进化视图前端 + 端到端验收

顺序理由：状态机与预算是正确性地基，先做对再往上叠功能。反过来先做 Verifier 会导致地基改动时上层全部返工。

