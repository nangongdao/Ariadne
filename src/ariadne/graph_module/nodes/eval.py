"""Eval 节点 —— 用 Verifier 求值断言。

参数模型：EvalNodeParams（assertions + input）。
执行器：EvalNodeExecutor，用 VerifierFactory 构造验证器，
对 ctx.inputs["input"] 求值断言，输出 {passed: bool, verdict: str}。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import NodeExecutionContext, NodeExecutor
from ariadne.loop_module.goal import Assertion, AssertionKind
from ariadne.loop_module.verifier import VerifierFactory
from ariadne.loop_module.verifier.base import VerificationContext, judge
from ariadne.spec_module.loader import derive_goal
from ariadne.spec_module.schema import Spec


@dataclass(frozen=True)
@register_node("eval")
class EvalNodeParams:
    """Eval 节点参数。

    assertions: list[AssertionSpec dict] —— 从 spec.yaml 格式构造 Assertion。
    input: 被评估内容的端口引用（实际值从 ctx.inputs["input"] 取）。
    """

    assertions: list[dict[str, Any]]
    input: str = "input"


class EvalNodeExecutor(NodeExecutor):
    """Eval 节点执行器。

    从 params["assertions"] 构造 Assertion 列表，用 VerifierFactory
    对 ctx.inputs["input"] 求值，输出 {passed: bool, verdict: str}。
    """

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        params = ctx.node.params
        assertion_data = params.get("assertions", [])
        if not isinstance(assertion_data, list) or not assertion_data:
            return {"passed": False, "verdict": "无断言"}

        # 构造 Assertion 列表
        assertions = []
        for a_data in assertion_data:
            if not isinstance(a_data, dict):
                continue
            kind = AssertionKind(a_data.get("kind", ""))
            spec_dict = a_data.get("spec", {})
            assertions.append(
                Assertion(
                    id=a_data.get("id", ""),
                    kind=kind,
                    spec=spec_dict if isinstance(spec_dict, dict) else {},
                    weight=float(a_data.get("weight", 1.0)),
                    blocking=bool(a_data.get("blocking", True)),
                    hint=a_data.get("hint", ""),
                )
            )

        if not assertions:
            return {"passed": False, "verdict": "无有效断言"}

        # 取被评估的输入
        eval_input = str(ctx.inputs.get("input", ""))

        # 构造验证上下文
        vctx = VerificationContext(output=eval_input)

        # 对每条断言求值
        outcomes = []
        for assertion in assertions:
            verifier = VerifierFactory(assertion.kind)
            outcome = verifier.verify(assertion, vctx)
            outcomes.append(outcome)

        # 构造裁决（用 derive_goal 的 Goal 来计算 converged）
        from ariadne.spec_module.schema import GoalSpec

        # 从 assertions 构造最小 Goal
        goal_spec = GoalSpec(
            task="eval",
            assertions=[
                {
                    "id": a.id,
                    "kind": a.kind.value,
                    "spec": a.spec,
                    "weight": a.weight,
                    "blocking": a.blocking,
                    "hint": a.hint,
                }
                for a in assertions
            ],
        )
        spec = Spec(goal=goal_spec)
        goal = derive_goal(spec)

        verdict = judge(tuple(outcomes), goal)

        passed = verdict.converged
        verdict_str = "passed" if passed else "failed"
        if verdict.failed:
            verdict_str = f"failed: {', '.join(o.assertion_id for o in verdict.failed)}"

        return {"passed": passed, "verdict": verdict_str}


__all__ = [
    "EvalNodeExecutor",
    "EvalNodeParams",
]
