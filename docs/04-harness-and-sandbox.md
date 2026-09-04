# 04 Harness 约束引擎与沙箱

## 1. 定位

Harness 是**硬边界**，与 Loop 的软目标严格区分：

| | Loop Engine | Harness Engine |
|---|---|---|
| 语义 | 软目标，迭代逼近 | 硬约束，越界即拦截 |
| 失败处理 | 生成修正指令，重试 | 直接 block / rewrite / 要求审批 |
| 能否被"多试几轮"绕过 | —— | **绝对不能** |
| 求值性质 | 有状态、依赖历史 | 无状态纯函数、只看当前上下文 |
| 超时策略 | 计入预算 | **fail-closed**（求值超时视为拒绝） |

fail-closed 是安全设计的底线：规则引擎自己挂了，不能变成"放行一切"。

## 2. 规则模型

四大类规则，覆盖 AI 工作流的全部风险面：

| 类别 | 规则示例 | 检查时机 |
|---|---|---|
| **输入约束** input | PII 检测、prompt 注入检测、输入长度上限、敏感话题拦截 | `pre_model`、`pre_tool` |
| **输出约束** output | 引用必带、格式校验（JSON/Markdown）、敏感信息泄漏、事实一致性下限 | `post_model`、`pre_persist` |
| **资源约束** resource | Token 上限、成本上限、耗时上限、并发上限、单次调用最大重试 | 全部卡点 |
| **工具约束** tool | 工具白名单、网络访问域名白名单、文件路径限制、危险命令黑名单 | `pre_tool` |

### 2.1 规则声明（YAML）

```yaml
# rules/default.yaml
version: 1
rules:
  - id: block-pii-in-prompt
    category: input
    hook: pre_model
    severity: critical
    when: 'detect_pii(input.text) != []'
    action: rewrite            # 脱敏后继续，而非直接拒绝
    rewrite:
      strategy: mask_pii
    message: "输入包含 PII，已脱敏后继续"

  - id: require-citations
    category: output
    hook: post_model
    severity: high
    when: 'output.token_count > 200 && count_citations(output.text) < 3'
    action: block
    message: "长文输出必须包含至少 3 个引用来源"

  - id: token-circuit-breaker
    category: resource
    hook: pre_model
    severity: critical
    when: 'ctx.loop.total_tokens + estimate_tokens(input) > ctx.loop.budget.max_total_tokens'
    action: block
    message: "触发 Token 熔断"

  - id: no-destructive-shell
    category: tool
    hook: pre_tool
    severity: critical
    when: 'tool.name == "shell" && matches(tool.args.cmd, "(rm\s+-rf|mkfs|dd\s+if=|:\(\)\{)")'
    action: block
    message: "检测到危险命令"

  - id: prod-deploy-approval
    category: tool
    hook: pre_tool
    severity: critical
    when: 'tool.name == "deploy" && tool.args.env == "production"'
    action: require_approval
    message: "生产部署需人工审批"
```

### 2.2 表达式求值：为什么用 CEL 而不是自研 DSL

`when` 字段用 **CEL（Common Expression Language）**：

- 沙箱安全 —— 无副作用、无循环、无文件/网络访问，天然不能被规则作者滥用。
- **求值时间有上界**，可满足 fail-closed 的超时要求（自研 DSL 或 `eval()` 都做不到这点）。
- 有成熟实现（`cel-python`），语法接近 Python，规则作者上手成本低。

明确否决 `eval()` / `exec()`：规则文件可能来自租户上传，用 Python 原生求值等于给了任意代码执行能力。

内置函数库（宿主侧实现，规则侧只能调用不能定义）：

| 函数 | 用途 |
|---|---|
| `detect_pii(text) -> list[str]` | 返回检出的 PII 类型 |
| `detect_injection(text) -> float` | prompt 注入置信度 0-1 |
| `count_citations(text) -> int` | 引用计数（支持 Markdown 链接、脚注、`[n]` 格式） |
| `estimate_tokens(payload) -> int` | 预估 Token（用于预扣） |
| `matches(text, pattern) -> bool` | 正则匹配（**带回溯上限，防 ReDoS**） |
| `json_valid(text, schema) -> bool` | JSON Schema 校验 |
| `sensitive_score(text) -> float` | 敏感内容评分 |

## 3. 五个执行卡点

| 卡点 | 时机 | 可用上下文 | 典型规则 |
|---|---|---|---|
| `pre_model` | LLM 调用前 | input、ctx（loop 状态、预算） | PII、注入检测、Token 熔断 |
| `post_model` | LLM 返回后 | output、usage、cost | 引用必带、格式、敏感泄漏 |
| `pre_tool` | 工具调用前 | tool.name、tool.args | 工具白名单、危险命令、审批 |
| `post_tool` | 工具返回后 | tool.result | 结果大小上限、敏感数据过滤 |
| `pre_persist` | 产物落库前 | artifact | 最终格式校验、脱敏落盘 |

**所有出站调用必经卡点**是设计目标，当前实现覆盖度：

| 卡点 | 实现状态 | 调用点 |
|---|---|---|
| `pre_model` | 已接线 | `LoopEngine._precheck` + `GuardedLLMAdapter.complete` |
| `post_model` | 已接线 | `GuardedLLMAdapter.complete` |
| `pre_tool` | 已接线 | `GuardedCommandRunner.run`（`LoopEngine._build_command_runner` 装配） |
| `post_tool` | **已接线** | `GuardedCommandRunner._post_tool_check`（`inner.run` 之后） |
| `pre_persist` | **已接线** | `GuardedArtifactWriter.write`（`LoopEngine._build_artifact_writer` 装配，`_persist_artifacts` 调用） |

BLOCK 语义随卡点而变（刻意设计，见 `guarded.py` 模块 docstring）：
- `pre_model` / `post_model` / `pre_tool` BLOCK → Loop 进 `BLOCKED` / `RULES_BLOCKED` 终态（硬约束，不可重试绕过）
- `post_tool` BLOCK → 命令已执行，判断言**失败**（输出内容违规，critique 驱动模型修输出而非修配置）
- `pre_persist` BLOCK → 产出物不落盘，本轮 `EXECUTION_FAILED`（内容违规时 Loop 无法以违规输出收敛）

LLM 侧的不可绕过性成立：`worker.loop_worker._build_engine` 用 `GuardedLLMAdapter`
包装 provider adapter，引擎只持有包装后的实例。

工具侧接的不是 Tool Registry（没有这东西），而是 Loop 里真正执行命令串的那条
路径：`CommandVerifier` → `CommandRunner`。`LoopConfig.tool_executor` 与
`graph_module` 的 `ToolNodeExecutor` 都从未在生产接线，照它们接会再造一层
死代码。`GuardedCommandRunner` 包装 `CommandRunner` Protocol，`harness=None`
时退回裸 `RestrictedRunner`，行为与接线前一致。

这一层补的**不是**"从无到有的防护"，说清边界以免误判风险面：

- 第一层 `ExecPolicy`（M3，`restricted_exec.py`）：argv[0] 白名单 + `shlex`
  拆分。`rm` / `sudo` / `dd` / `su` / `chmod` 本就不在白名单里；不走 shell，
  所以 `pytest; rm -rf /` 的分号只是普通参数。
- 第二层 `pre_tool` 规则（tool.yaml，7 条）：补的是前者拿不到的三件事 ——
  ①**全命令串匹配**。`ExecPolicy` 只看 argv[0] 的 basename，于是白名单内的
  命令带危险载荷时能整条过掉：`node --eval "require('child_process')…"` 是
  任意代码执行（`_check_python_args` 限住了 python 的 `-c`，但 node 侧没有
  等价限制），`npx foo --url http://169.254.169.254/…` 是拿云凭证 —— 只有
  `tool-interpreter-eval` / `tool-metadata-endpoint` 拦得住；②租户可配置
  （`ExecPolicy` 是源码常量，规则来自 spec.yaml，可按租户收紧）；③审计
  （白名单拒绝只产出一条 errored 断言，规则命中会写 `AuditRecord`，含规则
  id、severity、winning hit）。

两层的白名单**必须保持一致，第二层不得更窄**。曾经第二层只列
`pytest|ruff|mypy|node|tsc|npm test|npx vitest`，比第一层窄，于是
`go test ./...` / `cargo test` / `jest --ci` / `eslint src/` / `python -m pytest`
被第二层拦掉 —— 而第一层是放行的。这不是"偏严"，是**可用性事故**：用这些
工具链的项目每条 COMMAND 断言都返回 errored，Loop 永远判不出收敛。
`tests/test_redteam.py::TestWhitelistParity` 按集合遍历
`DEFAULT_ALLOWED_COMMANDS` 钉住这个不变式。

动作语义在此卡点的退化，两处都是刻意的：`require_approval` 等同 `block`
（命令执行在 Verifier 的同步路径上，没有挂起等审批的地方 —— 挂起点在 Loop
状态机的 `APPROVAL_REQUIRED`，不是同一个生命周期）；`rewrite` / `route`
放行并告警（改写命令串等于静默执行用户没要求的命令，`route` 更无处可路由）。

`block` 抛 `CommandNotAllowedError` 而非新异常类型：`CommandVerifier` 已捕获
它并映射为 `errored=True`，消息进证据。语义也对得上 —— 被规则拦下的命令属于
"断言配置得改"，不是"被测输出不合格"；判失败会让 critique 给出"改代码"的
无效指令。

### 3.1 `ctx.loop` 上下文契约

resource 类规则读的键在 `harness_module/models.py:LOOP_CONTEXT_DEFAULTS` 声明，
`HarnessEvaluator._context_to_dict` 按此补全缺键并对齐类型。

| 键 | 类型 | 含义 | 缺省 |
|---|---|---|---|
| `iteration` | int | 当前轮次 | 0 |
| `max_iterations` | int | 轮次上限 | 0 |
| `budget_used` | int | 已用 Token 累计 | 0 |
| `budget_limit` | int | Token 上限 | 0 |
| `cost_usd` | float | 已花费美元 | 0.0 |
| `cost_limit` | float | 成本上限（美元） | 0.0 |

两条硬性约束，违反任一条都会让规则**无条件命中**（fail-closed 把求值错误当成命中）：

1. **缺键即错误**。CEL 里 `ctx.loop.budget_limit` 在键不存在时抛 `KeyError`，
   且错误会污染 `&&` 两侧 —— 短路求值不救场。所以 `budget_limit > 0 && ...`
   这种"没配上限就不触发"的前置守卫在缺键时完全失效，效果与本意相反。
   归一化补 0 让守卫真正生效。
2. **数值类型不隐式提升**。`IntType` 与 `DoubleType` 之间无重载：
   `ctx.loop.budget_limit * 0.9`（int × double）、`ctx.loop.cost_limit > 0`
   （double vs int 字面量）都会报 "found no matching overload"。
   规则侧要么用 `double()` 显式转换，要么保证字面量与契约类型一致。

### 3.2 `tool` 上下文契约

`harness_module/models.py:TOOL_CONTEXT_DEFAULTS` 声明，`evaluator._normalize_tool`
按它归一化。

| 键 | 类型 | 含义 | 缺省 |
|---|---|---|---|
| `cmd` | str | 完整命令串 | `""` |

只声明随包规则实际读到的键。`args` / `workdir` 之类没有规则读，补了只是让契约
看起来更大。

这里的缺键代价比 `ctx.loop` 更直接：`pre_tool` 的命中动作是 `block`，所以
"调用方忘填 `cmd`" = 求值错误 → fail-closed 命中 → 所有命令被拦 → 断言全部
`errored` → Loop 永远无法验证收敛。规则若读契约外的键，同样会拦下一切 ——
`tests/test_guarded_command.py::TestMissingContextKey` 把这条钉成已知行为，
并让随包规则集跑一条正常命令作回归闸。

## 4. 六种裁决动作

| 动作 | 行为 | 对 Loop 的影响 |
|---|---|---|
| `allow` | 放行 | 无 |
| `warn` | 放行但记录告警 | 计入 SLI 的告警率 |
| `block` | 拒绝执行，抛 `HarnessBlocked` | Loop 转 `BLOCKED` 终态（不可通过重试绕过） |
| `rewrite` | 改写载荷后继续（脱敏 / 截断 / 补全） | 记录改写前后 diff |
| `require_approval` | 挂起等人工 | Loop 转 `HUMAN_PENDING` |
| `route` | 改路由（换模型 / 换工具） | 记录路由决策原因 |

多规则命中时的**冲突消解顺序**：`block` > `require_approval` > `route` > `rewrite` > `warn` > `allow`。同优先级按 `severity` 再按规则 id 字典序，保证求值结果**确定可复现** —— 这是审计的前提。

## 5. Spec 驱动

单一 `spec.yaml` 作为项目的事实源，Loop 与 Harness 都从它派生配置，避免两处定义漂移：

```yaml
# spec.yaml
project: tech-blog-generator
objective: 生成技术博客，质量得分 > 85，预算 < $0.1

goal:
  mode: quality
  budget:
    max_iterations: 8
    max_cost_usd: 0.1
    max_total_tokens: 120000
  assertions:
    - id: quality-gate
      kind: metric
      spec: { name: composite_quality, op: ">=", value: 85 }
      hint: 优先提升事实性与引用完整度
    - id: markdown-format
      kind: regex
      spec: { pattern: '^#\s.+', must_match: true }
      hint: 必须以一级标题开头
    - id: min-citations
      kind: metric
      spec: { name: citation_count, op: ">=", value: 3 }

harness:
  rules_file: rules/default.yaml
  overrides:
    require-citations: { severity: critical }

sandbox:
  profile: strict        # strict | standard | trusted
```

## 6. 沙箱规格

三档 profile，按信任等级选择：

| 维度 | `strict`（默认） | `standard` | `trusted` |
|---|---|---|---|
| 隔离技术 | Firecracker microVM | gVisor | gVisor |
| 网络 | 完全禁止 | 域名白名单（含 provider API） | 白名单 + 内网可达 |
| 根文件系统 | 只读 | 只读 | 只读 |
| 可写路径 | `/tmp`（tmpfs, 512MB） | `/tmp` + 工作区 | `/tmp` + 工作区 + 缓存 |
| 宿主挂载 | 无 | 无 | 无（仅通过 artifact 注入） |
| CPU | 1 core, 30s | 2 core, 120s | 4 core, 600s |
| 内存 | 512 MB | 2 GB | 8 GB |
| 进程数上限 | 32 | 128 | 512 |
| syscall 过滤 | seccomp 白名单 | seccomp 白名单 | seccomp 默认档 |
| 用户 | 非 root, 随机 uid | 非 root | 非 root |

强制项（三档都不可关闭）：

- **禁止宿主目录挂载**。代码与依赖通过预构建镜像 + artifact 注入进入沙箱。
- **禁止提权**：`no-new-privileges`、drop 全部 capabilities、禁用 `ptrace`。
- **禁止访问云元数据端点**（`169.254.169.254` 等），这是容器逃逸拿凭证的常见路径。
- **输出大小上限**：stdout/stderr 各 1MB，超出截断并标记；防止日志炸内存。
- **执行完立即销毁实例**，不复用（避免跨租户/跨轮次状态残留）。

### 6.1 运行时镜像

预构建镜像避免每轮 `pip install` 的时间与网络依赖：

| 镜像 | 内容 |
|---|---|
| `ariadne/runtime-python:3.11` | pytest、ruff、mypy、numpy、pandas |
| `ariadne/runtime-node:22` | vitest、tsc、eslint、prettier |
| `ariadne/runtime-shell` | 基础 coreutils + jq + 常用 CLI |

依赖需求超出镜像范围时，走"依赖预热"流程：在受控网络下预装并生成新镜像层，缓存复用。**不允许在 strict 档沙箱内联网装包。**

### 6.2 沙箱池

冷启动是 Loop 的延迟大头（Verify-Execute 模式每轮都要跑一次）。用预热池缓解：

- 维持 N 个空闲实例（`N = 并发上限 × 1.5`）。
- 取用时从池中弹出，用完销毁，池异步补齐。
- gVisor 冷启动 ~100-200ms，Firecracker ~150-300ms；预热后取用近似 0。

## 7. 审计

每次规则求值都产生一条不可变审计记录：规则 id、卡点、输入摘要哈希、裁决、耗时、命中的表达式。审计数据独立表存储，**只允许 append**，用于事后追责与合规证明（见 [10 安全与多租户](10-security.md#6-审计日志)）。

