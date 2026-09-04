"""GuardedArtifactWriter —— 用 Harness 包装产出物落盘（pre_persist 卡点）。

pre_persist 规则在产出物写入工作目录**前**求值，能拦截敏感内容泄漏 / 过大输出
等问题。与 GuardedCommandRunner / GuardedLLMAdapter 同构：包装 Protocol、求值、
按 Decision 动作、写审计。

与已有防线的关系：

  第一层 FencedCodeWriter（artifact.py）：解析围栏代码块、路径校验（防路径穿越）、
  覆写确认。只管"在哪儿写、写不写"，看不见内容是什么。

  第二层 pre_persist 规则（本模块接入）：检查**内容本身**。
  - 敏感度评分（output-sensitive-high，敏感内容不应落盘）
  - 尺寸上限（output-huge，巨型输出可能是模型幻觉或攻击）
  - 租户可配置（规则来自 spec.yaml）
  - 审计（写 AuditRecord，包含规则 id、severity、winning hit）

pre_persist BLOCK 不调 inner.write，返回 WriteReport(ok=False, error=拦截理由)。
engine 的 _persist_artifacts 收到 ok=False → EXECUTION_FAILED → critique 携带
error 驱动模型修正内容。硬约束语义成立：内容不改规则持续命中，Loop 无法以违规
输出收敛。
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
from ariadne.loop_module.artifact import ArtifactWriter, WriteReport
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


def evaluate_pre_persist(
    *,
    evaluator: HarnessEvaluator,
    output_text: str,
    audit_sink: AuditSink,
    loop_state_provider: Callable[[], dict[str, object]] | None,
    project_id: UUID | None,
    loop_id: str,
) -> Decision:
    """对一次产出物/工具写入做 pre_persist 求值（公共卡点）。

    供 GuardedArtifactWriter 与 WorkspaceToolExecutor 共用 —— 两者都写工作
    目录，内容都必须过同一套 pre_persist 规则（output-sensitive-high /
    output-huge）。规则同时填 artifact 和 output 两个键：随包规则
    output-sensitive-high 读的是 output.text（output.yaml L36-40），不是
    artifact.text。按现状填，否则缺键 fail-closed 兜成命中。
    """
    ctx = HarnessContext(
        hook=HookKind.PRE_PERSIST,
        artifact={"text": output_text},
        output={"text": output_text},
        loop=_loop_context(loop_state_provider),
    )
    return evaluator.evaluate(hook=HookKind.PRE_PERSIST, context=ctx)


def _loop_context(
    loop_state_provider: Callable[[], dict[str, object]] | None,
) -> dict[str, object]:
    if loop_state_provider is None:
        return {}
    try:
        return loop_state_provider()
    except Exception:
        # 取不到 Loop 状态不该让落盘失败。resource 类规则读的键会由
        # _normalize_loop 补成缺省，其前置守卫（budget_limit > 0）
        # 因此不触发。
        logger.warning("loop_state_provider failed at pre_persist", exc_info=True)
        return {}


def audit_pre_persist_decision(
    *,
    decision: Decision,
    context: HarnessContext,
    audit_sink: AuditSink,
    project_id: UUID | None,
    loop_id: str,
    output_len: int,
) -> None:
    """写审计。context_snapshot **不记录输出文本本身**：规则因敏感内容命中，
    再把内容写进审计日志等于二次泄漏。只记元信息（hook、loop_id、长度）。
    """
    if isinstance(audit_sink, NullAuditSink):
        return

    record = AuditRecord.create(
        project_id=project_id,
        hook=HookKind.PRE_PERSIST,
        action=decision.action,
        rule_hits=decision.hits,
        winning_hit=decision.winning_hit,
        context_snapshot={
            "hook": HookKind.PRE_PERSIST.value,
            "loop_id": loop_id,
            "output_len": output_len,
        },
        loop_id=loop_id,
    )
    write_audit_sync(audit_sink, record, logger=logger)


@dataclass
class GuardedArtifactWriter:
    """在 pre_persist 卡点求值后再落盘产出物。实现 ArtifactWriter Protocol。

    inner 是真实写入器（FencedCodeWriter 或 NullArtifactWriter）。
    """

    inner: ArtifactWriter
    evaluator: HarnessEvaluator
    audit_sink: AuditSink = field(default_factory=NullAuditSink)
    loop_state_provider: Callable[[], dict[str, object]] | None = None
    project_id: UUID | None = None
    loop_id: str = ""

    def write(self, output: str, workdir: Path) -> WriteReport:
        """求值 pre_persist，放行则调 inner.write，拦截则返回错误报告。"""
        decision = evaluate_pre_persist(
            evaluator=self.evaluator,
            output_text=output,
            audit_sink=self.audit_sink,
            loop_state_provider=self.loop_state_provider,
            project_id=self.project_id,
            loop_id=self.loop_id,
        )
        ctx = HarnessContext(
            hook=HookKind.PRE_PERSIST,
            artifact={"text": output},
            output={"text": output},
            loop=_loop_context(self.loop_state_provider),
        )
        audit_pre_persist_decision(
            decision=decision,
            context=ctx,
            audit_sink=self.audit_sink,
            project_id=self.project_id,
            loop_id=self.loop_id,
            output_len=len(output),
        )

        if decision.action in (Action.BLOCK, Action.REQUIRE_APPROVAL):
            # REQUIRE_APPROVAL 等同 BLOCK：产出物落盘在 engine 的同步路径，
            # 没有挂起等审批的地方。BLOCK 返回 ok=False，engine 收到后
            # 转 EXECUTION_FAILED 并把 error 交给 critique 驱动模型修正。
            message = (
                decision.winning_hit.message
                if decision.winning_hit
                else "产出物被 Harness 规则拦截"
            )
            rule_id = decision.winning_hit.rule.id if decision.winning_hit else ""
            logger.warning(
                "harness blocked artifact at pre_persist",
                extra={"loop_id": self.loop_id, "rule_id": rule_id},
            )
            return WriteReport(
                error=f"被 Harness 规则拦截（{rule_id}）：{message}" if rule_id else message,
            )

        if decision.action is Action.WARN:
            logger.warning(
                "harness warned at pre_persist",
                extra={
                    "loop_id": self.loop_id,
                    "rule_id": decision.winning_hit.rule.id if decision.winning_hit else "",
                },
            )
        elif decision.action in (Action.REWRITE, Action.ROUTE):
            # 产出物落盘无安全改写语义 —— 静默改写内容等于欺骗测试结果。
            # ROUTE 同样无处可路由。放行并留痕，让规则作者看见自己写了个
            # 在这个卡点无效的动作。
            logger.warning(
                "pre_persist does not support %s, proceeding as allow",
                decision.action.value,
                extra={"loop_id": self.loop_id},
            )

        return self.inner.write(output, workdir)

    __all__ = (
    "GuardedArtifactWriter",
    "audit_pre_persist_decision",
    "evaluate_pre_persist",
)
