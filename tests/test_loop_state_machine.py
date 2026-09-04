"""状态机穷尽测试。

状态机是预算安全的地基 —— 转移错误会导致 Loop 卡死或绕过熔断。
因此这里遍历**全部** (状态 × 事件) 组合，而非只测正常路径。
"""

from __future__ import annotations

import pytest

from ariadne.loop_module.state_machine import (
    SUCCESS_STATES,
    TERMINAL_STATES,
    InvalidTransitionError,
    LoopEvent,
    LoopState,
    allowed_events,
    diagnose,
    is_success,
    is_terminal,
    next_state,
    reachable_states,
    try_next_state,
)

ALL_STATES = list(LoopState)
ALL_EVENTS = list(LoopEvent)


class TestStructure:
    def test_seventeen_states(self) -> None:
        assert len(ALL_STATES) == 17

    def test_eight_terminal_states(self) -> None:
        assert len(TERMINAL_STATES) == 8

    def test_only_converged_is_success(self) -> None:
        """其余终态都需要人介入 —— 混为一谈会让失败无法归因。"""
        assert {LoopState.CONVERGED} == SUCCESS_STATES

    def test_no_unreachable_states(self) -> None:
        """不可达状态说明转移表写漏了。"""
        unreachable = set(ALL_STATES) - reachable_states()
        assert not unreachable, f"不可达状态: {sorted(s.value for s in unreachable)}"

    def test_every_terminal_has_diagnosis(self) -> None:
        """终态必须有可操作的诊断说明，否则前端无法给建议。"""
        for state in TERMINAL_STATES:
            assert diagnose(state)

    def test_diagnose_rejects_non_terminal(self) -> None:
        with pytest.raises(ValueError, match="不是终态"):
            diagnose(LoopState.PLANNING)


class TestTerminality:
    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
    def test_terminal_states_accept_nothing(self, state: LoopState) -> None:
        """终态不再转移。允许离开终态会让"已完成"变成不确定状态。"""
        assert allowed_events(state) == frozenset()
        for event in ALL_EVENTS:
            assert try_next_state(state, event) is None

    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
    def test_is_terminal(self, state: LoopState) -> None:
        assert is_terminal(state)

    def test_non_terminal_states_have_events(self) -> None:
        for state in ALL_STATES:
            if state in TERMINAL_STATES:
                continue
            assert allowed_events(state), f"{state.value} 无任何可用事件，会卡死"

    def test_is_success_only_converged(self) -> None:
        assert is_success(LoopState.CONVERGED)
        assert not is_success(LoopState.MAX_ITERATIONS)
        assert not is_success(LoopState.STALLED)


class TestExhaustive:
    def test_all_combinations_are_deterministic(self) -> None:
        """同一 (状态, 事件) 必须永远得到同一结果 —— 审计的前提。"""
        for state in ALL_STATES:
            for event in ALL_EVENTS:
                first = try_next_state(state, event)
                second = try_next_state(state, event)
                assert first == second

    def test_illegal_combinations_raise(self) -> None:
        """非法组合抛异常而非静默停在原状态。"""
        illegal_count = 0
        for state in ALL_STATES:
            for event in ALL_EVENTS:
                if try_next_state(state, event) is not None:
                    continue
                illegal_count += 1
                with pytest.raises(InvalidTransitionError):
                    next_state(state, event)
        # 17×21=357 个组合，合法的是少数；确认确实测到了大量非法组合
        assert illegal_count > 250

    def test_error_message_lists_alternatives(self) -> None:
        with pytest.raises(InvalidTransitionError, match="允许"):
            next_state(LoopState.PLANNING, LoopEvent.CONVERGED)

    def test_error_message_says_terminal(self) -> None:
        with pytest.raises(InvalidTransitionError, match="已是终态"):
            next_state(LoopState.CONVERGED, LoopEvent.START)


class TestCancellation:
    @pytest.mark.parametrize(
        "state",
        [s for s in ALL_STATES if s not in TERMINAL_STATES],
    )
    def test_every_live_state_can_cancel(self, state: LoopState) -> None:
        """任何流转态都必须能取消 —— 否则用户无法中止跑飞的 Loop。"""
        assert next_state(state, LoopEvent.CANCEL) is LoopState.CANCELLED


class TestHappyPath:
    def test_single_iteration_convergence(self) -> None:
        """一轮达标的完整路径。"""
        state = LoopState.CREATED
        for event, expected in [
            (LoopEvent.START, LoopState.VALIDATE),
            (LoopEvent.GOAL_VALID, LoopState.PLANNING),
            (LoopEvent.CONTEXT_READY, LoopState.PRECHECK),
            (LoopEvent.RULES_PASSED, LoopState.EXECUTING),
            (LoopEvent.EXECUTION_DONE, LoopState.EVALUATING),
            (LoopEvent.EVALUATION_DONE, LoopState.JUDGING),
            (LoopEvent.CONVERGED, LoopState.CONVERGED),
        ]:
            state = next_state(state, event)
            assert state is expected
        assert is_terminal(state) and is_success(state)

    def test_revision_loops_back_to_planning(self) -> None:
        """不达标 → REVISING → PLANNING，形成闭环。"""
        state = next_state(LoopState.JUDGING, LoopEvent.NEEDS_REVISION)
        assert state is LoopState.REVISING
        assert next_state(state, LoopEvent.REVISION_READY) is LoopState.PLANNING

    def test_three_iteration_cycle(self) -> None:
        """三轮迭代后收敛 —— 验证环路可反复走。"""
        state = LoopState.PLANNING
        for _ in range(3):
            state = next_state(state, LoopEvent.CONTEXT_READY)
            state = next_state(state, LoopEvent.RULES_PASSED)
            state = next_state(state, LoopEvent.EXECUTION_DONE)
            state = next_state(state, LoopEvent.EVALUATION_DONE)
            assert state is LoopState.JUDGING
            state = next_state(state, LoopEvent.NEEDS_REVISION)
            state = next_state(state, LoopEvent.REVISION_READY)
            assert state is LoopState.PLANNING


class TestHardConstraints:
    def test_blocked_cannot_be_retried(self) -> None:
        """Harness block 进终态，不生成 critique、不进下一轮。

        这是安全设计的核心：硬约束不能通过"多试几轮"绕过。
        """
        blocked = next_state(LoopState.PRECHECK, LoopEvent.RULES_BLOCKED)
        assert blocked is LoopState.BLOCKED
        assert is_terminal(blocked)
        # 不存在任何从 BLOCKED 回到迭代的路径
        assert try_next_state(blocked, LoopEvent.NEEDS_REVISION) is None
        assert try_next_state(blocked, LoopEvent.REVISION_READY) is None

    def test_post_execution_block_also_terminal(self) -> None:
        """post_model 卡点拦截同样进终态。"""
        assert (
            next_state(LoopState.EXECUTING, LoopEvent.RULES_BLOCKED)
            is LoopState.BLOCKED
        )

    def test_approval_timeout_is_rejection_not_approval(self) -> None:
        """fail-closed：超时视为拒绝，绝不能放行。"""
        assert (
            next_state(LoopState.HUMAN_PENDING, LoopEvent.APPROVAL_TIMEOUT)
            is LoopState.REJECTED
        )

    def test_unverifiable_goal_rejected_before_any_execution(self) -> None:
        """把模糊指令挡在系统之外，而非跑 10 轮才发现无法判定。"""
        state = next_state(LoopState.CREATED, LoopEvent.START)
        assert next_state(state, LoopEvent.GOAL_INVALID) is LoopState.REJECTED

    def test_budget_checked_at_multiple_points(self) -> None:
        """预算在多个卡点检查 —— 单点检查会被绕过。"""
        for state in (
            LoopState.PLANNING,
            LoopState.PRECHECK,
            LoopState.EXECUTING,
            LoopState.JUDGING,
        ):
            assert (
                next_state(state, LoopEvent.BUDGET_EXCEEDED)
                is LoopState.BUDGET_EXCEEDED
            ), f"{state.value} 未检查预算"


class TestFailureAttribution:
    def test_execution_failure_goes_to_judging_not_failed(self) -> None:
        """执行失败可能是可重试的 provider 抖动，交给 JUDGING 按模式决定。

        直接进 FAILED 会让 Retry 模式失去意义。
        """
        assert (
            next_state(LoopState.EXECUTING, LoopEvent.EXECUTION_FAILED)
            is LoopState.JUDGING
        )

    def test_internal_error_is_distinct_from_task_failure(self) -> None:
        """FAILED 表示平台内部错误，与"任务没做好"要能区分。"""
        assert (
            next_state(LoopState.EXECUTING, LoopEvent.INTERNAL_ERROR)
            is LoopState.FAILED
        )

    def test_stalled_is_its_own_terminal(self) -> None:
        """STALLED 是有价值的失败：明确告知"断言给不出有效反馈信号"。"""
        stalled = next_state(LoopState.JUDGING, LoopEvent.STALLED)
        assert stalled is LoopState.STALLED
        assert "反馈信号无效" in diagnose(stalled)

    def test_all_failure_modes_are_distinguishable(self) -> None:
        """四种"没成功"必须是不同终态，否则无法归因。"""
        outcomes = {
            next_state(LoopState.JUDGING, LoopEvent.BUDGET_EXCEEDED),
            next_state(LoopState.JUDGING, LoopEvent.MAX_ITERATIONS_REACHED),
            next_state(LoopState.JUDGING, LoopEvent.STALLED),
            next_state(LoopState.PRECHECK, LoopEvent.RULES_BLOCKED),
        }
        assert len(outcomes) == 4


class TestHumanInTheLoop:
    def test_approval_resumes_execution(self) -> None:
        assert (
            next_state(LoopState.HUMAN_PENDING, LoopEvent.APPROVED)
            is LoopState.EXECUTING
        )

    def test_approval_can_be_required_before_or_after_execution(self) -> None:
        """pre_tool 卡点与收敛后确认都需要审批。"""
        assert (
            next_state(LoopState.PRECHECK, LoopEvent.APPROVAL_REQUIRED)
            is LoopState.HUMAN_PENDING
        )
        assert (
            next_state(LoopState.JUDGING, LoopEvent.APPROVAL_REQUIRED)
            is LoopState.HUMAN_PENDING
        )

    def test_human_rejection_is_terminal(self) -> None:
        rejected = next_state(
            LoopState.HUMAN_PENDING, LoopEvent.REJECTED_BY_HUMAN
        )
        assert is_terminal(rejected)
