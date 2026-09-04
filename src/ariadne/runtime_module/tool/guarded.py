"""GuardedCommandRunner —— 用 Harness 包装命令执行（pre_tool / post_tool 卡点）。

形状与 GuardedLLMAdapter 一致：包装 Protocol、求值、按 Decision 动作处置、
写审计。包装的是 CommandRunner 而非 LLMClient，因为 Loop 里真正执行命令的
是 CommandVerifier → CommandRunner 这条路径。

与已有防线的关系（重要，别把这层当成"从无到有"）：

  第一层 ExecPolicy（M3，restricted_exec.py）：argv[0] 白名单 + shlex 拆分。
  rm / sudo / dd / su / chmod 本就不在白名单里，`pytest; rm -rf /` 也因为
  不走 shell 而只是把分号当普通参数。

  第二层 pre_tool 规则（本模块接入）：补的是前者拿不到的三件事 ——
  1. 全命令串匹配。ExecPolicy 只看 argv[0] 的 basename，看不见参数。
     `npx` 在白名单内，于是 `npx foo --url http://169.254.169.254/...`
     能过第一层，只有 tool-metadata-endpoint 规则拦得住。
  2. 租户可配置。ExecPolicy 是源码里的常量，规则来自 spec.yaml。
  3. 审计。CommandNotAllowedError 只产出一条 errored 断言，规则拦截会写
     AuditRecord（规则 id、severity、winning hit）。

  post_tool 规则（工具**返回后**求值）：查命令的**输出**而非命令本身。
  典型用途是结果大小上限 / 敏感数据过滤（docs/04 第 3 节）。

pre_tool BLOCK 抛 CommandNotAllowedError：CommandVerifier 已经捕获它
并映射为 errored=True + 消息进证据（command.py 的 _verify）。语义是——
被规则拦下的命令属于"断言配置得改"，而不是"被测输出不合格"。判失败会让
critique 给出"修代码"的无效指令。

post_tool BLOCK 的语义相反：命令**跑了**，是它的输出内容违规。此时判失败
（exit_code=1 + 消息进 stderr），critique 会据此让模型修输出而非修配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from ariadne.harness_module.audit import (
    AuditRecord,
    AuditSink,
    NullAuditSink,
    write_audit_sync,
)
from ariadne.harness_module.evaluator import HarnessEvaluator
from ariadne.harness_module.models import Action, Decision, HarnessContext, HookKind
from ariadne.loop_module.verifier.command import CommandRunner
from ariadne.loop_module.verifier.restricted_exec import (
    CommandNotAllowedError,
    ExecResult,
)
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


@dataclass
class GuardedCommandRunner:
    """在 pre_tool 卡点求值后再执行命令。实现 CommandRunner Protocol。

    inner 是真实执行器（RestrictedRunner 或 SandboxRunner —— 两者都满足
    同一个 Protocol，所以这层对换沙箱是透明的）。
    """

    inner: CommandRunner
    evaluator: HarnessEvaluator
    audit_sink: AuditSink = field(default_factory=NullAuditSink)
    loop_state_provider: Callable[[], dict[str, object]] | None = None
    project_id: UUID | None = None
    loop_id: str = ""

    def run(self, cmd: str, *, workdir: Path) -> ExecResult:
        """求值 pre_tool 放行则执行，执行后求值 post_tool 再交还结果。"""
        ctx = HarnessContext(
            hook=HookKind.PRE_TOOL,
            tool={"cmd": cmd},
            loop=self._loop_context(),
        )
        decision = self.evaluator.evaluate(hook=HookKind.PRE_TOOL, context=ctx)
        self._audit(decision, ctx, HookKind.PRE_TOOL)

        if decision.action in (Action.BLOCK, Action.REQUIRE_APPROVAL):
            # REQUIRE_APPROVAL 在此等同 BLOCK：命令执行在 Verifier 的同步
            # 路径上，没有挂起等人工审批的地方。挂起点在 Loop 状态机的
            # APPROVAL_REQUIRED，与这里不是同一个生命周期。
            message = (
                decision.winning_hit.message
                if decision.winning_hit
                else "命令被 Harness 规则拦截"
            )
            rule_id = decision.winning_hit.rule.id if decision.winning_hit else ""
            logger.warning(
                "harness blocked command at pre_tool",
                extra={"loop_id": self.loop_id, "rule_id": rule_id},
            )
            raise CommandNotAllowedError(f"{message}（规则 {rule_id}）" if rule_id else message)

        if decision.action is Action.WARN:
            logger.warning(
                "harness warned at pre_tool",
                extra={
                    "loop_id": self.loop_id,
                    "rule_id": decision.winning_hit.rule.id if decision.winning_hit else "",
                },
            )
        elif decision.action in (Action.REWRITE, Action.ROUTE):
            # 命令串没有安全的改写语义 —— 把 `pytest` 改成别的东西等于静默
            # 执行了用户没要求的命令；ROUTE 更是无处可路由。放行并留痕，
            # 让规则作者看见自己写了个在这个卡点无效的动作。
            logger.warning(
                "pre_tool does not support %s, proceeding as allow",
                decision.action.value,
                extra={"loop_id": self.loop_id},
            )

        result = self.inner.run(cmd, workdir=workdir)
        return self._post_tool_check(cmd, result)

    def _post_tool_check(self, cmd: str, result: ExecResult) -> ExecResult:
        """命令执行后求值 post_tool。BLOCK 判**失败**而非 errored。

        与 pre_tool 的语义刻意相反：命令已经跑了，是它的**输出内容**违规
        （结果过大、泄露敏感数据等）—— 那是"被测输出不合格"，应让
        critique 驱动模型修输出；判 errored 会让模型去改断言配置，方向
        完全错。合成 exit_code=1 的 ExecResult 即可让断言失败（verifier
        只看 succeeded / combined_output）。
        """
        ctx = HarnessContext(
            hook=HookKind.POST_TOOL,
            tool={"cmd": cmd, "result": result.combined_output()},
            loop=self._loop_context(),
        )
        decision = self.evaluator.evaluate(hook=HookKind.POST_TOOL, context=ctx)
        self._audit(decision, ctx, HookKind.POST_TOOL)

        if decision.action in (Action.BLOCK, Action.REQUIRE_APPROVAL):
            message = (
                decision.winning_hit.message
                if decision.winning_hit
                else "工具结果被 Harness 规则拦截"
            )
            rule_id = decision.winning_hit.rule.id if decision.winning_hit else ""
            logger.warning(
                "harness blocked command result at post_tool",
                extra={"loop_id": self.loop_id, "rule_id": rule_id},
            )
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=f"{message}（规则 {rule_id}）" if rule_id else message,
                duration_ms=result.duration_ms,
            )

        if decision.action is Action.WARN:
            logger.warning(
                "harness warned at post_tool",
                extra={
                    "loop_id": self.loop_id,
                    "rule_id": decision.winning_hit.rule.id if decision.winning_hit else "",
                },
            )
        elif decision.action in (Action.REWRITE, Action.ROUTE):
            logger.warning(
                "post_tool does not support %s, proceeding as allow",
                decision.action.value,
                extra={"loop_id": self.loop_id},
            )

        return result

    def _loop_context(self) -> dict[str, object]:
        if self.loop_state_provider is None:
            return {}
        try:
            return self.loop_state_provider()
        except Exception:
            # 取不到 Loop 状态不该让命令执行失败。resource 类规则读的键会
            # 由 _normalize_loop 补成缺省，其前置守卫（budget_limit > 0）
            # 因此不触发。
            logger.warning("loop_state_provider failed at pre_tool", exc_info=True)
            return {}

    def _audit(
        self, decision: Decision, context: HarnessContext, hook: HookKind
    ) -> None:
        """写审计。同步路径里桥接异步 sink，失败不阻塞命令执行。

        默认 NullAuditSink 时直接跳过：它的 write 是 no-op，为此起线程跑
        事件循环纯属浪费（这条同步路径在 engine 的协程里被调用，只能靠
        独立线程跑 asyncio.run —— 见 SandboxRunner.run 的同一处权衡）。
        """
        if isinstance(self.audit_sink, NullAuditSink):
            return

        record = AuditRecord.create(
            project_id=self.project_id,
            hook=hook,
            action=decision.action,
            rule_hits=decision.hits,
            winning_hit=decision.winning_hit,
            context_snapshot={
                "hook": hook.value,
                "loop_id": self.loop_id,
                "cmd": str(context.tool.get("cmd", "")),
                "result_len": len(str(context.tool.get("result", ""))),
            },
            loop_id=self.loop_id,
        )
        write_audit_sync(self.audit_sink, record, logger=logger)




__all__ = [
    "GuardedCommandRunner",
]
