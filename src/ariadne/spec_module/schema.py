"""spec.yaml 的 Pydantic 模型 —— 单一事实源。

spec.yaml 是 Loop 与 Harness 的共享配置源（docs/M4 §6）：
- goal 段派生 loop_module.Goal（含可验证性校验）
- rules 段派生 harness_module.Rule 列表
- sandbox 段配置沙箱 profile

spec 是"声明式"的：用户写想要什么，系统派生出各模块的配置。
不写 spec 时用默认值（最小安全默认值，docs/M4 §2）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ariadne.sandbox_module.base import SandboxProfile


class AssertionSpec(BaseModel):
    """断言的 spec.yaml 表示。对应 loop_module.Assertion。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: str  # command / schema / regex / metric / human
    spec: dict[str, object] = Field(default_factory=dict)
    weight: float = 1.0
    blocking: bool = True
    hint: str = ""


class BudgetSpec(BaseModel):
    """预算的 spec.yaml 表示。对应 loop_module.Budget。"""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int = 10
    max_total_tokens: int = 200_000
    max_cost_usd: float = 1.0
    max_tokens_per_iteration: int = 32_000
    max_wall_clock_seconds: int = 900


class GoalSpec(BaseModel):
    """目标的 spec.yaml 表示。对应 loop_module.Goal。"""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    mode: str = "quality"  # retry / quality / verify_execute / hitl
    assertions: list[AssertionSpec] = Field(min_length=1)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    stall_threshold: float = 2.0
    stall_patience: int = 2


class RuleSpec(BaseModel):
    """单条 Harness 规则的 spec.yaml 表示。对应 harness_module.Rule。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: str  # input / output / resource / tool
    hook: str  # pre_model / post_model / pre_tool / post_tool / pre_persist
    when: str = Field(min_length=1)
    action: str = "warn"  # allow / warn / block / rewrite / require_approval / route
    severity: str = "medium"  # low / medium / high / critical
    message: str = ""
    rewrite_strategy: str = ""
    route_target: str = ""


class SandboxSpec(BaseModel):
    """沙箱配置的 spec.yaml 表示。"""

    model_config = ConfigDict(extra="forbid")

    profile: SandboxProfile = SandboxProfile.STRICT
    allow_untrusted_code: bool = False
    image: str = "ariadne/runtime-python:3.11"


class Spec(BaseModel):
    """spec.yaml 的完整模型。单一事实源。"""

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    goal: GoalSpec
    rules: list[RuleSpec] = Field(default_factory=list)
    sandbox: SandboxSpec = Field(default_factory=SandboxSpec)


__all__ = [
    "AssertionSpec",
    "BudgetSpec",
    "GoalSpec",
    "RuleSpec",
    "SandboxSpec",
    "Spec",
]
