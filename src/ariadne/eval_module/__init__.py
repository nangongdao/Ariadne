"""评测引擎：三类评估器 + 复合评分 + SLI。

registry + factory 与 telemetry.adapters 同构。注意 _ensure_loaded 用独立
标志而非"字典非空" —— 任何代码直接 import 某个评估器模块都会让字典变
非空，从而让"非空即已加载"的判断提前返回（M1 踩过这个坑）。
"""

from typing import TYPE_CHECKING, Any

from ariadne.eval_module.base import (
    BaseEvaluator,
    Dimension,
    EvalContext,
    EvalResult,
    EvaluatorKind,
    Severity,
    ThresholdOp,
    Violation,
    compare,
    truncate_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable

EVALUATOR_REGISTRY: dict[str, type[BaseEvaluator]] = {}
_loaded = False


def register_evaluator(
    name: str,
) -> "Callable[[type[BaseEvaluator]], type[BaseEvaluator]]":
    def decorator(cls: type[BaseEvaluator]) -> type[BaseEvaluator]:
        EVALUATOR_REGISTRY[name] = cls
        return cls

    return decorator


def EvaluatorFactory(etype: str, /, **kwargs: Any) -> BaseEvaluator:  # noqa: N802
    """按注册名取评估器实例。未知名称显式报错，不静默回退。

    第一个参数是**位置限定**（`/`）且叫 etype 而非 name：几乎每个评估器的
    构造器都有一个 `name`（实例名，会落到 EvalResult.name 和 metrics 里），
    与注册名不是一回事。参数名重合时 `EvaluatorFactory("regex", name="has_todo")`
    会撞 "got multiple values for argument 'name'" —— 位置限定让这个撞不了。
    """
    _ensure_loaded()
    if etype not in EVALUATOR_REGISTRY:
        known = ", ".join(sorted(EVALUATOR_REGISTRY))
        raise ValueError(f"未知的评估器 {etype!r}，已注册: {known}")
    return EVALUATOR_REGISTRY[etype](**kwargs)


def available_evaluators() -> list[str]:
    _ensure_loaded()
    return sorted(EVALUATOR_REGISTRY)


def evaluators_by_kind(kind: EvaluatorKind) -> list[str]:
    _ensure_loaded()
    return sorted(
        name for name, cls in EVALUATOR_REGISTRY.items() if cls.kind is kind
    )


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from ariadne.eval_module.deterministic import (  # noqa: F401
        numeric,
        regex,
        schema,
    )
    from ariadne.eval_module.judge import runner  # noqa: F401
    from ariadne.eval_module.statistical import overlap  # noqa: F401


__all__ = [
    "EVALUATOR_REGISTRY",
    "BaseEvaluator",
    "Dimension",
    "EvalContext",
    "EvalResult",
    "EvaluatorFactory",
    "EvaluatorKind",
    "Severity",
    "ThresholdOp",
    "Violation",
    "available_evaluators",
    "compare",
    "evaluators_by_kind",
    "register_evaluator",
    "truncate_evidence",
]
