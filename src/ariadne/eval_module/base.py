"""评测契约。

设计原则（见 docs/05）：能用确定性评估器解决的，绝不用 LLM Judge。
三类评估器按"可信度递减、覆盖面递增"排列，Judge 是最后手段。

核心契约：evaluate() 不得抛异常。单个评估器失败不能中断整个实验，
失败时返回 passed=False 且 evidence 说明原因。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_EVIDENCE_CHARS = 2000
EVIDENCE_HEAD_LINES = 20
EVIDENCE_TAIL_LINES = 5


class EvaluatorKind(StrEnum):
    """可信度递减顺序。用于在配置校验时警告"能用确定性却用了 Judge"。"""

    DETERMINISTIC = "deterministic"
    STATISTICAL = "statistical"
    JUDGE = "judge"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Violation(BaseModel):
    """Judge 定位到的具体问题。

    span 字段是关键：只给总分的 Judge 对 Loop 毫无帮助 —— M3 的
    Critique Synthesizer 需要它生成定向修正指令。
    """

    model_config = ConfigDict(frozen=True)

    dimension: str
    span: str = ""
    severity: Severity = Severity.MEDIUM
    detail: str = ""


class EvalContext(BaseModel):
    """评估器的输入。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    input: str = ""
    output: str = ""
    expected: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    # COMMAND 类评估器的工作目录（M4 前由受限子进程使用）
    artifact_path: Path | None = None


class EvalResult(BaseModel):
    """评估结果。

    value 的量纲由评估器自己声明（见 BaseEvaluator.value_range），
    不强制统一到 0-100 —— 相似度天然是 0-1，通过率天然是 0/1，
    强行缩放只会让阈值配置更难理解。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: float
    passed: bool
    evidence: str = ""
    violations: tuple[Violation, ...] = ()
    judge_model: str = ""
    duration_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    # 评估器自身出错（非"评测未通过"）时为 True，用于区分
    # "输出确实不合格"与"我们没测出来"
    errored: bool = False


def truncate_evidence(
    text: str, head: int = EVIDENCE_HEAD_LINES, tail: int = EVIDENCE_TAIL_LINES
) -> str:
    """证据必须截断。

    一个完整的 stack trace 能吃掉整个上下文预算 —— M3 的 critique
    要把证据喂回模型，长度失控会直接推高每轮成本。
    """
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text[:MAX_EVIDENCE_CHARS]

    omitted = len(lines) - head - tail
    kept = [*lines[:head], f"… 省略 {omitted} 行 …", *lines[-tail:]]
    return "\n".join(kept)[:MAX_EVIDENCE_CHARS]


class BaseEvaluator(ABC):
    """所有评估器的基类。

    子类只需实现 _evaluate()；异常兜底由 evaluate() 统一处理，
    保证"单个评估器失败不中断实验"这条契约不依赖子类自觉。
    """

    kind: ClassVar[EvaluatorKind]
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def value_range(self) -> tuple[float, float]:
        """value 的取值区间，供前端画图与阈值校验使用。"""
        return (0.0, 1.0)

    @abstractmethod
    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        """子类实现。允许抛异常，由 evaluate() 兜底。"""

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        """带异常兜底的求值入口。实验编排只调这个方法。"""
        import time

        start = time.perf_counter()
        try:
            result = self._evaluate(ctx)
        except Exception as exc:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence=f"{type(exc).__name__}: {exc}"[:MAX_EVIDENCE_CHARS],
                errored=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        if result.duration_ms == 0:
            elapsed = int((time.perf_counter() - start) * 1000)
            return result.model_copy(update={"duration_ms": elapsed})
        return result


class ThresholdOp(StrEnum):
    GTE = ">="
    GT = ">"
    LTE = "<="
    LT = "<"
    EQ = "=="
    NEQ = "!="


def compare(value: float, op: ThresholdOp, threshold: float) -> bool:
    """阈值比较。集中一处避免各评估器各写一遍。"""
    match op:
        case ThresholdOp.GTE:
            return value >= threshold
        case ThresholdOp.GT:
            return value > threshold
        case ThresholdOp.LTE:
            return value <= threshold
        case ThresholdOp.LT:
            return value < threshold
        case ThresholdOp.EQ:
            return value == threshold
        case ThresholdOp.NEQ:
            return value != threshold


Dimension = Literal[
    "factuality", "ifr", "helpfulness", "safety", "tone", "coherence"
]
