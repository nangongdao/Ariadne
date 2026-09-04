"""Judge 调用与结构化输出解析。

可信度工程的六条措施（见 docs/05）在此落地其中四条：
1. 版本锁定 + temperature=0 + seed
2. 结构化输出（json_schema），不解析自由文本
3. 生成模型与 Judge 模型强制隔离
4. judge_model 记入结果（版本切换视为破坏性变更）

另两条（双向投票、κ 对齐）在 pairwise.py 与 kappa.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from ariadne.eval_module import register_evaluator
from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
    Severity,
    ThresholdOp,
    Violation,
    compare,
    truncate_evidence,
)
from ariadne.eval_module.judge.prompts import (
    JUDGE_OUTPUT_SCHEMA,
    system_prompt,
    user_prompt,
)
from ariadne.telemetry.models import TokenUsage

MAX_VIOLATIONS = 20


class JudgeClient(Protocol):
    """Judge 的模型调用接口。

    抽象成 Protocol 而非绑定具体 provider：单元测试可注入桩，
    且 M3 接入 Loop 时可复用 Runtime 层的 LLM Adapter。
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        temperature: float,
        seed: int | None,
    ) -> JudgeResponse: ...


@dataclass(frozen=True)
class JudgeResponse:
    """模型返回。content 应是符合 schema 的 JSON 文本。"""

    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: Decimal = Decimal("0")


@dataclass(frozen=True)
class JudgeConfig:
    """Judge 配置。

    默认值全部指向确定性：temperature=0、固定 seed、结构化输出。
    """

    model: str
    dimension: str
    threshold: float = 85.0
    op: ThresholdOp = ThresholdOp.GTE
    temperature: float = 0.0
    seed: int | None = 42
    # 生成侧模型。设置后会校验隔离
    generation_model: str = ""


class JudgeIsolationError(RuntimeError):
    """生成模型与 Judge 模型相同（或同族）时抛出。

    自评会系统性高估 —— 这是 Ralph 原则在评测层的延伸。
    """


def _model_family(model: str) -> str:
    """粗粒度模型族识别。

    gpt-4o / gpt-4o-mini 属同族；claude-sonnet-5 / claude-haiku-4-5 同族。
    同族不同尺寸也算违规，因为它们共享训练数据与偏好。
    """
    lowered = model.lower()
    for family in ("gpt", "claude", "gemini", "qwen", "deepseek", "llama", "mistral"):
        if family in lowered:
            return family
    return lowered.split("-")[0] if "-" in lowered else lowered


def enforce_isolation(judge_model: str, generation_model: str) -> None:
    """在**配置加载时**校验，不等评测时才发现。"""
    if not generation_model:
        return
    if judge_model == generation_model:
        raise JudgeIsolationError(
            f"Judge 模型与生成模型相同（{judge_model}）。自评会系统性高估，"
            "请指定不同的 Judge 模型。"
        )
    if _model_family(judge_model) == _model_family(generation_model):
        raise JudgeIsolationError(
            f"Judge 模型 {judge_model} 与生成模型 {generation_model} 属同族"
            f"（{_model_family(judge_model)}）。同族模型共享偏好，仍会高估。"
        )


def parse_judge_output(content: str) -> tuple[float, str, tuple[Violation, ...], str]:
    """解析 Judge 的结构化输出。

    返回 (score, reasoning, violations, error)。error 非空表示解析失败 ——
    此时调用方应记 errored 而非当 0 分，避免把"没测出来"当"很差"。
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        text = "\n".join(body).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return 0.0, "", (), f"Judge 输出非合法 JSON: {exc.msg}"

    if not isinstance(payload, dict):
        return 0.0, "", (), f"Judge 输出不是对象而是 {type(payload).__name__}"

    raw_score = payload.get("score")
    if not isinstance(raw_score, (int, float)):
        return 0.0, "", (), f"score 字段缺失或非数值: {raw_score!r}"

    score = float(min(max(raw_score, 0), 100))
    reasoning = str(payload.get("reasoning", ""))

    violations: list[Violation] = []
    for item in (payload.get("violations") or [])[:MAX_VIOLATIONS]:
        if not isinstance(item, dict):
            continue
        raw_severity = str(item.get("severity", "medium")).lower()
        severity = (
            Severity(raw_severity)
            if raw_severity in {s.value for s in Severity}
            else Severity.MEDIUM
        )
        violations.append(
            Violation(
                dimension="",  # 由调用方填入，避免模型自己编维度名
                span=str(item.get("span", ""))[:300],
                severity=severity,
                detail=str(item.get("detail", ""))[:500],
            )
        )

    return score, reasoning, tuple(violations), ""


@register_evaluator("judge")
class JudgeEvaluator(BaseEvaluator):
    """LLM-as-Judge 评估器。

    可信度 ★★，覆盖面最广。用于无法用规则表达的维度。
    必须配合 κ 对齐（见 kappa.py）才能作为 blocking 断言依据。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.JUDGE

    def __init__(
        self,
        name: str = "judge",
        *,
        client: JudgeClient,
        config: JudgeConfig,
    ) -> None:
        super().__init__(name)
        # 构造时校验隔离与维度合法性，不等评测才失败
        enforce_isolation(config.model, config.generation_model)
        self._system = system_prompt(config.dimension)
        self._client = client
        self._config = config

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, 100.0)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        prompt = user_prompt(
            task=ctx.input, output=ctx.output, expected=ctx.expected
        )
        response = self._client.complete(
            system=self._system,
            user=prompt,
            schema=JUDGE_OUTPUT_SCHEMA,
            model=self._config.model,
            temperature=self._config.temperature,
            seed=self._config.seed,
        )

        score, reasoning, violations, error = parse_judge_output(response.content)
        if error:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence=truncate_evidence(f"{error}\n原始输出: {response.content[:500]}"),
                judge_model=response.model,
                cost_usd=response.cost_usd,
                errored=True,
            )

        # 维度由配置决定而非模型自报，避免模型编造维度名
        tagged = tuple(
            v.model_copy(update={"dimension": self._config.dimension})
            for v in violations
        )
        passed = compare(score, self._config.op, self._config.threshold)

        return EvalResult(
            name=self.name,
            value=score,
            passed=passed,
            evidence=""
            if passed
            else truncate_evidence(
                f"得分 {score:.0f} 不满足 {self._config.op.value} "
                f"{self._config.threshold}\n{reasoning}"
            ),
            violations=tagged,
            judge_model=response.model,
            cost_usd=response.cost_usd,
        )
