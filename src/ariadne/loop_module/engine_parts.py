"""Loop Engine 的装配与 IO 方法（Mixin）。

从 engine.py 拆出（2026-09-04）：装配（ArtifactWriter / Verifier /
CommandRunner）与工具/产出物执行的实现细节独立成 mixin，让 engine.py
聚焦状态机主循环本身，符合 800 行文件上限约定。

LoopEngine(EngineAssemblyMixin, EngineIOMixin, ...) 继承这两个 mixin，
测试仍通过 engine._precheck / engine._build_artifact_writer 等私有方法
直接调用（行为不变）。mixin 依赖 self._cfg / self._goal / self._guard
等 LoopEngine 实例属性 —— 不是独立可用的类。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ariadne.loop_module.artifact import (
    ArtifactWriter,
    FencedCodeWriter,
    NullArtifactWriter,
)
from ariadne.loop_module.goal import AssertionKind
from ariadne.loop_module.idempotency import (
    IDEMPOTENCY_TTL_SECONDS,
)
from ariadne.loop_module.idempotency import build_key as build_idempotency_key
from ariadne.loop_module.state_machine import LoopEvent
from ariadne.loop_module.verifier import VerifierFactory
from ariadne.loop_module.verifier.base import BaseVerifier
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.loop_module.budget import BudgetGuard
    from ariadne.loop_module.engine_types import LoopConfig
    from ariadne.loop_module.goal import Goal
    from ariadne.loop_module.verifier.command import CommandRunner

logger = get_logger(__name__)


class EngineAssemblyMixin:
    """装配：产出物落盘器、Verifier、CommandRunner。"""

    if TYPE_CHECKING:
        _cfg: LoopConfig
        _goal: Goal
        _guard: BudgetGuard
        _iteration: int

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
                verifiers[kind] = VerifierFactory(
                    kind, runner=self._build_command_runner()
                )
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


class EngineIOMixin:
    """产出物落盘与工具执行。"""

    if TYPE_CHECKING:
        _cfg: LoopConfig
        _iteration: int
        _last_output: str
        _execution_error: str
        _artifact_writer: ArtifactWriter
        _last_write: Any
        harness_loop_context: Any

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


__all__ = [
    "EngineAssemblyMixin",
    "EngineIOMixin",
]