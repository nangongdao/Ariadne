"""Verifier 注册表。

registry 与 telemetry.adapters / eval_module 同构。同样用独立加载标志
而非"字典非空"——后者会让直接 import 子模块的代码把其余实现挡在外面
（M1/M2 各踩过一次）。
"""

from typing import TYPE_CHECKING, Any

from ariadne.loop_module.goal import AssertionKind
from ariadne.loop_module.verifier.base import (
    AssertionOutcome,
    BaseVerifier,
    Verdict,
    VerificationContext,
    compute_score,
    judge,
)

if TYPE_CHECKING:
    from collections.abc import Callable

VERIFIER_REGISTRY: dict[AssertionKind, type[BaseVerifier]] = {}
_loaded = False


def register_verifier(
    kind: AssertionKind,
) -> "Callable[[type[BaseVerifier]], type[BaseVerifier]]":
    def decorator(cls: type[BaseVerifier]) -> type[BaseVerifier]:
        VERIFIER_REGISTRY[kind] = cls
        return cls

    return decorator


def VerifierFactory(kind: AssertionKind, **kwargs: Any) -> BaseVerifier:  # noqa: N802
    """按断言类型取 Verifier。未注册类型显式报错。"""
    _ensure_loaded()
    if kind not in VERIFIER_REGISTRY:
        known = ", ".join(sorted(k.value for k in VERIFIER_REGISTRY))
        raise ValueError(f"未注册的断言类型 {kind.value!r}，已支持: {known}")
    return VERIFIER_REGISTRY[kind](**kwargs)


def available_kinds() -> list[AssertionKind]:
    _ensure_loaded()
    return sorted(VERIFIER_REGISTRY, key=lambda k: k.value)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from ariadne.loop_module.verifier import builtin, command  # noqa: F401


__all__ = [
    "VERIFIER_REGISTRY",
    "AssertionOutcome",
    "BaseVerifier",
    "Verdict",
    "VerificationContext",
    "VerifierFactory",
    "available_kinds",
    "compute_score",
    "judge",
    "register_verifier",
]
