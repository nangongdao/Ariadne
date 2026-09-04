"""Harness 规则引擎的领域模型。

Harness 是**硬边界**，与 Loop 的软目标严格区分（docs/04 第 1 节）：
- Loop 失败生成 critique 重试；Harness 失败直接 block。
- Harness 的 block 不能通过"多试几轮"绕过 —— 硬约束不能软化。
- 规则求值是无状态纯函数，只看当前上下文。

四类规则覆盖 AI 工作流的全部风险面：
  input / output / resource / tool
五个卡点：pre_model / post_model / pre_tool / post_tool / pre_persist
六种动作：allow / warn / block / rewrite / require_approval / route
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuleCategory(StrEnum):
    """规则类别。覆盖全部风险面。"""

    INPUT = "input"
    OUTPUT = "output"
    RESOURCE = "resource"
    TOOL = "tool"


class HookKind(StrEnum):
    """五个执行卡点。所有出站调用必经其一（docs/04 第 3 节）。"""

    PRE_MODEL = "pre_model"
    POST_MODEL = "post_model"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_PERSIST = "pre_persist"


class Severity(StrEnum):
    """严重度。用于冲突消解的同优先级排序。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """数值越大越严重，用于排序。"""
        return {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}[
            self
        ]


class Action(StrEnum):
    """六种裁决动作（docs/04 第 4 节）。

    block/require_approval 会让 Loop 进终态或挂起，不能被重试绕过。
    """

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    REWRITE = "rewrite"
    REQUIRE_APPROVAL = "require_approval"
    ROUTE = "route"


# 冲突消解的优先级顺序（docs/04 第 4 节）。
# block 最高 —— 安全约束优先于一切；allow 最低 —— 默认放行只在无其他命中时。
ACTION_PRIORITY: tuple[Action, ...] = (
    Action.BLOCK,
    Action.REQUIRE_APPROVAL,
    Action.ROUTE,
    Action.REWRITE,
    Action.WARN,
    Action.ALLOW,
)

# 各动作的优先级数值，数值越小优先级越高
_ACTION_PRIORITY_INDEX: dict[Action, int] = {
    action: i for i, action in enumerate(ACTION_PRIORITY)
}


@dataclass(frozen=True)
class Rule:
    """单条规则。不可变 —— 规则集加载后不应被运行时修改。

    `when` 是 CEL 表达式，求值为 True 时规则命中。
    默认 action 为 WARN 而非 BLOCK（docs/M4 5.3）：新建规则先观察命中
    情况再收紧，避免一上线就误拦生产流量。
    """

    id: str
    category: RuleCategory
    hook: HookKind
    when: str  # CEL 表达式
    action: Action = Action.WARN
    severity: Severity = Severity.MEDIUM
    message: str = ""
    # rewrite 动作的改写策略标识（由 actions 模块解释）
    rewrite_strategy: str = ""
    # route 动作的路由目标（如换模型名）
    route_target: str = ""

    @property
    def priority(self) -> int:
        """动作在冲突消解中的优先级。越小越优先。"""
        return _ACTION_PRIORITY_INDEX[self.action]


@dataclass(frozen=True)
class RuleHit:
    """规则命中。规则 + 求值结果 + 命中时的上下文快照。"""

    rule: Rule
    # CEL 表达式求值结果（通常 True，但可携带更细的信息）
    value: Any = True
    # 命中时的消息（可被规则的 message 覆盖）
    message: str = ""


@dataclass(frozen=True)
class Decision:
    """冲突消解后的最终裁决。

    审计要求：同样的规则集 + 同样的上下文必须得到同样的 Decision。
    确定性由 resolve() 的固定排序保证。
    """

    action: Action
    hits: tuple[RuleHit, ...] = ()  # 所有命中的规则（含未胜出的，用于审计）
    winning_hit: RuleHit | None = None  # 胜出的命中
    message: str = ""
    # rewrite 的载荷改写（由 actions 应用），这里只记策略
    rewrite_strategy: str = ""
    route_target: str = ""

    @property
    def blocked(self) -> bool:
        """是否拒绝执行。block 直接进 BLOCKED 终态。"""
        return self.action is Action.BLOCK

    @property
    def needs_approval(self) -> bool:
        return self.action is Action.REQUIRE_APPROVAL


@dataclass(frozen=True)
class HarnessContext:
    """规则求值的上下文。

    不可变快照 —— 规则求值是无状态纯函数，只看当前上下文，不看历史。
    不同卡点填不同字段：pre_model 填 input/loop，post_model 填 output/usage 等。
    """

    hook: HookKind
    # 各卡点可用的上下文（docs/04 第 3 节）。缺省为 None/空。
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    tool: dict[str, Any] = field(default_factory=dict)
    artifact: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    # Loop 状态（预算、轮次等），供 resource 类规则求值。
    # 键名与类型契约见 LOOP_CONTEXT_DEFAULTS —— 调用方填哪些键不是自由的。
    loop: dict[str, Any] = field(default_factory=dict)


# Loop 上下文契约。resource 类规则读的键 + 缺省值 + 类型。
#
# 为什么必须显式声明而不是「调用方爱填什么填什么」：
#
# 1. CEL 里缺键是求值错误，不是 null。`ctx.loop.budget_limit` 在键不存在时
#    抛 KeyError，且错误会污染 `&&` 两侧（短路不救场），被 fail-closed 兜成
#    命中。于是「没配上限」= 无条件拦截，与规则作者写 `budget_limit > 0`
#    前置守卫的本意完全相反。
# 2. CEL 数值类型严格：IntType 和 DoubleType 之间没有隐式提升。
#    `ctx.loop.budget_limit * 0.9` 在 budget_limit 是 int 时无重载报错，
#    `ctx.loop.iteration > 0.0` 同理。类型由调用方随手决定 = 随机踩雷。
#
# 所以键名和类型都在这里钉死，evaluator 按此归一化，规则作者按此写表达式。
# 新增键要同步更新 docs/04 的上下文表和 rules/*.yaml 的注释。
LOOP_CONTEXT_DEFAULTS: dict[str, int | float] = {
    # 轮次（整型，规则侧做整数比较）
    "iteration": 0,
    "max_iterations": 0,
    # Token 预算（整型）
    "budget_used": 0,
    "budget_limit": 0,
    # 成本（浮点，美元）
    "cost_usd": 0.0,
    "cost_limit": 0.0,
}

# pre_tool 上下文契约。理由同 LOOP_CONTEXT_DEFAULTS：缺键在 CEL 里是求值
# 错误而非 null，会被 fail-closed 兜成命中 —— 而 pre_tool 的命中动作是 block，
# 于是「调用方忘填 cmd」= 所有命令被拦，Loop 永远无法验证收敛。
#
# result 只有 post_tool 卡点填，但随包规则（tool-result-huge）在 pre_tool
# 求值时同样经 _normalize_tool 兜底 —— 缺省空串让前置守卫（size() > 阈值）
# 不触发，否则「命令执行前」的求值会被 fail-closed 兜成命中。
#
# 只声明随包规则实际读到的键（cmd / result）。不预留 args/workdir 之类 ——
# 没有规则读的键，补了也只是让契约看起来更大。
TOOL_CONTEXT_DEFAULTS: dict[str, str] = {
    "cmd": "",
    "result": "",
}


__all__ = [
    "ACTION_PRIORITY",
    "LOOP_CONTEXT_DEFAULTS",
    "TOOL_CONTEXT_DEFAULTS",
    "Action",
    "Decision",
    "HarnessContext",
    "HookKind",
    "Rule",
    "RuleCategory",
    "RuleHit",
    "Severity",
]
