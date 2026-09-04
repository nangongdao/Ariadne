"""Loop 状态机。

纯函数转移，无副作用、无 IO —— 这样才能穷尽测试所有 (状态 × 事件) 组合。
状态机是预算安全的地基：转移错误会导致 Loop 卡死或绕过熔断。

17 个状态、8 个终态。终态分这么细是为了让失败可归因 ——
"撞预算"和"反馈无效导致原地打转"需要完全不同的处理动作。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class LoopState(StrEnum):
    # 流转态
    CREATED = "CREATED"
    VALIDATE = "VALIDATE"
    PLANNING = "PLANNING"
    PRECHECK = "PRECHECK"
    EXECUTING = "EXECUTING"
    EVALUATING = "EVALUATING"
    JUDGING = "JUDGING"
    REVISING = "REVISING"
    HUMAN_PENDING = "HUMAN_PENDING"

    # 终态
    CONVERGED = "CONVERGED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    STALLED = "STALLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class LoopEvent(StrEnum):
    START = "START"
    GOAL_VALID = "GOAL_VALID"
    GOAL_INVALID = "GOAL_INVALID"
    CONTEXT_READY = "CONTEXT_READY"
    RULES_PASSED = "RULES_PASSED"
    RULES_BLOCKED = "RULES_BLOCKED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED_BY_HUMAN = "REJECTED_BY_HUMAN"
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
    EXECUTION_DONE = "EXECUTION_DONE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EVALUATION_DONE = "EVALUATION_DONE"
    CONVERGED = "CONVERGED"
    NEEDS_REVISION = "NEEDS_REVISION"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MAX_ITERATIONS_REACHED = "MAX_ITERATIONS_REACHED"
    STALLED = "STALLED"
    REVISION_READY = "REVISION_READY"
    CANCEL = "CANCEL"
    INTERNAL_ERROR = "INTERNAL_ERROR"


TERMINAL_STATES: Final[frozenset[LoopState]] = frozenset(
    {
        LoopState.CONVERGED,
        LoopState.REJECTED,
        LoopState.BLOCKED,
        LoopState.BUDGET_EXCEEDED,
        LoopState.MAX_ITERATIONS,
        LoopState.STALLED,
        LoopState.FAILED,
        LoopState.CANCELLED,
    }
)

# 成功终态。只有 CONVERGED 算成功 —— 其余都需要人介入
SUCCESS_STATES: Final[frozenset[LoopState]] = frozenset({LoopState.CONVERGED})

# 转移表。缺失的 (状态, 事件) 组合即非法转移。
# 刻意用显式表而非 if/elif 链：表可被穷尽遍历测试，链不能。
_TRANSITIONS: Final[dict[tuple[LoopState, LoopEvent], LoopState]] = {
    (LoopState.CREATED, LoopEvent.START): LoopState.VALIDATE,
    (LoopState.CREATED, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.VALIDATE, LoopEvent.GOAL_VALID): LoopState.PLANNING,
    (LoopState.VALIDATE, LoopEvent.GOAL_INVALID): LoopState.REJECTED,
    (LoopState.VALIDATE, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.PLANNING, LoopEvent.CONTEXT_READY): LoopState.PRECHECK,
    # 轮次上限在 PLANNING 阶段检查：构造上下文前就该知道还能不能跑
    (LoopState.PLANNING, LoopEvent.MAX_ITERATIONS_REACHED): LoopState.MAX_ITERATIONS,
    (LoopState.PLANNING, LoopEvent.BUDGET_EXCEEDED): LoopState.BUDGET_EXCEEDED,
    (LoopState.PLANNING, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.PRECHECK, LoopEvent.RULES_PASSED): LoopState.EXECUTING,
    # Harness block 直接进终态，不生成 critique、不进下一轮 ——
    # 硬约束不能通过"多试几轮"绕过
    (LoopState.PRECHECK, LoopEvent.RULES_BLOCKED): LoopState.BLOCKED,
    (LoopState.PRECHECK, LoopEvent.APPROVAL_REQUIRED): LoopState.HUMAN_PENDING,
    (LoopState.PRECHECK, LoopEvent.BUDGET_EXCEEDED): LoopState.BUDGET_EXCEEDED,
    (LoopState.PRECHECK, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.EXECUTING, LoopEvent.EXECUTION_DONE): LoopState.EVALUATING,
    # 执行失败不直接进 FAILED：可能是可重试的 provider 抖动，
    # 交给 JUDGING 按 Loop 模式决定重试还是终止
    (LoopState.EXECUTING, LoopEvent.EXECUTION_FAILED): LoopState.JUDGING,
    (LoopState.EXECUTING, LoopEvent.RULES_BLOCKED): LoopState.BLOCKED,
    (LoopState.EXECUTING, LoopEvent.BUDGET_EXCEEDED): LoopState.BUDGET_EXCEEDED,
    (LoopState.EXECUTING, LoopEvent.INTERNAL_ERROR): LoopState.FAILED,
    (LoopState.EXECUTING, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.EVALUATING, LoopEvent.EVALUATION_DONE): LoopState.JUDGING,
    (LoopState.EVALUATING, LoopEvent.INTERNAL_ERROR): LoopState.FAILED,
    (LoopState.EVALUATING, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.JUDGING, LoopEvent.CONVERGED): LoopState.CONVERGED,
    (LoopState.JUDGING, LoopEvent.NEEDS_REVISION): LoopState.REVISING,
    (LoopState.JUDGING, LoopEvent.BUDGET_EXCEEDED): LoopState.BUDGET_EXCEEDED,
    (LoopState.JUDGING, LoopEvent.MAX_ITERATIONS_REACHED): LoopState.MAX_ITERATIONS,
    (LoopState.JUDGING, LoopEvent.STALLED): LoopState.STALLED,
    (LoopState.JUDGING, LoopEvent.APPROVAL_REQUIRED): LoopState.HUMAN_PENDING,
    (LoopState.JUDGING, LoopEvent.INTERNAL_ERROR): LoopState.FAILED,
    (LoopState.JUDGING, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.REVISING, LoopEvent.REVISION_READY): LoopState.PLANNING,
    (LoopState.REVISING, LoopEvent.INTERNAL_ERROR): LoopState.FAILED,
    (LoopState.REVISING, LoopEvent.CANCEL): LoopState.CANCELLED,
    (LoopState.HUMAN_PENDING, LoopEvent.APPROVED): LoopState.EXECUTING,
    (LoopState.HUMAN_PENDING, LoopEvent.REJECTED_BY_HUMAN): LoopState.REJECTED,
    # 审批超时视为拒绝而非放行 —— fail-closed
    (LoopState.HUMAN_PENDING, LoopEvent.APPROVAL_TIMEOUT): LoopState.REJECTED,
    (LoopState.HUMAN_PENDING, LoopEvent.CANCEL): LoopState.CANCELLED,
}


class InvalidTransitionError(ValueError):
    """非法状态转移。

    显式报错而非静默忽略：状态机是预算安全的地基，
    悄悄停在原状态会让 Loop 卡死且无从诊断。
    """

    def __init__(self, state: LoopState, event: LoopEvent) -> None:
        allowed = sorted(e.value for e in allowed_events(state))
        hint = "已是终态" if state in TERMINAL_STATES else f"允许: {allowed}"
        super().__init__(f"状态 {state.value} 不接受事件 {event.value}（{hint}）")
        self.state = state
        self.event = event


def next_state(current: LoopState, event: LoopEvent) -> LoopState:
    """纯函数转移。非法组合抛 InvalidTransitionError。"""
    target = _TRANSITIONS.get((current, event))
    if target is None:
        raise InvalidTransitionError(current, event)
    return target


def try_next_state(current: LoopState, event: LoopEvent) -> LoopState | None:
    """不抛异常的版本。用于"探测是否可转移"的场景。"""
    return _TRANSITIONS.get((current, event))


def allowed_events(state: LoopState) -> frozenset[LoopEvent]:
    return frozenset(
        event for (src, event) in _TRANSITIONS if src == state
    )


def is_terminal(state: LoopState) -> bool:
    return state in TERMINAL_STATES


def is_success(state: LoopState) -> bool:
    return state in SUCCESS_STATES


def reachable_states() -> frozenset[LoopState]:
    """从 CREATED 出发可达的状态集合。

    用于测试断言"没有不可达状态"—— 不可达状态说明转移表写漏了。
    """
    reached = {LoopState.CREATED}
    frontier = [LoopState.CREATED]
    while frontier:
        state = frontier.pop()
        for event in allowed_events(state):
            target = _TRANSITIONS[(state, event)]
            if target not in reached:
                reached.add(target)
                frontier.append(target)
    return frozenset(reached)


# 终态的归因说明。前端据此给出可操作建议（见 docs/07 的诊断提示）。
TERMINAL_DIAGNOSIS: Final[dict[LoopState, str]] = {
    LoopState.CONVERGED: "全部阻塞性断言通过",
    LoopState.REJECTED: "目标不可验证，或人工拒绝",
    LoopState.BLOCKED: "Harness 硬约束拦截，不建议重试",
    LoopState.BUDGET_EXCEEDED: "预算耗尽。得分上升中则提高预算，趋势平坦则先修断言设计",
    LoopState.MAX_ITERATIONS: "轮次耗尽但仍在改善，可提高 max_iterations",
    LoopState.STALLED: "反馈信号无效，原地打转。建议补充 hint 或改用 command 类断言",
    LoopState.FAILED: "内部错误，非任务本身的问题",
    LoopState.CANCELLED: "被主动取消",
}


def diagnose(state: LoopState) -> str:
    if state not in TERMINAL_STATES:
        raise ValueError(f"{state.value} 不是终态，无诊断信息")
    return TERMINAL_DIAGNOSIS[state]
