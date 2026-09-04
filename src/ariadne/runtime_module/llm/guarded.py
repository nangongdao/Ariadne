"""GuardedLLMAdapter —— 用 Harness 包装 LLM 调用。

在 LLM 调用前后插入 Harness 规则求值（docs/M4 §4）：
- pre_model：检查输入（PII、注入、长度等），可 block/rewrite/route
- post_model：检查输出（引用、敏感信息、JSON 等），可 block/warn
- 审计：每次求值写 AuditRecord

BLOCK → 抛 HarnessBlockError，engine 捕获后映射为 RULES_BLOCKED → BLOCKED 终态。
REWRITE → 改写 prompt 后调 inner LLM。
ROUTE → 换 model 名（但不在此处改 Loop 的 model_for 逻辑，只改传给 inner 的 model 名）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ariadne.harness_module.audit import AuditRecord, AuditSink, NullAuditSink
from ariadne.harness_module.evaluator import HarnessEvaluator
from ariadne.harness_module.models import (
    Action,
    Decision,
    HarnessContext,
    HookKind,
)
from ariadne.loop_module.engine import LLMClient, LLMResponse
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


class HarnessBlockError(Exception):
    """Harness 规则判定 BLOCK。

    Engine 捕获此异常，映射为 LoopEvent.RULES_BLOCKED → BLOCKED 终态。
    不通过多轮重试绕过 —— 硬约束。
    """

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        msg = decision.winning_hit.message if decision.winning_hit else "blocked by harness"
        super().__init__(f"harness blocked: {msg}")


@dataclass
class GuardedLLMAdapter:
    """用 Harness 包装的 LLM 客户端。

    实现 LLMClient Protocol（async def complete(prompt, *, model) -> LLMResponse）。
    inner 是真实的 provider adapter（如 AnthropicLLMClient）。
    evaluator 是编译后的 HarnessEvaluator（规则集）。
    audit_sink 记录每次求值的 Decision。
    """

    inner: LLMClient
    evaluator: HarnessEvaluator
    audit_sink: AuditSink = field(default_factory=NullAuditSink)
    # Loop 状态回调：提供预算/轮次信息给 resource 类规则
    loop_state_provider: Any = None  # Callable[[], dict[str, Any]] | None
    # M6：审计需要 project_id + loop_id（AuditLogRow.project_id 是 NOT NULL）
    project_id: UUID | None = None
    loop_id: str = ""

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        """带 Harness 前后求值的 LLM 调用。"""
        loop_ctx = self._loop_context()

        # ---- pre_model 求值 ----
        pre_ctx = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": prompt},
            loop=loop_ctx,
        )
        pre_decision = self.evaluator.evaluate(
            hook=HookKind.PRE_MODEL, context=pre_ctx
        )
        await self._audit(HookKind.PRE_MODEL, pre_decision, pre_ctx)

        if pre_decision.action is Action.BLOCK:
            raise HarnessBlockError(pre_decision)

        # REWRITE：改写 prompt
        actual_prompt = prompt
        if pre_decision.action is Action.REWRITE:
            actual_prompt = self._apply_rewrite(pre_decision, prompt)

        # ROUTE：换 model 名
        actual_model = model
        if pre_decision.action is Action.ROUTE:
            actual_model = pre_decision.route_target or model

        # ---- 调 inner LLM ----
        response = await self.inner.complete(actual_prompt, model=actual_model)

        # ---- post_model 求值 ----
        post_ctx = HarnessContext(
            hook=HookKind.POST_MODEL,
            output={"text": response.output},
            usage={
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
            cost={"usd": float(response.cost_usd)},
            loop=loop_ctx,
        )
        post_decision = self.evaluator.evaluate(
            hook=HookKind.POST_MODEL, context=post_ctx
        )
        await self._audit(HookKind.POST_MODEL, post_decision, post_ctx)

        if post_decision.action is Action.BLOCK:
            raise HarnessBlockError(post_decision)

        # post_model REWRITE：改写输出
        actual_output = response.output
        if post_decision.action is Action.REWRITE:
            actual_output = self._apply_rewrite(post_decision, response.output)
            response = LLMResponse(
                output=actual_output,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                claimed_done=response.claimed_done,
                model=response.model,
                cost_usd=response.cost_usd,
            )

        return response

    def _loop_context(self) -> dict[str, Any]:
        """从 loop_state_provider 获取 Loop 状态（预算、轮次等）。"""
        if self.loop_state_provider is None:
            return {}
        try:
            provider = self.loop_state_provider
            return provider()  # type: ignore[no-any-return]
        except Exception:
            return {}

    def _apply_rewrite(self, decision: Decision, text: str) -> str:
        """应用 rewrite 策略。

        内置策略：redact_pii（脱敏 PII）、sanitize（移除注入模式）。
        自定义策略由调用方在 loop_state_provider 中注册（M4 阶段先做内置）。
        """
        strategy = decision.rewrite_strategy or (
            decision.winning_hit.rule.rewrite_strategy
            if decision.winning_hit
            else ""
        )
        if strategy == "redact_pii":
            from ariadne.harness_module.functions import detect_pii

            pii_types = detect_pii(text)
            result = text
            # 简单脱敏：把检测到的 PII 段替换为 [REDACTED]
            import re

            for pii_type in pii_types:
                if pii_type == "email":
                    result = re.sub(
                        r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "[REDACTED]", result
                    )
                elif pii_type == "phone":
                    result = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[REDACTED]", result)
                elif pii_type == "ssn":
                    result = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED]", result)
            return result
        if strategy == "sanitize":
            # 移除常见 prompt injection 模式
            import re

            return re.sub(
                r"(?i)(ignore\s+(previous|prior|above)\s+(instructions?|prompts?))",
                "[FILTERED]",
                text,
            )
        # 未知策略：不改写（fail-open for rewrite，与 block 的 fail-closed 相反）
        logger.warning("unknown rewrite strategy %s, skipping", strategy)
        return text

    async def _audit(
        self,
        hook: HookKind,
        decision: Decision,
        context: HarnessContext,
    ) -> None:
        """写审计记录。审计失败不阻塞主流程。"""
        import contextlib

        record = AuditRecord.create(
            project_id=self.project_id,
            hook=hook,
            action=decision.action,
            rule_hits=decision.hits,
            winning_hit=decision.winning_hit,
            context_snapshot={
                "hook": hook.value,
                "loop_id": self.loop_id,
            },
            loop_id=self.loop_id,
        )
        with contextlib.suppress(Exception):
            await self.audit_sink.write(record)


__all__ = [
    "GuardedLLMAdapter",
    "HarnessBlockError",
]
