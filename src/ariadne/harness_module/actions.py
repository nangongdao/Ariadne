"""Harness 六动作执行 —— 把 Decision 翻译为对载荷的操作。

纯函数分发，无 IO、无副作用。调用方（GuardedLLMAdapter / engine）拿到
ActionResult 后自行决定如何映射到状态机事件。

关键语义（docs/04 第 4 节）：
- BLOCK：硬终止，进 BLOCKED 终态，不生成 critique，不可被重试绕过。
- REQUIRE_APPROVAL：挂起进 HUMAN_PENDING，等人工审批。
- ROUTE：换目标（如换模型），继续执行。
- REWRITE：改写载荷（如脱敏），继续执行。
- WARN：透传但记录命中（计入 warn-rate SLI）。
- ALLOW：透传。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ariadne.harness_module.models import Action, Decision, RuleHit


class RewriteError(ValueError):
    """REWRITE 动作执行失败（改写函数抛错或未提供）。"""


class RewriteFn(Protocol):
    """改写函数的协议。

    strategy 是规则里声明的 rewrite_strategy（如 "redact_pii"），
    payload 是待改写的内容（prompt / output / tool args）。
    返回改写后的 payload。
    """

    def __call__(self, strategy: str, payload: Any) -> Any: ...


@dataclass(frozen=True)
class ActionResult:
    """动作执行结果。不可变，供调用方判断下一步。

    - blocked=True 时调用方应进 BLOCKED 终态，不继续。
    - needs_approval=True 时调用方应进 HUMAN_PENDING。
    - route_target 非空时调用方应换目标。
    - payload 在 REWRITE 时是改写后的值，其余动作是原值透传。
    """

    action: Action
    handled: bool = True  # 调用方是否需要特殊处理（block/approval/route/rewrite 为 True）
    payload: Any = None  # 处理后的载荷
    blocked: bool = False
    needs_approval: bool = False
    route_target: str = ""
    rewrite_applied: bool = False
    winning_hit: RuleHit | None = None
    hits: tuple[RuleHit, ...] = ()
    message: str = ""

    @property
    def should_continue(self) -> bool:
        """载荷是否可以继续往下传（未被拦截/挂起）。"""
        return not self.blocked and not self.needs_approval


def apply_decision(
    decision: Decision,
    payload: Any,
    *,
    rewrite_fn: RewriteFn | None = None,
) -> ActionResult:
    """把冲突消解后的 Decision 翻译为对 payload 的操作。

    纯函数：不调 IO，不改外部状态。REWRITE 时调 rewrite_fn（也是纯函数）。

    无命中（ALLOW）时 fast-return，payload 原样透传。
    """
    if decision.action is Action.ALLOW and not decision.hits:
        return ActionResult(
            action=Action.ALLOW,
            handled=False,
            payload=payload,
            hits=decision.hits,
        )

    if decision.action is Action.BLOCK:
        return ActionResult(
            action=Action.BLOCK,
            blocked=True,
            winning_hit=decision.winning_hit,
            hits=decision.hits,
            message=decision.message,
        )

    if decision.action is Action.REQUIRE_APPROVAL:
        return ActionResult(
            action=Action.REQUIRE_APPROVAL,
            needs_approval=True,
            winning_hit=decision.winning_hit,
            hits=decision.hits,
            message=decision.message,
        )

    if decision.action is Action.ROUTE:
        return ActionResult(
            action=Action.ROUTE,
            route_target=decision.route_target,
            winning_hit=decision.winning_hit,
            hits=decision.hits,
            message=decision.message,
        )

    if decision.action is Action.REWRITE:
        strategy = decision.rewrite_strategy
        if rewrite_fn is None:
            raise RewriteError(
                f"REWRITE action requires a rewrite_fn (strategy={strategy!r}), none provided"
            )
        try:
            new_payload = rewrite_fn(strategy, payload)
        except Exception as exc:
            raise RewriteError(f"rewrite strategy {strategy!r} failed: {exc}") from exc
        return ActionResult(
            action=Action.REWRITE,
            payload=new_payload,
            rewrite_applied=True,
            winning_hit=decision.winning_hit,
            hits=decision.hits,
            message=decision.message,
        )

    # WARN：透传 payload，但记录命中
    if decision.action is Action.WARN:
        return ActionResult(
            action=Action.WARN,
            handled=False,
            payload=payload,
            winning_hit=decision.winning_hit,
            hits=decision.hits,
            message=decision.message,
        )

    # ALLOW with hits（理论上 resolve 不会产生这个，但防御性处理）
    return ActionResult(
        action=Action.ALLOW,
        handled=False,
        payload=payload,
        hits=decision.hits,
    )


__all__ = [
    "ActionResult",
    "RewriteError",
    "RewriteFn",
    "apply_decision",
]
