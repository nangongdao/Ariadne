"""四类非命令型 Verifier：SCHEMA / REGEX / METRIC / HUMAN。

COMMAND 类需要执行环境，单独放 command.py。
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Protocol

from ariadne.eval_module.base import ThresholdOp, compare, truncate_evidence
from ariadne.loop_module.goal import Assertion, AssertionKind
from ariadne.loop_module.verifier import register_verifier
from ariadne.loop_module.verifier.base import (
    AssertionOutcome,
    BaseVerifier,
    VerificationContext,
)

MAX_PATTERN_INPUT = 200_000


@register_verifier(AssertionKind.SCHEMA)
class SchemaVerifier(BaseVerifier):
    """JSON Schema 校验。信号强度 ★★★★★：二值、无歧义、零成本。"""

    kind: ClassVar[AssertionKind] = AssertionKind.SCHEMA

    def _verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        from ariadne.eval_module import EvaluatorFactory
        from ariadne.eval_module.base import EvalContext

        schema = assertion.spec.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("schema 字段必须是对象")

        # 复用 M2 的评估器：同一份实现，避免两处逻辑漂移
        evaluator = EvaluatorFactory("json_schema", schema=schema)
        result = evaluator.evaluate(
            EvalContext(item_id=assertion.id, output=ctx.output)
        )
        return AssertionOutcome(
            assertion_id=assertion.id,
            kind=assertion.kind,
            passed=result.passed,
            value=1.0 if result.passed else 0.0,
            evidence=result.evidence,
            errored=result.errored,
        )


@register_verifier(AssertionKind.REGEX)
class RegexVerifier(BaseVerifier):
    """正则匹配。must_match=False 表示"必须不出现"。"""

    kind: ClassVar[AssertionKind] = AssertionKind.REGEX

    def _verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        pattern_text = str(assertion.spec.get("pattern", ""))
        must_match = bool(assertion.spec.get("must_match", True))
        flags = re.MULTILINE if assertion.spec.get("multiline", True) else 0

        pattern = re.compile(pattern_text, flags)
        found = pattern.search(ctx.output[:MAX_PATTERN_INPUT])
        passed = bool(found) if must_match else not found

        if passed:
            evidence = ""
        elif must_match:
            evidence = f"未匹配到 /{pattern_text}/"
        else:
            assert found is not None
            evidence = f"匹配到不应出现的内容: {found.group(0)[:200]!r}"

        return AssertionOutcome(
            assertion_id=assertion.id,
            kind=assertion.kind,
            passed=passed,
            value=1.0 if passed else 0.0,
            evidence=truncate_evidence(evidence),
        )


class MetricProvider(Protocol):
    """指标来源。

    抽象成 Protocol：M3 阶段由 Loop 引擎把 M2 的评测结果注入，
    单元测试可用字典桩，不必真跑评估器。
    """

    def metric(self, name: str, ctx: VerificationContext) -> float | None: ...


class DictMetricProvider:
    """字典桩。用于测试与"评测结果已算好"的场景。"""

    def __init__(self, values: dict[str, float]) -> None:
        self._values = values

    def metric(self, name: str, ctx: VerificationContext) -> float | None:
        return self._values.get(name)


class EvaluatorMetricProvider:
    """从 M2 评估器实时求值。

    构造时接收评估器名到实例的映射 —— Loop 引擎负责按项目配置装配，
    Verifier 不关心评估器怎么来的。
    """

    def __init__(self, evaluators: dict[str, Any]) -> None:
        self._evaluators = evaluators

    def metric(self, name: str, ctx: VerificationContext) -> float | None:
        evaluator = self._evaluators.get(name)
        if evaluator is None:
            return None

        from ariadne.eval_module.base import EvalContext

        result = evaluator.evaluate(
            EvalContext(
                item_id=name,
                input=ctx.task,
                output=ctx.output,
                expected=ctx.expected,
            )
        )
        # 出错时返回 None，让 Verifier 记 errored 而非当 0 分
        return None if result.errored else float(result.value)


@register_verifier(AssertionKind.METRIC)
class MetricVerifier(BaseVerifier):
    """数值指标比较。信号强度 ★★★：依赖评估器，可能有噪声。

    这是唯一需要外部依赖的非命令型 Verifier，因此 provider 由构造注入。
    """

    kind: ClassVar[AssertionKind] = AssertionKind.METRIC

    def __init__(self, provider: MetricProvider) -> None:
        self._provider = provider

    def _verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        name = str(assertion.spec.get("name", ""))
        raw_op = str(assertion.spec.get("op", ">="))
        threshold = float(assertion.spec.get("value", 0))

        if raw_op not in {o.value for o in ThresholdOp}:
            raise ValueError(f"非法比较符: {raw_op}")

        value = self._provider.metric(name, ctx)
        if value is None:
            # 指标拿不到是配置或评估器问题，记 errored 而非判失败 ——
            # 后者会让 critique 给出"提升该指标"的无效指令
            return AssertionOutcome(
                assertion_id=assertion.id,
                kind=assertion.kind,
                passed=False,
                evidence=f"指标 {name!r} 不可用（未配置或评估器出错）",
                errored=True,
            )

        passed = compare(value, ThresholdOp(raw_op), threshold)
        # 归一化：以阈值为基准，超出即满分
        normalized = 1.0 if passed else max(min(value / threshold, 1.0), 0.0) if threshold else 0.0

        return AssertionOutcome(
            assertion_id=assertion.id,
            kind=assertion.kind,
            passed=passed,
            value=normalized,
            evidence=""
            if passed
            else f"{name} = {value:.4g}，不满足 {raw_op} {threshold:g}",
        )


class ApprovalStore(Protocol):
    """人工审批状态来源。"""

    def decision(self, assertion_id: str) -> bool | None: ...


class InMemoryApprovals:
    def __init__(self, decisions: dict[str, bool] | None = None) -> None:
        self._decisions = dict(decisions or {})

    def approve(self, assertion_id: str) -> None:
        self._decisions[assertion_id] = True

    def reject(self, assertion_id: str) -> None:
        self._decisions[assertion_id] = False

    def decision(self, assertion_id: str) -> bool | None:
        return self._decisions.get(assertion_id)


@register_verifier(AssertionKind.HUMAN)
class HumanVerifier(BaseVerifier):
    """人工审批。未决时返回 pending_human，Loop 转 HUMAN_PENDING。

    信号强度 ★★★★★：人的判断是最可靠的，代价是慢。
    """

    kind: ClassVar[AssertionKind] = AssertionKind.HUMAN

    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    def _verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        decision = self._store.decision(assertion.id)

        if decision is None:
            return AssertionOutcome(
                assertion_id=assertion.id,
                kind=assertion.kind,
                passed=False,
                pending_human=True,
                evidence="等待人工审批",
            )

        return AssertionOutcome(
            assertion_id=assertion.id,
            kind=assertion.kind,
            passed=decision,
            value=1.0 if decision else 0.0,
            evidence="" if decision else "人工审批未通过",
        )
