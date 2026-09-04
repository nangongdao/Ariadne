"""目标、断言、预算。

设计的把关点：**创建时就拒绝不可验证的目标**，而非跑 10 轮烧完预算才发现
目标本身没法判定。"把代码优化一下"不是目标，"所有 pytest 通过且 ruff
无 error"才是。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

MAX_ASSERTIONS = 32
MIN_ITERATIONS = 1
MAX_ITERATIONS_CAP = 50


class AssertionKind(StrEnum):
    """断言类型。信号强度递减顺序 —— 决定 Loop 的收敛效率。

    COMMAND / SCHEMA 是二值无歧义的（★★★★★），
    METRIC 依赖评估器且可能有噪声（★★★）。
    代码生成场景之所以是标杆场景，就是因为能用 COMMAND。
    """

    COMMAND = "command"
    SCHEMA = "schema"
    REGEX = "regex"
    METRIC = "metric"
    HUMAN = "human"


# 各类型必需的 spec 字段。缺字段在创建时报错，不等运行时。
REQUIRED_SPEC_FIELDS: dict[AssertionKind, tuple[str, ...]] = {
    AssertionKind.COMMAND: ("cmd",),
    AssertionKind.SCHEMA: ("schema",),
    AssertionKind.REGEX: ("pattern",),
    AssertionKind.METRIC: ("name", "op", "value"),
    AssertionKind.HUMAN: (),
}

# 信号强度。用于在校验时警告"全是弱信号断言"
_SIGNAL_STRENGTH: dict[AssertionKind, int] = {
    AssertionKind.COMMAND: 5,
    AssertionKind.SCHEMA: 5,
    AssertionKind.HUMAN: 5,
    AssertionKind.REGEX: 4,
    AssertionKind.METRIC: 3,
}


@dataclass(frozen=True)
class Assertion:
    """单条断言。

    blocking=False 表示"希望满足但不阻塞收敛"——用于 κ 不达标的
    Judge 指标（见 docs/05 的 κ 门禁）。
    """

    id: str
    kind: AssertionKind
    spec: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    blocking: bool = True
    # 失败时给模型的定向提示。人工预设的提示效果通常远好于模型自己总结，
    # 因此 Critique Synthesizer 优先用它
    hint: str = ""

    @property
    def signal_strength(self) -> int:
        return _SIGNAL_STRENGTH[self.kind]

    def describe(self) -> str:
        """给模型看的人话描述。不暴露内部 spec 结构。"""
        match self.kind:
            case AssertionKind.COMMAND:
                return f"命令 `{self.spec.get('cmd', '')}` 必须成功执行"
            case AssertionKind.SCHEMA:
                return "输出必须是符合给定 JSON Schema 的合法 JSON"
            case AssertionKind.REGEX:
                must = self.spec.get("must_match", True)
                verb = "必须匹配" if must else "必须不出现"
                return f"输出{verb}模式 /{self.spec.get('pattern', '')}/"
            case AssertionKind.METRIC:
                return (
                    f"指标 {self.spec.get('name')} 必须 "
                    f"{self.spec.get('op')} {self.spec.get('value')}"
                )
            case AssertionKind.HUMAN:
                return "需要人工审批"


@dataclass(frozen=True)
class Budget:
    """预算。三层熔断的配置来源（见 budget.py）。"""

    max_iterations: int = 10
    max_total_tokens: int = 200_000
    max_cost_usd: float = 1.0
    max_tokens_per_iteration: int = 32_000
    max_wall_clock_seconds: int = 900


LoopMode = Literal["retry", "quality", "verify_execute", "hitl"]


@dataclass(frozen=True)
class Goal:
    """目标 = 断言集合 + 预算 + 策略。不是一段自然语言。"""

    task: str
    assertions: tuple[Assertion, ...]
    budget: Budget = field(default_factory=Budget)
    mode: LoopMode = "quality"
    # 得分提升低于此值算无进展
    stall_threshold: float = 2.0
    # 连续 N 轮无进展则判 STALLED
    stall_patience: int = 2
    # 工作目录的种子文件：(相对路径, 内容)。COMMAND 断言的验证对象从这里来。
    #
    # 用 tuple 而非 dict 是因为 Goal 是 frozen dataclass —— dict 不可哈希，
    # 且 tuple 让序列化往返（loop_runs 仓储）保持确定顺序。
    #
    # 没有种子文件时 COMMAND 断言的强度会被削弱：模型可以自己生成一份
    # 恒通过的测试来"达标"。要验证真实修复能力，测试文件必须由用户提供。
    workspace: tuple[tuple[str, str], ...] = ()

    @property
    def blocking_assertions(self) -> tuple[Assertion, ...]:
        return tuple(a for a in self.assertions if a.blocking)

    @property
    def blocking_ids(self) -> frozenset[str]:
        return frozenset(a.id for a in self.blocking_assertions)

    def assertion_by_id(self, assertion_id: str) -> Assertion | None:
        return next((a for a in self.assertions if a.id == assertion_id), None)

    def spec_summary(self) -> str:
        """任务规格。上下文的固定段，每轮不变以便命中 provider 缓存。"""
        lines = [f"任务：{self.task}", "", "必须满足的条件："]
        for assertion in self.blocking_assertions:
            lines.append(f"  - {assertion.describe()}")

        optional = [a for a in self.assertions if not a.blocking]
        if optional:
            lines += ["", "期望满足（不阻塞完成）："]
            lines += [f"  - {a.describe()}" for a in optional]
        return "\n".join(lines)
