"""Loop Engine —— 编排主循环。

把状态机、预算、Verifier、Critique、上下文、指纹串起来的核心。
四条铁律（docs/03）在此落地：

1. 目标必须可验证 —— VALIDATE 阶段调 validate_goal，不通过直接 REJECTED
2. 不信任模型自评 —— 收敛只看 Verifier 裁决，claimed_done 仅记录
3. 状态外置 —— 每轮 JUDGING 后落检查点，崩溃从检查点续跑
4. 预算硬熔断 —— reserve 失败即终止，不是告警

所有外部依赖（LLM、存储、时钟、事件推送）抽象成 Protocol，单元测试用
纯桩即可跑通收敛/假完成/预算熔断/振荡/崩溃恢复全部路径——engine 的正确性
不该依赖真实 provider 或容器。生产装配见 worker/loop_worker.py。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from ariadne.loop_module.artifact import (
    ArtifactWriter,
    FencedCodeWriter,
    NullArtifactWriter,
    WriteReport,
)
from ariadne.loop_module.budget import (
    BudgetDecision,
    BudgetGuard,
    BudgetUsage,
    BudgetVerdict,
)
from ariadne.loop_module.checkpoint import Checkpoint, CheckpointStore
from ariadne.loop_module.context import ContextBuilder
from ariadne.loop_module.critique import Critique, CritiqueSynthesizer
from ariadne.loop_module.fingerprint import (
    IterationTrace,
    OscillationDetector,
    OscillationReport,
    OscillationVerdict,
    failure_fingerprint,
    output_fingerprint,
)
from ariadne.loop_module.goal import Assertion, AssertionKind, Goal
from ariadne.loop_module.goal_validation import validate_goal
from ariadne.loop_module.idempotency import (
    IDEMPOTENCY_TTL_SECONDS,
    decode_outcome,
    encode_outcome,
)
from ariadne.loop_module.idempotency import build_key as build_idempotency_key
from ariadne.loop_module.modes import BaseLoopMode, LoopModeFactory
from ariadne.loop_module.state_machine import (
    InvalidTransitionError,
    LoopEvent,
    LoopState,
    next_state,
)
from ariadne.loop_module.verifier import VerifierFactory
from ariadne.loop_module.verifier.base import (
    AssertionOutcome,
    BaseVerifier,
    Verdict,
    VerificationContext,
    judge,
)
from ariadne.loop_module.verifier.builtin import MetricProvider
from ariadne.utils.logging import get_logger
from ariadne.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from ariadne.harness_module.audit import AuditSink
    from ariadne.harness_module.evaluator import HarnessEvaluator
    from ariadne.loop_module.verifier.command import CommandRunner

logger = get_logger(__name__)

# 预扣估算的安全系数。估高比估低安全：低估会超支（账单事故），
# 高估只会偶尔误触发降级。LLM 输出长度难精确预估，用上下文 × 系数兜底。
RESERVE_FACTOR = 1.5


@dataclass(frozen=True)
class LLMResponse:
    """LLM 调用结果。

    usage 不可缺省：预算结算必须按真实用量，估算会让预算熔断失准。
    rate_limit_quota 可选：Anthropic 等 provider 返回剩余配额，用于预测性调度。
    """

    output: str
    input_tokens: int
    output_tokens: int
    # 模型自称完成。**仅记录用于假完成统计，不参与收敛判定**。
    claimed_done: bool = False
    model: str = ""
    cost_usd: Decimal = Decimal(0)
    # 速率限制配额信息（主动余量调度）。None 表示 provider 不返回配额头。
    rate_limit_quota: Any = None  # typing.Any 避免循环导入，实际类型是 RateLimitQuota | None


class LLMClient(Protocol):
    """LLM 调用抽象。

    抽象成 Protocol 让单元测试用脚本桩跑通收敛路径，生产用真实 provider
    adapter。engine 不关心 provider 细节，只管输出与用量。
    """

    async def complete(self, prompt: str, *, model: str) -> LLMResponse: ...


class Clock(Protocol):
    """时钟抽象。测试用可控时钟验证墙钟熔断，生产用 time.monotonic。"""

    def monotonic(self) -> float: ...


class MonotonicClock:
    def monotonic(self) -> float:
        import time

        return time.monotonic()


class IdempotencyStore(Protocol):
    """幂等键存储。防止 Worker 接管后副作用重复执行（docs/03 第 9 节）。

    三个方法而非只有 try_acquire：跳过执行后引擎必须报一个结果，
    而裸租约拿不回上次结果 —— 报失败会让 Loop 去改没坏的代码，报通过
    是伪造。语义细节见 loop_module.idempotency 的模块 docstring。
    """

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool: ...

    async def recall(self, key: str) -> str | None: ...

    async def remember(self, key: str, payload: str, ttl_seconds: int) -> None: ...


class NullIdempotencyStore:
    """不幂等。仅用于无副作用的纯生成场景。

    try_acquire 恒真 + recall 恒空 = 每次都执行、从不复用，
    与"没有幂等守卫"等价。
    """

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool:
        return True

    async def recall(self, key: str) -> str | None:
        return None

    async def remember(self, key: str, payload: str, ttl_seconds: int) -> None:
        return None


class EventSink(Protocol):
    """SSE/进度事件出口。engine 每次状态变化推送，前端据此渲染进化视图。"""

    async def emit(self, event: LoopEvent) -> None: ...


class NullEventSink:
    async def emit(self, event: LoopEvent) -> None: ...


@dataclass
class LoopConfig:
    """Engine 装配。

    刻意把所有可注入依赖放一个 dataclass：构造 engine 的地方（worker/API）
    只需组装这一个对象，且测试能只替换需要的部分。
    """

    goal: Goal
    loop_id: str
    project_id: UUID
    budget_guard: BudgetGuard
    llm: LLMClient
    checkpoint_store: CheckpointStore
    mode: BaseLoopMode = field(default_factory=lambda: LoopModeFactory("quality"))
    metric_provider: MetricProvider | None = None
    critique: CritiqueSynthesizer = field(default_factory=CritiqueSynthesizer)
    oscillator: OscillationDetector = field(default_factory=OscillationDetector)
    idempotency: IdempotencyStore = field(default_factory=NullIdempotencyStore)
    event_sink: EventSink = field(default_factory=NullEventSink)
    clock: Clock = field(default_factory=MonotonicClock)
    # 真实模型名。None 时用 mode.base_model（测试用桩模式名）。
    # 生产装配（worker）传入 settings.llm 的 model / degraded_model。
    model: str | None = None
    degraded_model: str | None = None
    # COMMAND 类断言的工作目录。None 时 command 断言记 errored（见 CommandVerifier）
    artifact_path: Path | None = None
    # 副作用工具执行器。模型在输出里用 ```ariadne-tool 围栏块显式请求
    # 调用（契约见 loop_module.tools），引擎在产出物落盘后执行。
    # None 时模型请求工具按执行失败处理 —— 绝不静默忽略，否则模型以为
    # 副作用已发生而断言在验证磁盘，Loop 只会空转烧预算。
    tool_executor: Callable[[str, dict[str, object]], Awaitable[str]] | None = None
    # M4 Harness 规则引擎。None 时 _precheck 行为同 M3（硬编码放行）。
    harness: HarnessEvaluator | None = None
    # M4 审计 sink。None 时用 NullAuditSink（不记审计）。
    audit_sink: AuditSink | None = None
    # COMMAND 断言的底层执行器。None 时用 RestrictedRunner（受限子进程）。
    # 生产装配（worker）在沙箱可用时传入 SandboxRunner —— 受限子进程按自己的
    # 文档防不住内核层逃逸，不该是多租户下的默认。
    command_runner: CommandRunner | None = None
    # 产出物落盘器。把模型输出物化成 artifact_path 下的文件，供 COMMAND 断言验证。
    # None 且存在 COMMAND 断言 + artifact_path 时，engine 自动装 FencedCodeWriter
    # —— 见 _build_artifact_writer。要显式关闭请传 NullArtifactWriter。
    artifact_writer: ArtifactWriter | None = None


@dataclass
class IterationResult:
    """单轮可见结果。供 SSE 推送与进化视图消费。"""

    iteration: int
    state: LoopState
    verdict: Verdict | None
    critique: Critique | None
    usage: BudgetUsage
    output_fp: str
    failure_fp: str
    false_completion: bool


@dataclass
class LoopOutcome:
    """Loop 终态结果。

    last_rate_limit_quota 用于主动余量调度：并行池读取最后一次 LLM 调用
    的配额信息，预测配额耗尽前主动降并发（2026-09-03）。
    """

    loop_id: str
    final_state: LoopState
    iterations: int
    usage: BudgetUsage
    verdict: Verdict | None
    converged: bool
    # 最后一次 LLM 调用的速率限制配额（主动余量调度）。
    # None 表示未调用 LLM 或 provider 不返回配额头。
    last_rate_limit_quota: Any = None

    @property
    def success(self) -> bool:
        return self.final_state is LoopState.CONVERGED


class LoopEngine:
    """状态机驱动的主循环。

    不持有任何不可恢复的运行时状态——所有跨轮状态（预算用量、振荡历史、
    上一轮输出）都在每轮结束后落检查点。这样 Worker 崩溃后另一实例只需
    重新构造 engine + 从检查点恢复，无需共享内存。
    """

    def __init__(self, config: LoopConfig) -> None:
        self._cfg = config
        self._goal = config.goal
        self._guard = config.budget_guard
        self._context = ContextBuilder(
            goal=self._goal, output_mode=config.mode.output_mode(self._goal)
        )
        # Goal 的 stall 参数同步到振荡检测器。OscillationDetector 有自己的默认值，
        # 但 Goal 上的值才是用户配置 —— 不同步会让 Goal(stall_threshold=...) 被静默忽略
        config.oscillator.stall_threshold = self._goal.stall_threshold
        config.oscillator.stall_patience = self._goal.stall_patience
        # 运行态：每轮重建前从检查点恢复
        self._state: LoopState = LoopState.CREATED
        self._iteration = 0
        self._last_output = ""
        self._previous_output = ""
        self._last_verdict: Verdict | None = None
        self._last_critique: Critique | None = None
        self._last_response: LLMResponse = None  # type: ignore[assignment]
        self._last_outcomes: tuple[AssertionOutcome, ...] = ()
        self._execution_error: str = ""  # 最近一次执行失败的错误描述
        # critique 历史：供上下文"历史失败摘要"段。每轮生成后追加。
        self._critiques: list[Critique] = []
        self._start_time = config.clock.monotonic()
        self._verifiers = self._build_verifiers()
        self._artifact_writer = self._build_artifact_writer()
        self._last_write: WriteReport | None = None

    def _build_artifact_writer(self) -> ArtifactWriter:
        """装配产出物落盘器。

        默认值刻意是"会落盘"而非"不落盘"：只要目标里有 COMMAND 断言且
        给了工作目录，就说明这个 Loop 要验证磁盘上的文件。此时不落盘的
        后果不是报错而是**静默空转** —— 每轮跑同一份没变过的文件，Loop
        烧完预算也不可能收敛。让正确的事成为默认，是因为出错的那一侧
        没有任何可见信号。

        没有 COMMAND 断言时用 NullArtifactWriter：纯文本场景（REGEX /
        SCHEMA 断言只看 output 字符串）落盘是多余的 IO。

        有 harness 时套上 pre_persist 卡点（GuardedArtifactWriter）——
        与 _build_command_runner 套 pre_tool 卡点同构。这里是 output.yaml
        那条 pre_persist 规则（output-sensitive-high）唯一的求值点：
        落盘只有 engine._persist_artifacts → artifact_writer.write 这一条
        路径。harness=None 时返回裸 writer，不加求值开销。
        """
        if self._cfg.artifact_writer is not None:
            inner: ArtifactWriter = self._cfg.artifact_writer
        else:
            needs_files = any(
                a.kind is AssertionKind.COMMAND for a in self._goal.assertions
            )
            inner = (
                FencedCodeWriter()
                if needs_files and self._cfg.artifact_path is not None
                else NullArtifactWriter()
            )

        if self._cfg.harness is None:
            return inner

        from ariadne.harness_module.audit import NullAuditSink
        from ariadne.runtime_module.artifact.guarded import GuardedArtifactWriter

        return GuardedArtifactWriter(
            inner=inner,
            evaluator=self._cfg.harness,
            audit_sink=self._cfg.audit_sink or NullAuditSink(),
            loop_state_provider=self.harness_loop_context,
            project_id=self._cfg.project_id,
            loop_id=self._cfg.loop_id,
        )

    def _build_verifiers(self) -> dict[AssertionKind, BaseVerifier]:
        """按 Goal 用到的断言类型装配 Verifier。按需构造，不浪费。"""
        kinds = {a.kind for a in self._goal.assertions}
        verifiers: dict[AssertionKind, BaseVerifier] = {}
        for kind in kinds:
            if kind is AssertionKind.METRIC:
                provider = self._cfg.metric_provider
                if provider is None:
                    # 无 metric provider 时给空字典桩：metric 断言会记 errored
                    from ariadne.loop_module.verifier.builtin import DictMetricProvider

                    provider = DictMetricProvider({})
                verifiers[kind] = VerifierFactory(kind, provider=provider)
            elif kind is AssertionKind.COMMAND:
                verifiers[kind] = VerifierFactory(kind, runner=self._build_command_runner())
            else:
                verifiers[kind] = VerifierFactory(kind)
        return verifiers

    def _build_command_runner(self) -> CommandRunner:
        """COMMAND 断言的执行器。有 harness 时套上 pre_tool 卡点。

        这里是 tool.yaml 那批 pre_tool 规则的求值点之一 —— Loop 里执行
        命令串的路径有两条：CommandVerifier → CommandRunner（本方法），
        以及模型的 ariadne-tool 工具调用（_guard_tool，同样过 pre_tool）。

        底层执行器取 cfg.command_runner，未注入时是 RestrictedRunner（受限
        子进程）—— 保持不装配沙箱的调用方（测试、单机开发）行为不变。

        harness=None 时返回裸执行器，不套卡点：没有规则集的 Loop 不该多出
        一层求值开销。

        与 loop_state_provider 后绑的不对称之处：verifier 在 __init__ 里构造
        （见上面的 self._verifiers），此时 self.harness_loop_context 已可绑，
        不需要 loop_worker 里 GuardedLLMAdapter 那样的回填步骤。
        """
        from ariadne.loop_module.verifier.command import RestrictedRunner

        inner = self._cfg.command_runner or RestrictedRunner()
        if self._cfg.harness is None:
            return inner

        from ariadne.harness_module.audit import NullAuditSink
        from ariadne.runtime_module.tool.guarded import GuardedCommandRunner

        return GuardedCommandRunner(
            inner=inner,
            evaluator=self._cfg.harness,
            audit_sink=self._cfg.audit_sink or NullAuditSink(),
            loop_state_provider=self.harness_loop_context,
            project_id=self._cfg.project_id,
            loop_id=self._cfg.loop_id,
        )

    async def run(self, *, _skip_restore: bool = False) -> LoopOutcome:
        """执行 Loop 直到终态。

        主循环结构：每个阶段产出 LoopEvent，next_state 决定去向，
        engine 不自造转移。这样状态机的正确性测试不被引擎逻辑污染。

        _skip_restore 仅供 resume() 使用：审批后已推进到 EXECUTING，再走
        恢复逻辑会用检查点覆盖审批状态。正常调用不要传此参数。
        """
        if not _skip_restore:
            # 崩溃恢复：有检查点则从断点续跑，预算/历史从检查点恢复
            checkpoint = await self._cfg.checkpoint_store.latest(
                self._cfg.loop_id, project_id=self._cfg.project_id
            )
            if checkpoint is not None:
                self._restore(checkpoint)
                # 恢复到 HUMAN_PENDING：等外部审批，不进主循环（否则 _step 从
                # HUMAN_PENDING 发 CONTEXT_READY 是非法转移）
                if self._state is LoopState.HUMAN_PENDING:
                    return self._outcome()
                # === 主迭代循环 ===（恢复时跳过 VALIDATE：目标已验证过）
            else:
                # === VALIDATE ===（首启动）
                await self._advance(LoopEvent.START)
                event = await self._validate()
                await self._advance(event)
                if self._state is LoopState.REJECTED:
                    return self._outcome()

        # === 主迭代循环 ===
        while not self._is_terminal():
            state = await self._step()
            # _step 已推进到下一阶段的入口状态
            if state is LoopState.CONVERGED:
                break
            if state is LoopState.REVISING:
                continue  # → PLANNING 进入下一轮
            if state is LoopState.HUMAN_PENDING:
                return self._outcome()  # HITL：等外部审批
            if state is LoopState.PRECHECK:
                # 仍在迭代中，循环继续
                continue
            if self._is_terminal():
                # 终态（BLOCKED/BUDGET_EXCEEDED/MAX_ITERATIONS/STALLED/FAILED/CANCELLED）
                return self._outcome()

        return self._outcome()

    async def resume(self, approved: bool) -> LoopOutcome:
        """HUMAN_PENDING 后由人工审批推进。

        approved=True → EXECUTING 继续迭代；False → REJECTED 终止。
        超时由调用方判定后以 False 调用（fail-closed，见状态机）。
        """
        if self._state is not LoopState.HUMAN_PENDING:
            raise InvalidTransitionError(self._state, LoopEvent.APPROVED)
        event = LoopEvent.APPROVED if approved else LoopEvent.REJECTED_BY_HUMAN
        state = await self._advance(event)
        if state is not LoopState.EXECUTING:
            return self._outcome()
        # 跳过恢复：审批已把状态推进到 EXECUTING，恢复逻辑会用检查点覆盖。
        return await self.run(_skip_restore=True)

    async def _step(self) -> LoopState:
        """执行单轮迭代（PLANNING → ... → JUDGING），返回结束时的状态。

        提取成方法让 run() 用局部变量持有状态，避免 mypy 对 self._state
        的跨方法收窄失效。状态转移全由 next_state 决定，本方法不自造转移。
        """
        # PLANNING: 构造上下文前先查预算与轮次
        state = await self._advance(await self._planning())
        if state is not LoopState.PRECHECK:
            return state  # BUDGET_EXCEEDED / MAX_ITERATIONS / CANCELLED

        # PRECHECK: M3 用最小安全默认值（硬编码允许），M4 接 Harness
        state = await self._advance(await self._precheck())
        if state in (LoopState.BLOCKED, LoopState.HUMAN_PENDING):
            return state

        # EXECUTING: 调 LLM（预算预扣 → 调用 → 结算）
        state = await self._advance(await self._executing())
        if state is LoopState.FAILED:
            return state
        if state is LoopState.JUDGING:
            # EXECUTION_FAILED → JUDGING：执行失败按模式决定重试/修正。
            # 不走 EVALUATING（没有新输出可验证），直接判修正。
            return await self._judge_execution_failure()
        if state is not LoopState.EVALUATING:
            return state  # BUDGET_EXCEEDED / BLOCKED / CANCELLED → 终态

        # EVALUATING: 跑 Verifier 求值
        state = await self._advance(await self._evaluating())
        if state is LoopState.FAILED:
            return state

        # JUDGING: 收敛判定 + 振荡检测 + 落检查点
        return await self._advance(await self._judging())

    # ---------- 阶段实现 ----------

    async def _validate(self) -> LoopEvent:
        """VALIDATE：目标可验证性校验。把模糊指令挡在系统之外。"""
        report = validate_goal(
            self._goal,
            available_metrics=self._available_metric_names(),
            sandbox_available=self._cfg.artifact_path is not None,
        )
        if report.ok:
            return LoopEvent.GOAL_VALID
        logger.warning(
            "goal rejected",
            extra={
                "loop_id": self._cfg.loop_id,
                "errors": [i.message for i in report.errors],
                "warnings": [i.message for i in report.warnings],
            },
        )
        return LoopEvent.GOAL_INVALID

    def _available_metric_names(self) -> frozenset[str]:
        provider = self._cfg.metric_provider
        if provider is None:
            return frozenset()
        # EvaluatorMetricProvider 暴露 evaluators 字典；DictMetricProvider 无此属性
        evaluators = getattr(provider, "_evaluators", None)
        if isinstance(evaluators, dict):
            return frozenset(evaluators.keys())
        return frozenset()

    async def _planning(self) -> LoopEvent:
        """PLANNING：检查轮次与预算，构造上下文。

        轮次上限在此检查：构造上下文前就该知道还能不能跑（状态机注释）。
        若上一轮结束在 REVISING，先发 REVISION_READY 回到 PLANNING 再继续。
        """
        if self._state is LoopState.REVISING:
            await self._advance(LoopEvent.REVISION_READY)

        next_iteration = self._iteration + 1

        decision = self._guard.check_iteration(next_iteration)
        if decision.verdict is BudgetVerdict.EXCEEDED_ITERATION:
            return LoopEvent.MAX_ITERATIONS_REACHED

        # 墙钟检查放这里：每轮开始查一次，避免卡在不返回的调用上
        elapsed = self._cfg.clock.monotonic() - self._start_time
        wall = self._guard.check_wall_clock(elapsed)
        if wall.verdict is BudgetVerdict.EXCEEDED_WALL_CLOCK:
            return LoopEvent.BUDGET_EXCEEDED

        self._iteration = next_iteration
        # 递增计数器的 iterations，让 BudgetUsage.iterations 准确（前端进度条用）
        self._guard.record_iteration()
        return LoopEvent.CONTEXT_READY

    async def _precheck(self) -> LoopEvent:
        """PRECHECK：Harness 规则引擎求值 pre_model 卡点。

        M4 接入 Harness（CEL 求值 + 五卡点）。harness=None 时行为同 M3
        （硬编码放行）。Harness 判定 BLOCK → RULES_BLOCKED → BLOCKED 终态，
        不能通过多试几轮绕过（硬约束）。
        """
        if self._cfg.mode.requires_pre_approval(self._goal):
            return LoopEvent.APPROVAL_REQUIRED

        if self._cfg.harness is not None:
            from ariadne.harness_module.models import HarnessContext, HookKind

            ctx = HarnessContext(
                hook=HookKind.PRE_MODEL,
                input={"text": self._last_output or self._goal.spec_summary()},
                loop=self.harness_loop_context(),
            )
            decision = self._cfg.harness.evaluate(
                hook=HookKind.PRE_MODEL, context=ctx
            )

            # 审计
            if self._cfg.audit_sink is not None:
                import contextlib

                from ariadne.harness_module.audit import AuditRecord

                record = AuditRecord.create(
                    project_id=self._cfg.project_id,
                    hook=HookKind.PRE_MODEL,
                    action=decision.action,
                    rule_hits=decision.hits,
                    winning_hit=decision.winning_hit,
                    context_snapshot={
                        "iteration": self._iteration,
                        "loop_id": self._cfg.loop_id,
                    },
                    loop_id=self._cfg.loop_id,
                )
                with contextlib.suppress(Exception):
                    await self._cfg.audit_sink.write(record)

            if decision.blocked:
                logger.warning(
                    "harness blocked at precheck",
                    extra={
                        "loop_id": self._cfg.loop_id,
                        "rule_hits": len(decision.hits),
                    },
                )
                return LoopEvent.RULES_BLOCKED
            if decision.needs_approval:
                return LoopEvent.APPROVAL_REQUIRED

        return LoopEvent.RULES_PASSED

    def harness_loop_context(self) -> dict[str, Any]:
        """Harness resource 类规则的 loop 上下文。

        公开而非私有：GuardedLLMAdapter.loop_state_provider 要绑到这上面。
        适配器在引擎之前构造（引擎把 llm 当入参），所以只能后绑 —— 见
        worker.loop_worker._build_engine。

        键名与类型按 LOOP_CONTEXT_DEFAULTS 契约填 —— 曾经这里填的是
        `budget_used=cost_usd`（把美元填进 token 预算字段）和引擎自造的
        `budget_remaining`（无人消费），而规则读的 budget_limit / cost_limit /
        max_iterations 一个都没填。缺键在 CEL 里是求值错误，被 fail-closed
        兜成命中，等于 resource 规则一开就无条件拦截。

        同一份上下文供 pre_model 卡点和 GuardedLLMAdapter 的 loop_state_provider
        复用，两处读同一个契约，不会再各写各的。
        """
        usage = self._guard.usage
        budget = self._goal.budget
        return {
            "iteration": self._iteration,
            "max_iterations": budget.max_iterations,
            "budget_used": usage.total_tokens,
            "budget_limit": budget.max_total_tokens,
            "cost_usd": float(usage.cost_usd),
            "cost_limit": float(budget.max_cost_usd),
        }

    async def _executing(self) -> LoopEvent:
        """EXECUTING：调 LLM，预算预扣 → 调用 → 结算。

        预扣估算用上下文长度 × 安全系数。settle 按真实用量修正差额。
        失败时 release 释放预扣——不释放会让失败也吃预算，多次失败后
        Loop 因"预算耗尽"终止但一个 Token 没花。

        Retry 模式的原地重试在此落地：可重试错误（429/超时/网络抖动）
        release → 退避 → 重新 reserve → 重新调用，直到模式给出 retry=False。
        预算正确性靠"失败的尝试不烧预算"维持。HarnessBlockError 是硬
        约束，短路在重试判断之前——不能通过重试绕过。
        """
        from ariadne.runtime_module.llm.guarded import HarnessBlockError

        context = self._context.build(
            iteration=self._iteration,
            last_output=self._last_output,
            previous_output=self._previous_output,
            critique=self._last_critique,
            history=self._critiques,
        )
        prompt = context.render()

        estimated = int(estimate_tokens(prompt) * RESERVE_FACTOR)
        decision = self._guard.reserve(estimated)
        if decision.verdict.is_fatal:
            logger.warning(
                "budget exhausted before execution",
                extra={"loop_id": self._cfg.loop_id, "reason": decision.reason},
            )
            return LoopEvent.BUDGET_EXCEEDED

        attempt = 0
        while True:
            model = self._select_model(decision)
            try:
                response = await self._cfg.llm.complete(prompt, model=model)
                break
            except HarnessBlockError as exc:
                # GuardedLLMAdapter 的 BLOCK → RULES_BLOCKED，硬约束
                self._guard.release(decision.reserved_tokens)
                logger.warning(
                    "harness blocked at executing",
                    extra={
                        "loop_id": self._cfg.loop_id,
                        "action": exc.decision.action.value,
                    },
                )
                return LoopEvent.RULES_BLOCKED
            except Exception as exc:
                self._guard.release(decision.reserved_tokens)
                self._execution_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "llm call failed",
                    extra={"loop_id": self._cfg.loop_id, "error": type(exc).__name__},
                )
                # 按模式决定：Retry 模式原地退避重试，其余交 JUDGING 走修正。
                # attempt 是本次调用的局部重试计数（非 iteration）。
                retry = self._cfg.mode.should_retry(exc, attempt)
                if not retry.retry:
                    return LoopEvent.EXECUTION_FAILED
                if retry.delay_seconds > 0:
                    await asyncio.sleep(retry.delay_seconds)
                attempt += 1
                decision = self._guard.reserve(estimated)
                if decision.verdict.is_fatal:
                    logger.warning(
                        "budget exhausted during retry",
                        extra={"loop_id": self._cfg.loop_id, "attempt": attempt},
                    )
                    return LoopEvent.BUDGET_EXCEEDED

        self._execution_error = ""
        usage = self._guard.settle(
            decision.reserved_tokens,
            response.input_tokens + response.output_tokens,
            response.cost_usd,
        )
        logger.info(
            "iteration executed",
            extra={
                "loop_id": self._cfg.loop_id,
                "iteration": self._iteration,
                "tokens": usage.total_tokens,
                "claimed_done": response.claimed_done,
            },
        )

        self._previous_output = self._last_output
        self._last_output = response.output
        self._last_response = response
        event = self._persist_artifacts()
        if event is LoopEvent.EXECUTION_FAILED:
            return event
        return await self._execute_tools()

    def _persist_artifacts(self) -> LoopEvent:
        """把本轮输出物化到工作目录，供 COMMAND 断言验证。

        落盘失败按**执行失败**处理而不是继续走验证：产出物没写进去时，
        COMMAND 断言跑的是上一轮（或初始）的文件，会得出一个与本轮输出
        无关的结论 —— 那比直接失败更糟，因为 critique 会据此让模型去改
        一个它已经改对了的地方。EXECUTION_FAILED 路径会把原因带进
        critique，模型下一轮能看到"你没标注文件名"这类可修正的信息。
        """
        workdir = self._cfg.artifact_path
        if workdir is None:
            return LoopEvent.EXECUTION_DONE

        report = self._artifact_writer.write(self._last_output, workdir)
        self._last_write = report
        if report.ok:
            if report.written:
                logger.info(
                    "artifacts persisted",
                    extra={
                        "loop_id": self._cfg.loop_id,
                        "iteration": self._iteration,
                        "files": list(report.written),
                    },
                )
            return LoopEvent.EXECUTION_DONE

        self._execution_error = f"产出物落盘失败：{report.describe()}"
        logger.warning(
            "artifact write failed",
            extra={
                "loop_id": self._cfg.loop_id,
                "iteration": self._iteration,
                "error": report.error,
            },
        )
        return LoopEvent.EXECUTION_FAILED

    async def _execute_tools(self) -> LoopEvent:
        """执行模型输出里的 ```ariadne-tool 工具调用（契约见 loop_module.tools）。

        失败语义与产出物落盘一致：按**执行失败**处理而非继续走验证。
        工具是副作用，工具没跑成时后续断言验证的是一个与模型意图无关的
        状态 —— critique 必须知道"工具没执行成功"以及为什么。

        每条调用都过三道关：Harness pre_tool 规则（硬约束，不能靠重试
        绕过）→ 幂等守卫（Worker 接管后不重复执行副作用）→ 真实执行器。
        """
        from ariadne.loop_module.tools import parse_tool_directives

        report = parse_tool_directives(self._last_output)
        if not report.directives and not report.errors:
            return LoopEvent.EXECUTION_DONE

        if self._cfg.tool_executor is None:
            self._execution_error = (
                "模型请求执行工具，但本 Loop 未配置工具执行器 —— "
                "请把文件内容放进带 path= 标注的围栏代码块，由产出物落盘写入"
            )
            logger.warning(
                "tool requested but no executor configured",
                extra={"loop_id": self._cfg.loop_id, "iteration": self._iteration},
            )
            return LoopEvent.EXECUTION_FAILED

        failures = list(report.errors)
        for index, directive in enumerate(report.directives):
            rejection = await self._guard_tool(directive)
            if rejection:
                failures.append(rejection)
                continue
            result, error = await self._run_tool(index, directive)
            if error:
                failures.append(error)
                continue
            logger.info(
                "tool executed",
                extra={
                    "loop_id": self._cfg.loop_id,
                    "iteration": self._iteration,
                    "tool": directive.name,
                    "result": result,
                },
            )

        if failures:
            self._execution_error = "工具执行失败：" + "；".join(failures)
            return LoopEvent.EXECUTION_FAILED
        return LoopEvent.EXECUTION_DONE

    async def _guard_tool(self, directive: Any) -> str:
        """Harness pre_tool 求值 + 审计。返回拒绝原因，放行返回空串。"""
        if self._cfg.harness is None:
            return ""

        from ariadne.harness_module.audit import AuditRecord
        from ariadne.harness_module.models import HarnessContext, HookKind

        ctx = HarnessContext(
            hook=HookKind.PRE_TOOL,
            tool={"tool": directive.name, "args": dict(directive.args)},
            loop=self.harness_loop_context(),
        )
        decision = self._cfg.harness.evaluate(hook=HookKind.PRE_TOOL, context=ctx)

        if self._cfg.audit_sink is not None:
            import contextlib

            record = AuditRecord.create(
                project_id=self._cfg.project_id,
                hook=HookKind.PRE_TOOL,
                action=decision.action,
                rule_hits=decision.hits,
                winning_hit=decision.winning_hit,
                context_snapshot={
                    "tool": directive.name,
                    "loop_id": self._cfg.loop_id,
                    "iteration": self._iteration,
                },
                loop_id=self._cfg.loop_id,
            )
            with contextlib.suppress(Exception):
                await self._cfg.audit_sink.write(record)

        if decision.blocked:
            return f"工具 {directive.name} 被 Harness 规则拦截（硬约束）"
        if decision.needs_approval:
            # 执行路径上没有挂起等审批的生命周期（挂起点在状态机的
            # APPROVAL_REQUIRED，见 GuardedCommandRunner 同款注释）。
            # fail-closed：判失败，critique 会说明原因。
            return f"工具 {directive.name} 需要人工审批，本轮按失败处理"
        return ""

    async def _run_tool(
        self, index: int, directive: Any
    ) -> tuple[str, str]:
        """执行单条工具指令，带幂等守卫。返回 (结果, 错误)。

        与 COMMAND 断言同款守卫：同一轮的工具调用在 Worker 接管后不得
        重复执行副作用。抢不到幂等键且无可复用结果时 fail-closed。
        """
        key = build_idempotency_key(
            self._cfg.loop_id,
            self._iteration,
            f"tool-{index}-{directive.name}",
        )
        acquired = await self._cfg.idempotency.try_acquire(
            key, IDEMPOTENCY_TTL_SECONDS
        )
        if not acquired:
            cached = await self._cfg.idempotency.recall(key)
            if cached is not None:
                return cached, ""
            return (
                "",
                f"工具 {directive.name} 已被另一次执行占用且无可复用结果，"
                "跳过以避免重复副作用",
            )

        assert self._cfg.tool_executor is not None
        try:
            result = await self._cfg.tool_executor(
                directive.name, dict(directive.args)
            )
        except Exception as exc:
            return "", f"{directive.name} 执行失败: {type(exc).__name__}: {exc}"
        await self._cfg.idempotency.remember(
            key, result, IDEMPOTENCY_TTL_SECONDS
        )
        return result, ""

    def _select_model(self, decision: BudgetDecision) -> str:
        """按预算裁决选模型。

        DEGRADE → mode 的降级模型或配置的 degraded_model；
        正常 → 配置的真实模型名覆盖 mode 的占位符
        （测试用桩模式名，生产用 settings.llm）。
        """
        model = self._cfg.mode.model_for()
        if decision.verdict is BudgetVerdict.DEGRADE and self._cfg.degraded_model:
            return self._cfg.degraded_model
        if not decision.verdict.is_fatal and self._cfg.model:
            return self._cfg.model
        return model

    async def _evaluating(self) -> LoopEvent:
        """EVALUATING：对每条断言跑 Verifier 求值。

        Verifier 契约：verify() 不抛异常（base.py 兜底）。单条断言失败
        不会让整轮崩掉——那会让 Loop 进 FAILED 而非给出可修正的反馈。
        """
        response = self._last_response
        ctx = VerificationContext(
            output=response.output,
            task=self._goal.task,
            artifact_path=self._cfg.artifact_path,
            claimed_done=response.claimed_done,
        )

        outcomes: list[AssertionOutcome] = []
        for assertion in self._goal.assertions:
            outcome = await self._verify_one(assertion, ctx)
            outcomes.append(outcome)

        self._last_outcomes = tuple(outcomes)
        return LoopEvent.EVALUATION_DONE

    async def _verify_one(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        """跑单条断言。COMMAND 类经幂等守卫，其余直接跑。

        只守 COMMAND：SCHEMA/REGEX/METRIC 是输出的纯函数，重跑无副作用；
        HUMAN 走 HUMAN_PENDING 审批，不在此执行。COMMAND 的命令串来自
        用户 spec，没有任何东西强制它只读 —— 可以是 git push。

        为什么守卫在引擎而不在 CommandVerifier 内部：verifier 的 `_verify`
        是同步的，而幂等存储是 async；且轮次号只有引擎知道。
        """
        verifier = self._verifiers[assertion.kind]
        if assertion.kind is not AssertionKind.COMMAND:
            return verifier.verify(assertion, ctx)

        key = build_idempotency_key(self._cfg.loop_id, self._iteration, assertion.id)
        acquired = await self._cfg.idempotency.try_acquire(
            key, IDEMPOTENCY_TTL_SECONDS
        )
        if acquired:
            outcome: AssertionOutcome = verifier.verify(assertion, ctx)
            await self._cfg.idempotency.remember(
                key, encode_outcome(outcome), IDEMPOTENCY_TTL_SECONDS
            )
            return outcome

        # 没抢到租约 —— 这一步之前执行过。要么已有结果可复用，
        # 要么另一个 Worker 正在跑（只有租约信封）。
        cached = await self._cfg.idempotency.recall(key)
        if cached is not None:
            restored = decode_outcome(cached)
            if restored is not None:
                logger.info(
                    "命令断言命中幂等键，复用上次结果",
                    extra={
                        "loop_id": self._cfg.loop_id,
                        "iteration": self._iteration,
                        "assertion_id": assertion.id,
                        "passed": restored.passed,
                    },
                )
                return restored

        # fail-closed：已知有人执行过但拿不到结果，重跑会重复副作用。
        # 记 errored 而非判失败 —— errored 不算通过（judge 不会收敛），
        # 也不会让 critique 给出"修代码"的无效指令。
        logger.warning(
            "命令断言的幂等键被占用且无可复用结果，跳过执行",
            extra={
                "loop_id": self._cfg.loop_id,
                "iteration": self._iteration,
                "assertion_id": assertion.id,
            },
        )
        return AssertionOutcome(
            assertion_id=assertion.id,
            kind=assertion.kind,
            passed=False,
            evidence=(
                "该步骤已被另一次执行占用（幂等守卫），本次跳过以避免重复副作用"
            ),
            errored=True,
        )

    async def _judge_execution_failure(self) -> LoopState:
        """执行失败后的判定：不收敛，生成"执行失败"critique，进修正。

        不跑 Verifier（没有新输出）。执行失败可能是 provider 抖动或任务本身难——
        交给 critique 让模型在下一轮调整。Retry 模式若原地重试已在 _executing 处理。
        """
        # 合成一个失败裁决：blocking 断言不可能通过（执行都没成功）
        failed_outcomes = tuple(
            AssertionOutcome(
                assertion_id=a.id, kind=a.kind, passed=False, evidence="执行失败"
            )
            for a in self._goal.blocking_assertions
        )
        verdict = judge(failed_outcomes, self._goal, claimed_done=False)
        self._last_verdict = verdict

        # 执行失败时本轮没有产生新输出，output_fp 用空串而非上一轮的指纹——
        # 用上一轮会让振荡检测误把"连续执行失败"当成"输出振荡"。
        ofp = ""
        ffp = failure_fingerprint(verdict.failed_ids)
        trace = IterationTrace(
            iteration=self._iteration,
            output_fp=ofp,
            failure_fp=ffp,
            score=0.0,
            failed_ids=verdict.failed_ids,
        )
        oscillation = self._cfg.oscillator.record(trace)

        critique = self._cfg.critique.synthesize(
            verdict, self._goal, oscillation=oscillation
        )
        # 执行失败时把执行错误信息补进 critique，让模型知道发生了什么
        if self._execution_error:
            critique = Critique(
                failures=(*critique.failures, f"执行失败：{self._execution_error}"),
                evidence=critique.evidence,
                directives=critique.directives or ("检查并修正导致执行失败的问题",),
                forbidden=critique.forbidden,
                escalation=critique.escalation,
            )
        self._critiques.append(critique)
        self._last_critique = critique

        await self._save_checkpoint(ofp, ffp, oscillation, verdict, critique)
        return await self._advance(LoopEvent.NEEDS_REVISION)

    async def _judging(self) -> LoopEvent:
        """JUDGING：收敛判定 + 振荡检测 + 落检查点。

        Ralph 原则落点：converged 只看 blocking 断言，与 claimed_done 无关。
        振荡检测在此进行：STALLED 则终止，ESCALATE 则在 critique 标注。
        """
        response = self._last_response
        verdict = judge(
            self._last_outcomes,
            self._goal,
            claimed_done=response.claimed_done,
        )
        self._last_verdict = verdict

        # 振荡检测
        ofp = output_fingerprint(response.output)
        ffp = failure_fingerprint(verdict.failed_ids)
        trace = IterationTrace(
            iteration=self._iteration,
            output_fp=ofp,
            failure_fp=ffp,
            score=verdict.score,
            failed_ids=verdict.failed_ids,
        )
        oscillation = self._cfg.oscillator.record(trace)

        # 生成 critique（收敛时不需要；未收敛时用它驱动下一轮）
        critique: Critique | None = None
        if not verdict.converged:
            critique = self._cfg.critique.synthesize(
                verdict, self._goal, oscillation=oscillation
            )
            self._critiques.append(critique)
        self._last_critique = critique

        # 落检查点 —— 状态外置的铁律
        await self._save_checkpoint(ofp, ffp, oscillation, verdict, critique)

        logger.info(
            "iteration judged",
            extra={
                "loop_id": self._cfg.loop_id,
                "iteration": self._iteration,
                "converged": verdict.converged,
                "score": verdict.score,
                "false_completion": verdict.false_completion,
                "oscillation": oscillation.verdict.value,
            },
        )

        # 判定下一事件
        if verdict.converged:
            return LoopEvent.CONVERGED

        if oscillation.verdict is OscillationVerdict.STALLED:
            return LoopEvent.STALLED

        if verdict.needs_human:
            return LoopEvent.APPROVAL_REQUIRED

        # 预算在结算后可能已越界（实际用量超预估）—— 下一轮 PLANNING 会再查，
        # 但此处提前终止避免无谓地构造上下文
        if self._guard.usage.total_tokens > self._goal.budget.max_total_tokens:
            return LoopEvent.BUDGET_EXCEEDED
        cost_limit = self._goal.budget.max_cost_usd
        if self._guard.usage.cost_usd > Decimal(str(cost_limit)):
            return LoopEvent.BUDGET_EXCEEDED

        if self._iteration >= self._goal.budget.max_iterations:
            return LoopEvent.MAX_ITERATIONS_REACHED

        return LoopEvent.NEEDS_REVISION

    async def _save_checkpoint(
        self,
        ofp: str,
        ffp: str,
        oscillation: OscillationReport,
        verdict: Verdict,
        critique: Critique | None,
    ) -> None:
        await self._cfg.checkpoint_store.save(
            Checkpoint(
                loop_id=self._cfg.loop_id,
                iteration=self._iteration,
                state=self._state,
                usage=self._guard.snapshot(),
                output_fp=ofp,
                failure_fp=ffp,
                verdict=verdict,
                critique=critique,
                last_output=self._last_output,
                previous_output=self._previous_output,
                history=self._cfg.oscillator.history,
                critique_history=self._critique_history(),
            ),
            project_id=self._cfg.project_id,
        )

    def _critique_history(self) -> tuple[Critique, ...]:
        """收集历史 critique 用于上下文的历史失败摘要段。"""
        return tuple(self._critiques)

    # ---------- 状态推进与恢复 ----------

    async def _advance(self, event: LoopEvent) -> LoopState:
        """推进状态机并推送事件。返回新状态。

        返回新状态而非 None：让调用方用局部变量持有状态，避免 mypy 对
        self._state 的跨调用收窄失效（实例属性收窄在调用方法后不放宽）。
        """
        self._state = next_state(self._state, event)
        await self._cfg.event_sink.emit(event)
        return self._state

    def _is_terminal(self) -> bool:
        from ariadne.loop_module.state_machine import is_terminal

        return is_terminal(self._state)

    def _restore(self, checkpoint: Checkpoint) -> None:
        """从检查点恢复运行态。

        关键：预算从检查点恢复（guard.restore），否则预算被重置，
        Loop 会重新花一遍全部预算——最容易出的账单事故。
        振荡历史也要恢复，否则崩溃后振荡检测失效。

        检查点落盘时状态是 JUDGING；恢复后要进下一轮，状态映射为 REVISING，
        这样 _planning 先发 REVISION_READY 回到 PLANNING。若检查点本身就是
        终态（如 CONVERGED），直接保持，主循环不会进入。
        """
        self._iteration = checkpoint.iteration
        self._last_output = checkpoint.last_output
        self._previous_output = checkpoint.previous_output
        self._last_verdict = checkpoint.verdict
        self._last_critique = checkpoint.critique
        self._guard.restore(checkpoint.usage)
        self._cfg.oscillator.restore(checkpoint.history)
        self._critiques = list(checkpoint.critique_history)
        if checkpoint.state is LoopState.JUDGING:
            # 上轮判定完，恢复后进下一轮 -> REVISING -> PLANNING
            self._state = LoopState.REVISING
        else:
            self._state = checkpoint.state
        logger.info(
            "loop restored from checkpoint",
            extra={
                "loop_id": self._cfg.loop_id,
                "iteration": checkpoint.iteration,
                "state": self._state.value,
            },
        )

    def _outcome(self) -> LoopOutcome:
        return LoopOutcome(
            loop_id=self._cfg.loop_id,
            final_state=self._state,
            iterations=self._iteration,
            usage=self._guard.snapshot(),
            verdict=self._last_verdict,
            converged=self._state is LoopState.CONVERGED,
            last_rate_limit_quota=getattr(self._last_response, "rate_limit_quota", None)
            if self._last_response
            else None,
        )


__all__ = [
    "Clock",
    "EventSink",
    "IdempotencyStore",
    "IterationResult",
    "LLMClient",
    "LLMResponse",
    "LoopConfig",
    "LoopEngine",
    "LoopOutcome",
    "MonotonicClock",
    "NullEventSink",
    "NullIdempotencyStore",
]
