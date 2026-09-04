# M4 实施规格：Harness 与沙箱

> 前置：[M3](M3-spec.md) 已完成。总体设计见 [04 Harness 与沙箱](04-harness-and-sandbox.md)。

## 1. 交付定义

不该发生的事绝不发生 —— 且不能通过"多试几轮"绕过。

M4 完成的判定标准：红队测试用例集全部被拦；沙箱逃逸测试集全部失败（即防护有效）；规则求值 p99 < 5ms。

## 2. 范围边界

### 做

| 项 | 内容 |
|---|---|
| 四类规则 | input / output / resource / tool |
| CEL 求值 | 沙箱安全的表达式语言 + 内置函数库 |
| 五个执行卡点 | pre_model / post_model / pre_tool / post_tool / pre_persist |
| 六种动作 | allow / warn / block / rewrite / require_approval / route |
| 冲突消解 | 确定可复现的优先级顺序 |
| gVisor 沙箱 | 三档 profile + 预热池 + 运行时镜像 |
| **替换 M3 的受限子进程** | Verify-Execute 改走真沙箱 |
| `spec.yaml` 驱动 | 单一事实源，Loop 与 Harness 都从它派生 |
| `rules test` | 用样例载荷试跑规则 |
| HITL 审批流 | 完整化（含超时自动拒绝） |
| 审计日志 | 不可变 append-only |
| Loop 模式组合 | `Verify-Execute` 收敛后级联 `HITL` |

### 不做

多租户隔离与 RBAC（M6）、Firecracker（M6 的高安全档）、DAG 编排 UI（M5）。

## 3. 为什么 Harness 必须独立于 Loop

这是架构上最容易做错的地方：把"硬约束"和"软目标"合并会让安全机制失效。

| | Loop（软目标） | Harness（硬约束） |
|---|---|---|
| 失败处理 | 生成 critique，重试 | 直接 block |
| 能否被多试几轮绕过 | 是（这是设计意图） | **绝对不能** |
| 求值性质 | 有状态、依赖历史 | 无状态纯函数、只看当前上下文 |
| 超时策略 | 计入预算 | **fail-closed**（求值超时视为拒绝） |

fail-closed 是底线：规则引擎自己挂了，不能变成放行一切。

实现上，`block` 动作让 Loop 直接进 `BLOCKED` 终态，**不生成 critique、不进入下一轮**。这与断言失败的处理路径完全不同。

## 4. 为什么用 CEL 而不是自研 DSL 或 eval()

| 方案 | 否决理由 |
|---|---|
| Python `eval()` | 规则文件可能来自租户上传 → 任意代码执行。绝对不行 |
| 自研 DSL | 求值时间无上界，无法满足 fail-closed 的超时要求；且要自己做词法/语法/安全审计 |
| **CEL（选定）** | 无副作用、无循环、求值时间有上界、有成熟实现（`cel-python`）、语法接近 Python |

内置函数库在**宿主侧**实现，规则侧只能调用不能定义。`matches()` 必须带回溯上限防 ReDoS。

## 5. 关键实现决策

### 5.1 所有出站调用必经卡点

```python
class GuardedLLMAdapter:
    """Runtime 层的唯一出口。不提供绕过路径 ——
    任何"临时跳过 Harness"的开关最终都会变成生产事故。
    """

    async def chat(self, request: ChatRequest, ctx: HarnessContext) -> ChatResponse:
        decision = await self._harness.evaluate("pre_model", request, ctx)
        request = self._apply(decision, request)      # rewrite 在此生效

        response = await self._inner.chat(request)

        decision = await self._harness.evaluate("post_model", response, ctx)
        return self._apply(decision, response)
```

Tool Registry 同构。代码评审重点检查是否存在未走 `guard()` 的调用路径。

### 5.2 冲突消解必须确定可复现

```python
ACTION_PRIORITY = ("block", "require_approval", "route", "rewrite", "warn", "allow")

def resolve(hits: Sequence[RuleHit]) -> RuleHit:
    """同优先级按 severity 再按 rule_id 字典序。

    确定性是审计的前提：同样的输入必须永远得到同样的裁决，
    否则事后无法复现"当时为什么放行了"。
    """
```

### 5.3 新规则默认 warn

新建规则的默认 action 是 `warn` 而非 `block`。让用户先观察命中情况再收紧 —— 一上线就误拦生产流量是规则引擎最常见的事故。收紧到 `block` 必须是显式动作。

### 5.4 沙箱用完即毁

不复用实例，避免跨租户/跨轮次状态残留。冷启动开销用**预热池**缓解（`N = 并发上限 × 1.5`），gVisor 冷启动 ~100-200ms，预热后取用近似 0。

### 5.5 依赖预热而非联网装包

`strict` 档禁网，因此不能在沙箱内 `pip install`。超出预构建镜像的依赖走"依赖预热"流程：受控网络下预装并生成新镜像层，缓存复用。

## 6. 模块清单

```
src/ariadne/
├── harness_module/
│   ├── __init__.py           # RuleFactory + register_rule
│   ├── models.py             # Rule / RuleHit / Decision / HarnessContext
│   ├── loader.py             # YAML 规则集加载 + schema 校验
│   ├── evaluator.py          # CEL 求值 + 超时 fail-closed
│   ├── functions.py          # 内置函数库（宿主侧实现）
│   ├── resolve.py            # 冲突消解（纯函数）
│   ├── actions.py            # 六种动作的执行
│   ├── rules/                # 四类内置规则集
│   │   ├── input.yaml
│   │   ├── output.yaml
│   │   ├── resource.yaml
│   │   └── tool.yaml
│   └── audit.py              # 不可变审计记录
├── sandbox_module/
│   ├── __init__.py           # SandboxFactory
│   ├── base.py               # BaseSandbox 抽象
│   ├── profiles.py           # strict / standard / trusted 三档规格
│   ├── gvisor.py             # runsc 驱动
│   ├── pool.py               # 预热池
│   └── images.py             # 运行时镜像与依赖预热
├── spec_module/
│   ├── schema.py             # spec.yaml 的 Pydantic 模型
│   ├── loader.py             # 加载 + 派生 Goal 与规则集
│   └── validate.py           # 含目标可验证性校验
└── api/routers/
    ├── rules.py              # GET/PUT /v1/rules, POST /v1/rules/test
    ├── specs.py
    └── approvals.py          # 完整化 HITL
```

前端新增：规则管理页（含 `rules test` 试跑面板）、审计日志页、spec 编辑器、沙箱池状态面板。

## 7. 技术栈增量

| 选择 | 用途 | 理由 | 被否方案 |
|---|---|---|---|
| **`cel-python`** | 规则表达式求值 | 见第 4 节 | `asteval` / 自研：安全性与求值上界都不如 CEL |
| **gVisor (`runsc`)** | 沙箱隔离 | 用户态内核拦 syscall，防逃逸强于裸 Docker，启动开销远低于完整 VM | 裸 Docker：共享宿主内核，逃逸风险不可接受 |
| **`presidio-analyzer`（可选）** | PII 检测增强 | 比正则准确，支持上下文判断 | 只用正则：M1 的正则够用但召回有限 |
| **`docker` SDK / `containerd` client** | 沙箱生命周期 | gVisor 通过 runtime class 挂在容器运行时上 | 直接调 `runsc` CLI：进程管理要自己做 |
| **宿主侧规范化 + 词法族匹配**（`harness_module/injection.py`） | prompt injection | 见下 | 开源分类模型 / provider moderation API，见下 |

### prompt injection 检测的选型（决策已定，2026-08-31）

判据按原计划取「红队用例集上的召回率与 p99 延迟」。实测结果：

| 方案 | 召回（语料内 / 语料外） | 误报 | p99 延迟 | 结论 |
|---|---|---|---|---|
| 旧实现（3 条字面短语正则） | 8/19 = 42% / 未测 | 0 | < 1ms | 被替换 |
| **规范化 + 词法族匹配** | **19/19 / 14/14** | **0/22** | **0.11ms** | **采用** |
| 开源分类模型（如 deberta 类） | 未实测 | — | 10~50ms（CPU） | 否决 |
| provider moderation API | 未实测 | — | 100~500ms（网络） | 否决 |

**为什么否决分类模型**：规则求值的超时是**逐规则 100ms 且超时即 fail-closed 判成命中**（`evaluator.DEFAULT_TIMEOUT_MS`），而随包规则集的求值本身已经 13~27ms（见第 11 节）。再叠 10~50ms 的 CPU 推理会把合法输入推过超时线，代价不是「响应变慢」而是**正当请求被误拦**。另外要拖进 torch/transformers（数百 MB）与模型权重分发，对「单机 Docker Compose 一键起」的部署目标是实质退化。

**为什么否决 moderation API**：pre_model 卡点在每次 LLM 调用的热路径上，加一次网络往返（100~500ms）直接翻倍首 token 延迟；把用户输入外发给第三方与 D4「自托管优先、核心用户是数据敏感团队」冲突；且它把安全判定变成对外部可用性的硬依赖 —— API 挂了只能在「fail-closed 全拦」和「fail-open 破防」之间选，两个都不可接受。

**为什么规范化能把召回从 42% 提到全量**：旧实现的根因不是「短语枚举得不够多」，而是**拿原始文本比字面短语**。零宽字符、同形字、换行、逐字母插空格任一即可绕过，而这些手法与短语无关。规范化（零宽剥离 → NFKC → 同形字归一 → casefold → 空白折叠）把它们结构性消掉；同义替换那一类则靠「祈使动词 × 指令类宾语」的二维拆分覆盖 —— 加一个同义词只改一处，不必为每个组合写一条正则。

**仍然存在的已知缺口**（都记在 `tests/redteam_cases.py`，非静默）：

1. **纯语义诱导**无词法特征（讲一个故事把模型带到越界结论），词法层原理上覆盖不到。对策是隔离层（[docs/10 第 5 节](10-security.md)）而非检测层。
2. **超长输入是有界扫描**：超过 `MAX_SCAN_CHARS`（200K 字符）的部分不扫；200K 以内按关键词邻域归约，预算 24KB（`_REDUCED_BUDGET`）。混淆路径另有 24KB 上限。这些界是为了守住 100ms fail-closed 线 —— 不设界时 200KB 输入要 165ms，会把合法长输入判成注入。
3. **语料外召回 14/14 有乐观偏差**：这 14 条是在检测器写完后补的，但补齐缺口时又据它们调过模式。真正无偏的估计要等下一批全新手法。首次语料外实测（未针对性调整前）是 6/14 = 43%，据此补了 7 条词法族与 1 个新手法族。

误报为 0 的代价写在判定里：结构信号（`system:`、伪造边界、`unrestricted`）单独出现全部放行，"索取系统提示词"只作共现信号。理由是本平台的正当用途就包含分析和改写 prompt，而这条规则的动作是 block/critical。

## 8. 沙箱三档 profile 的强制项

三档都不可关闭的约束（详见 [04 文档](04-harness-and-sandbox.md#6-沙箱规格)）：

- 禁止宿主目录挂载 —— 代码通过预构建镜像 + artifact 注入
- 禁止提权：`no-new-privileges`、drop 全部 capabilities、禁 `ptrace`
- **禁止访问云元数据端点**（`169.254.169.254` 等）—— 容器逃逸拿凭证的常见路径
- 输出上限 stdout/stderr 各 1MB
- 执行完立即销毁

## 9. 验收清单

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | 规则求值 p99 < 5ms | 基准测试，1000 次求值 |
| 2 | 求值超时 fail-closed | 注入死循环表达式（CEL 无循环，用超大输入）→ 拒绝 |
| 3 | 冲突消解确定可复现 | 同一批规则命中，多次求值结果一致 |
| 4 | `block` 不可通过重试绕过 | Loop 遇 block → `BLOCKED` 终态，不进下一轮 |
| 5 | 危险命令被拦 | 红队用例集（`rm -rf`、fork bomb、`dd`、云元数据访问） |
| 6 | 注入检测召回 | 红队注入用例集，记录召回率与误报率 |
| 7 | 沙箱逃逸测试全部失败 | 逃逸用例集（挂载逃逸、提权、syscall 滥用、网络突破） |
| 8 | 禁网生效（strict 档） | 沙箱内发起出站请求 → 失败 |
| 9 | 沙箱用完即毁 | 前一次写的文件在下一次不可见 |
| 10 | 预热池降低冷启动 | 有池 vs 无池的 p50 取用延迟对比 |
| 11 | `rules test` 可用 | 样例载荷 → 正确报告命中与裁决 |
| 12 | 审计不可篡改 | 应用角色 UPDATE/DELETE 被拒 |
| 13 | 新规则默认 warn | 新建规则不指定 action → warn |
| 14 | M3 的受限子进程已下线 | 代码中无 `restricted.py` 的调用路径 |

第 5-7 项需要**红队用例集**作为外部依赖，且应进 CI 长期运行 —— 安全测试不是一次性验收项。

**第 5-6 项已落地**：语料 `tests/redteam_cases.py`（按攻击手法枚举，非按实现路径），断言 `tests/test_redteam.py`（分层归因：规则层 / `ExecPolicy` 策略层）。已知缺口一律标 `xfail(strict=True)` —— 缺口被意外补上会 XPASS 失败，逼着回来更新清单，缺口表靠机制维护而不靠记性。实测：

| 项 | 结果 | 说明 |
|---|---|---|
| 危险命令拦截 | **33/36** | 3 条缺口：`pytest \| sh`（非漏洞，执行层不走 shell，前提由 `TestShellIsNeverUsed` 的 AST 检查钉住）、`npx <任意包>`、`npm run <任意脚本>`（供应链管控，非命令串匹配可解） |
| 注入召回 | **29/29 = 100%**（语料内） | 换 `detect_injection()` 后语料内无缺口，棘轮 `MIN_INJECTION_BLOCKED` 已提到全量 29。原 11 条缺口（同义替换、换行、间隔字符、Unicode 同形、角色扮演、分隔符伪造、markdown、base64、中文缺时间词）全部转为 BLOCKED，并按手法族补入 10 条新用例。**语料外实测 14/14，但该批用例参与过调参，无偏估计要等下一批新手法**；首次语料外（调参前）为 6/14 = 43% |
| 误报 | **0/33** | 良性命令 15 条 + 良性输入 18 条，两层都不得误伤。良性输入从 6 条扩到 18 条，新增的每条都对应一次实测误报（如 `drop the constraint on the users table`、驼峰标识符 `ignorePreviousInstructions`、YAML 的 `system:` 键） |
| 注入检测延迟 | **p99 0.11ms**（典型输入） | 200KB 输入 ≤ 32ms，守住逐规则 100ms fail-closed 线；有界扫描的代价见第 7 节缺口 2 |

第 7 项（沙箱逃逸）仍缺，但**缺的原因已从两条收敛到一条**。原先"`SandboxRunner` 在生产从未被实例化"是装配缺口，已于 2026-08-31 闭合（见第 12 节）；现在只剩环境依赖：gVisor/Firecracker 需 Linux + runsc/KVM，本机验不了真隔离效果。

第 1 项（p99 < 5ms）是开工前定的目标，实测**达不到**且原因是结构性的（celpy 解释型
求值，单条规则约 2.6ms）。这里保留原目标不改小，实测数据与否决过的加速方案见
[第 11 节的订正](#验收项-1-的订正p99--5ms-达不到原因是结构性的)。

## 10. 工期与顺序

预计 4 周：

1. **第 1 周**：规则模型 + YAML 加载 + CEL 求值 + 内置函数 + 冲突消解（纯函数部分，可穷尽测试）
2. **第 2 周**：五卡点接入 Runtime + 六动作 + 审计
3. **第 3 周**：gVisor 驱动 + 三档 profile + 预热池 + 运行时镜像，替换 M3 的受限子进程
4. **第 4 周**：`spec.yaml` 驱动 + `rules test` + HITL 完整化 + 红队测试集 + 前端页面

第 3 周需要 Linux 环境（gVisor 不支持 Windows/macOS 原生）。Windows 开发机上通过 WSL2 或远程 Linux 主机进行。这是 M4 唯一的环境硬约束，应提前确认。

## 11. 实施进度（Windows 开发机）

> 更新日期：2026-08-30。当前 **1609 项通过 / 9 项跳过**（ruff + mypy strict + pytest）。
> 跳过的是需要 ClickHouse / Postgres 容器的 `-m integration` 用例（本机无 Docker）。

### 验收清单状态

| # | 验收项 | 状态 | 验证方式 |
|---|---|---|---|
| 1 | 规则求值 p99 < 5ms | ❌ **未达标** | `test_harness_benchmark.py`：随包规则集 p99 = 13~27ms。原记录的 1.03ms 量的是测试文件内自造的 2 条规则，不是线上规则集 —— 详见下方"验收项 1 的订正" |
| 2 | 求值超时 fail-closed | ✅ 通过 | `test_harness_evaluator.py`：注入语法错 → hit |
| 3 | 冲突消解确定可复现 | ✅ 通过 | `test_harness_resolve.py` 确定性测试 + `test_harness_benchmark.py` 10 次一致性 |
| 4 | `block` 不可通过重试绕过 | ✅ 通过 | `test_engine_harness.py`：harness BLOCK → BLOCKED 终态 |
| 5 | 危险命令被拦 | ✅ 规则就绪 | `harness_module/rules/tool.yaml` 内置 `rm -rf`/fork bomb/`dd`/云元数据规则 |
| 6 | 注入检测召回 | ✅ **达标** | `harness_module/injection.py`（规范化 + 词法族）经 `input.yaml` 的 `detect_injection()` 接入。语料内 29/29、误报 0/33、p99 0.11ms。分层测试 `test_harness_injection.py`（含预扫超集性质与延迟上界），端到端 `test_redteam.py` |
| 7 | 沙箱逃逸测试全部失败 | ⛔ **不适用**（Windows 单平台） | gVisor 要 runsc、Firecracker 要 KVM，Windows 上无可用路径 —— 不是"待验证"。替代防线：`win_job.py` 的资源上限 + `ExecPolicy` 白名单 + Harness `tool.yaml` 规则，见第 13 节 |
| 8 | 禁网生效（strict 档） | ⛔ **不适用**（Windows 单平台） | 进程级无法隔离网络命名空间，`network_deny` 依赖沙箱后端。唯一防线在 Harness 规则层匹配出站目标 —— 这是当前范围下最实质的残留风险 |
| 9 | 沙箱用完即毁 | ✅ 逻辑就绪 | `sandbox_module/pool.py` release 即销毁 + 异步补充；需 Linux 实跑验证 |
| 10 | 预热池降低冷启动 | ✅ 逻辑就绪 | `SandboxPool` 预热 N 个实例（`pool_size` 配置）；需 Linux 实跑验证 |
| 11 | `rules test` 可用 | ✅ 通过 | `test_harness_api.py`：POST /v1/rules/test 返回正确裁决 |
| 12 | 审计不可篡改 | ✅ 通过 | `harness_models.py` AuditLogRow + 迁移 REVOKE UPDATE/DELETE；`test_harness_models.py` 验证 |
| 13 | 新规则默认 warn | ✅ 通过 | `test_harness_loader.py`：不指定 action → warn |
| 14 | M3 的受限子进程已下线 | ⛔ **取消** | Windows 单平台下它是永久执行路径，不会下线。方向反过来了：不是等它退场，而是把它的限制真正强制住（`win_job.py`）。`fallback_to_restricted=false` 时 worker 拒绝启动而非静默降级（此前该配置项无人读取） |
| 15 | Windows 资源上限真强制 | ✅ **达标**（新增） | `win_job.py` Job Object：内存/进程数/CPU 三项各配对照组实测，`tests/test_win_job.py` 11 条。回退验证：禁用 Job Object 后正是那 4 条强制性断言失败 |

### 验收项 1 的订正：p99 < 5ms 达不到，原因是结构性的

原先这一项记的是 `p99 = 1.03ms ✅ 通过`。那个数字量的是 `test_harness_benchmark.py`
文件内自造的 3 条规则（落在 `pre_model` 的只有 2 条），不是线上随包加载的
`harness_module/rules/*.yaml`。改成量随包规则集后的实测（负载归一化，见下）：

| 卡点 | 规则数 | p50 | p99 | 相对纯 Python 基线 | 单条规则 |
|---|---|---|---|---|---|
| `pre_model` | 5 | 12.08ms | 19.36ms | 19.0x | 3.8x |
| `post_model` | 4 | 8.26ms | 13.18ms | 13.3x | 3.3x |
| `pre_tool` | 6 | 15.74ms | 27.25ms | 25.0x | 4.2x |

**为什么这是结构性的而非调优不足**：`cel-python` 是解释型求值器，每次
`evaluate()` 重走一遍 lark 解析树。实测单条规则约 2.6ms，且耗时跟表达式 AST
规模相关、与内置函数的实际工作量无关 —— `resource-iteration-warn` 是纯整数
比较、零函数调用，照样要 2.13ms。拆开看一次卡点求值：`_context_to_dict` 占
0.1%、`_normalize_loop` 0.1%、`json_to_cel` 3~6%，纯 CEL 求值 >100%（其余是
Python 循环与 `resolve` 的开销）。把 5 条规则压进 5ms 需要单条 < 1ms，celpy
做不到，优化上下文构造或归一化则毫无意义。

被否决的加速方案：

| 方案 | 否决理由 |
|---|---|
| celpy `CompiledRunner` | 能快一个量级，但它把 CEL 转写成 Python 再 `eval`。规则表达式可经 `/v1/rules` 由租户提供 —— 这等于交出任意代码执行权。第 4 节与 [04 文档](04-harness-and-sandbox.md) 第 2.2 节已就此否决 `eval`/`exec`，不能为性能反悔 |
| 减少每卡点规则数 | 这是改语义（少拦一类东西），不是优化 |
| 把一个卡点的规则合并成单个 CEL 表达式 | 丢掉逐条命中归属，审计要不了 |
| 缓存内置函数结果 | 单次卡点内不存在重复调用，无可缓存 |
| 并行求值 | GIL 下无收益，线程开销还大于 2.6ms 的任务本身 |

**实际影响**：单次卡点 10~27ms，相对 LLM 调用的 1000~5000ms 是 0.2%~2.5%。
[M6](M6-spec.md) 的"平台引入的额外延迟 < 50ms P95"仍然满足。所以真正的缺陷是
文档记了个不成立的数字，不是这点延迟 —— 指标应改成对得上现实的值，本节保留
原目标与实测差距，不悄悄改小目标当达标。

**基准为什么断言比值而不断言毫秒**：本机墙钟不可比，同一段测量在分钟级内测出过
p50 = 3.4ms 与 13.7ms（4 倍差）。所以 `test_harness_benchmark.py` 断言的是
「被测 / 同进程纯 Python 基线」的比值，两者交替采样、跟随同期机器负载，机器忙时
同等变慢、比值稳定。绝对毫秒只记录不断言，免得把环境抖动变成红灯。

顺带修掉的一个生产缺陷：`harness_module/functions.py` 的 `estimate_tokens` 曾在
函数体内惰性 `import ariadne.loop_module.context`，为一个 `len(text)/2.5` 的除法
拖进整个 loop_module 包（约 3.3s 冷导入）。这笔开销落在**首次规则求值**上，实测
首次 `evaluate()` 757ms，超过 `DEFAULT_TIMEOUT_MS = 100` 后被 fail-closed 判成
命中 —— worker 起来后的第一个请求会被无故拦下，日志只显示 `cel eval timeout`，
看不出真凶是 import。已把实体挪到无依赖的 `utils/tokens.py`（首次求值回落到
17~20ms），并由 `test_cold_start_within_timeout` 用**未预热**的 evaluator 守住。

### 已完成模块

- **`harness_module/`**：`models.py`（Rule/Decision/RuleHit）、`evaluator.py`（CEL 求值 + fail-closed）、
  `functions.py`（matches/detect_pii/count_citations/json_valid 内置函数）、`resolve.py`（冲突消解）、
  `loader.py`（YAML 加载 + `compile_rule_set`）、`actions.py`（六动作分发）、`audit.py`（AuditRecord + AuditSink + PostgresAuditSink）、
  `rules/`（input/output/resource/tool 四类内置规则）
- **`sandbox_module/`**：`base.py`（BaseSandbox + SandboxRunner + SandboxProfile + 装配期 `probe()`）、`profiles.py`（三档 ProfileSpec）、
  `gvisor.py`（GVisorSandbox 完整实现 + 不可用时抛 SandboxUnavailableError）、`firecracker.py`（FirecrackerSandbox）、
  `pool.py`（SandboxPool 预热池）、`selector.py`（**按 profile 选后端 + 降级链**）、`__init__.py`（SandboxFactory）
- **`spec_module/`**：`schema.py`（Spec Pydantic 模型）、`loader.py`（load_spec + derive_goal + derive_rules）、`validate.py`（validate_spec）
- **`runtime_module/llm/guarded.py`**：GuardedLLMAdapter（pre/post_model 求值 + BLOCK/REWRITE/ROUTE + 审计）
- **`loop_module/engine.py`**：`_precheck` 接入 harness、`_executing` 捕获 HarnessBlockError → RULES_BLOCKED
- **`api/routers/`**：`rules.py`（GET/PUT/POST test）、`specs.py`（POST/GET）、`approvals.py`（POST/GET/decide）
- **`storage/postgres/harness_models.py`**：AuditLogRow + ApprovalRow + RuleSetRow
- **`deploy/alembic/versions/c3d4e5f6a7b8_harness_tables.py`**：三张表迁移 + REVOKE
- **`config.py`**：HarnessSettings + SandboxSettings
- **`worker/loop_worker.py`**：`_build_engine` 从 rules_dir 加载 harness + 包装 GuardedLLMAdapter + 注入 audit_sink

### 待 Linux 环境完成

验收项 7/8（沙箱逃逸 + 禁网）需要 gVisor + runsc 运行时，仅 Linux 可用。代码已就绪，`gvisor.py` 实例化时检测 runsc 可用性，不可用时抛 `SandboxUnavailableError`。验收项 14（受限子进程下线）在 gVisor 验证通过后执行，当前保留为 fallback。

## 12. 沙箱装配缺口（2026-08-31 修复）

沙箱子系统在此之前是**建好但从未被调用**的死代码：模块齐备、单元测试全绿，却没有任何生产路径构造它。

具体短路点是 `loop_module/engine.py` 的一行硬编码 `inner = RestrictedRunner()`。由此连带失效的东西：

| 失效对象 | 后果 |
|---|---|
| `settings.sandbox` 五个旋钮 | 全部无人读取 —— 配了 `ARIADNE_SANDBOX_PROFILE` 也不生效 |
| `ProfileSpec.isolation` | 声明"strict 档用 firecracker"，但没有读取方，`GVisorSandbox` 恒发 `--runtime=runsc` |
| `SandboxFactory` / `SandboxPool` | 只在自身模块与测试内被引用 |
| `fallback_to_restricted` | 无人读取 —— 显式关掉降级的部署仍会静默跑在受限子进程上 |

最后一条是安全后果：`restricted_exec.py` 自己的文档写明它"无法隔离网络，也防不住内核层逃逸，仅适用于用户自己的代码在自己的机器上跑"。也就是说多租户部署以为配了硬件级隔离，实际跑在受限子进程里。

### 修法

1. `base.py` 加装配期 `probe()`。原先两个后端只在 `run()` 里检测可用性，于是"本机没有 runsc"要等 Loop 跑到第一条 COMMAND 断言才暴露 —— 那时 LLM token 已经烧掉。`probe()` 刻意**不是** `abstractmethod`：加了会让已有测试桩无法实例化。
2. 新增 `selector.py`：`profile → ProfileSpec.isolation → 后端`，降级链 firecracker→gvisor 沿用 `firecracker.py` 既有声明。profile 拼错抛 `ValueError` 而非默认到 strict —— 静默解释成某个档位等于让人以为配了隔离。
3. `LoopConfig.command_runner` 可注入，engine 未注入时仍用 `RestrictedRunner`（测试与单机开发行为不变）。
4. `loop_worker._build_command_runner()` 落地降级策略：可用→`SandboxRunner`；不可用且允许降级→记 warning 后回落；不可用且 `fallback_to_restricted=false`→**抛错拒绝启动**。

### 这次留下的测试为什么盯装配而非组件

单元测试只证明"如果调用它，它能工作"，不证明"它被调用了"。所以新测试断言的是装配关系本身：

- `TestEngineWiring::test_injected_runner_replaces_restricted_subprocess` —— 注入的执行器必须真的取代默认值
- `TestEngineWiring::test_injected_runner_still_goes_through_harness_gate` —— 换沙箱不能顺带绕掉 pre_tool 卡点
- `TestProfileDrivesBackend::test_every_profile_maps_to_a_registered_backend` —— 新增 profile 忘了注册后端会在此暴露
- `TestWorkerFallbackPolicy` —— 三条降级分支各一条

这四条在改动前会失败（已实测回退验证：恢复硬编码 + 移除 `probe()` 后正是这 4 条报错），不是事后追认的装饰。

本机（Windows，无 KVM/runsc）能验证的边界止于**选型与降级决策**；真隔离效果仍待 Linux，这个边界写在 `tests/test_sandbox_selector.py` 的 docstring 里。

## 13. Windows 单平台下的安全边界（2026-08-31 定范围）

项目确定只面向 Windows，不做其他端适配。这把 M4 的沙箱路线从"待环境"变成"不适用"，安全模型必须重写而不是等。

### 为什么沙箱在 Windows 上不是"暂时缺环境"

gVisor 的隔离来自 runsc 这个用户态内核，Firecracker 来自 KVM —— 两者都是 Linux 内核设施，Windows 上不存在等价物。所以 `SandboxRunner` 这条路在当前范围内永远走不通，`selector.py` 在 Windows 上恒定返回 `None`。

保留 `selector.py` 的理由不是"以后能用"，而是它把**沙箱不可用这件事从静默变成可见**：记 warning，或在 `fallback_to_restricted=false` 时拒绝启动。此前这个配置项无人读取，显式要求真隔离的部署也会静默跑在受限子进程上。

### 受限子进程的三个上限此前是假的

`restricted_exec._preexec` 在 win32 上直接 `return None`。后果是 `ExecPolicy` 的三个字段配了不生效：

| 字段 | Windows 上的实际效果（修复前） |
|---|---|
| `memory_bytes` | 无。跑飞的 pytest 能吃光物理内存 |
| `max_processes` | 无。fork bomb 不受限 |
| `cpu_seconds` | 无。只有 `timeout_s` 的墙钟超时 |

这与 R12 是同一类缺陷：配置项齐备、单元测试全绿，但没有强制路径。而 Windows 是唯一平台，所以它就是生产安全边界。

修法是 Job Object（`win_job.py`，ctypes 调 kernel32，不引新依赖），三项各配**放宽上限的对照组**实测——没有对照，"分配失败"可能只是机器内存不够，证明不了上限起了作用：

| 限制 | 超限用例 | 对照组 |
|---|---|---|
| `ProcessMemoryLimit` | 200MB 上限下分配 600MB → `MemoryError` | 2GB 上限下同样分配成功 |
| `ActiveProcessLimit` | 上限 1 时子进程起不了孙进程（spawned 0） | —— |
| `PerJobUserTimeLimit` | CPU 上限 3s，10s 死循环在墙钟超时（60s）**之前**被杀，退出码 `0xC0000044` | 断言 `timed_out is False`，把配额终止与墙钟超时分开 |

`KILL_ON_JOB_CLOSE` 顺带修掉 Windows 进程树终止不可靠的问题：关 job 句柄即杀光整棵树，比 `taskkill /T`（父进程已退出时可能漏掉孙进程）可靠。

退出码另做了翻译：超配额被杀时 `subprocess` 报的是 `3221225540` 这种十进制 NTSTATUS，不翻译的话 critique 会拿着这个数字去猜代码哪里写错了，而真实原因是资源超限 —— Loop 会朝错误方向改好几轮。

### 残留风险：禁网做不到，且执行层无补救

进程级隔离不了网络命名空间，`SandboxProfile.STRICT` 声明的 `network_deny` 依赖沙箱后端。唯一防线在 Harness 规则层：`tool.yaml` 匹配命令串里的出站目标（如 `169.254.169.254`）。

这是命令串匹配，绕过方式明显（变量拼接、DNS 别名、脚本内发起请求），所以**不该记成"已防护"**。Windows 单平台下这条是真实敞口，写清楚比留在"待 Linux 验证"里假装会解决要好。

对应的部署结论：当前范围只适合"用户自己的代码在自己的机器上跑"，`allow_untrusted_code` 保持默认 `False`；多租户跑不可信代码需要 Linux + 真沙箱，那是范围之外的事。

### 残留竞态：assign 在 Popen 之后

`WindowsJob.assign` 只能在 `subprocess.Popen` 返回后调用，理论上子进程能在此之前 fork 出不受限的孙进程。窗口是进程创建到 assign 之间（加载器初始化阶段，用户代码还没跑），实测 Python 子进程启动约 30ms 远大于该窗口。彻底消除要 `CREATE_SUSPENDED` + `ResumeThread`，而 `subprocess` 不暴露线程句柄 —— 代价是自己重写 `CreateProcessW` 连管道继承，不值得。这条已写在 `assign` 的 docstring 里。
