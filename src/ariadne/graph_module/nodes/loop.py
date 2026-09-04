"""Loop 节点 —— Goal 驱动的受控循环子图。

参数模型：LoopNodeParams（goal + 可选 rules/sandbox）。
执行器：LoopNodeExecutor，注入 LLMClient，
构造 LoopConfig + LoopEngine，await engine.run()，
输出 {output, iterations, converged}。

外部看是单入单出节点；内部是 Goal 驱动的迭代。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import NodeExecutionContext, NodeExecutor
from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
from ariadne.loop_module.engine import LoopConfig, LoopEngine
from ariadne.spec_module.loader import derive_goal
from ariadne.spec_module.schema import Spec

if TYPE_CHECKING:
    from ariadne.loop_module.engine import LLMClient, LoopOutcome


@dataclass(frozen=True)
@register_node("loop")
class LoopNodeParams:
    """Loop 节点参数。

    goal: GoalSpec dict（从 spec.yaml 转换或直接构造）。
    """

    goal: dict[str, Any]


class LoopNodeExecutor(NodeExecutor):
    """Loop 节点执行器。

    从 node.params["goal"] 构造 Goal，运行 LoopEngine，
    输出 {output: str, iterations: int, converged: bool}。
    """

    def __init__(self, llm: LLMClient, *, default_model: str | None = None) -> None:
        self._llm = llm
        self._default_model = default_model

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        from ariadne.spec_module.schema import GoalSpec

        goal_data = ctx.node.params.get("goal")
        if goal_data is None:
            raise ValueError("Loop 节点缺少 goal 参数")

        goal_spec = GoalSpec.model_validate(goal_data)
        spec = Spec(goal=goal_spec)
        goal = derive_goal(spec)

        loop_id = f"graph_loop_{ctx.node.id}"
        budget_guard = BudgetGuard(
            loop_id=loop_id,
            budget=goal.budget,
            counter=InMemoryCounter(),
        )
        checkpoint_store = InMemoryCheckpointStore()

        config = LoopConfig(
            goal=goal,
            loop_id=loop_id,
            project_id=UUID("00000000-0000-0000-0000-000000000000"),
            budget_guard=budget_guard,
            llm=self._llm,
            checkpoint_store=checkpoint_store,
            model=self._default_model,
        )
        engine = LoopEngine(config)
        outcome = await engine.run()

        return {
            "output": _extract_output(outcome),
            "iterations": outcome.iterations,
            "converged": outcome.converged,
        }


def _extract_output(outcome: LoopOutcome) -> str:
    """从 LoopOutcome 提取最终输出文本。"""
    if outcome.verdict is not None and outcome.verdict.outcomes:
        for o in outcome.verdict.outcomes:
            if hasattr(o, "evidence") and o.evidence:
                return str(o.evidence)
    return ""


__all__ = [
    "LoopNodeExecutor",
    "LoopNodeParams",
]
