"""Ralph Verifier —— 外部强制验证。

借鉴 Claude Code 社区 2025-2026 年形成的 Ralph Loop 范式：
**不信任模型的自我评估，由外部机制强制判断任务是否真正完成。**

三条硬性规则：

1. 收敛判定只看 blocking 断言是否全过。score 只用于趋势图与 STALLED
   检测，绝不作为收敛依据 —— 否则又回到"分数可被讨好"的老问题。
2. claimed_done 与 converged 分开记录。两者的差集就是"假完成"，
   这是产品的关键指标（目标拦截率 ≥ 95%）。
3. Verifier 的执行环境与被验证对象隔离。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ariadne.eval_module.base import truncate_evidence
from ariadne.loop_module.goal import Assertion, AssertionKind, Goal


@dataclass(frozen=True)
class VerificationContext:
    """验证输入。"""

    output: str
    task: str = ""
    expected: str | None = None
    # COMMAND 类断言的工作目录
    artifact_path: Path | None = None
    # 模型是否自称完成。**仅用于统计假完成率，不参与收敛判定**
    claimed_done: bool = False


@dataclass(frozen=True)
class AssertionOutcome:
    """单条断言的验证结果。"""

    assertion_id: str
    kind: AssertionKind
    passed: bool
    # 归一化到 0-1 的原始值，用于加权得分
    value: float = 0.0
    evidence: str = ""
    # 该断言是否需要人工介入（HUMAN 类）
    pending_human: bool = False
    # verifier 自身出错（与"断言未通过"区分）
    errored: bool = False
    duration_ms: int = 0

    @property
    def hint_worthy(self) -> bool:
        """是否值得在 critique 中给出定向提示。"""
        return not self.passed and not self.errored


@dataclass(frozen=True)
class Verdict:
    """裁决。收敛判定的唯一依据。"""

    converged: bool
    passed: tuple[str, ...] = ()
    failed: tuple[AssertionOutcome, ...] = ()
    # 加权得分。仅用于趋势与 STALLED 检测，**不用于收敛判定**
    score: float = 0.0
    # 模型自称完成（仅记录）
    claimed_done: bool = False
    pending_human: tuple[str, ...] = ()
    errored: tuple[str, ...] = ()
    outcomes: tuple[AssertionOutcome, ...] = field(default_factory=tuple)

    @property
    def false_completion(self) -> bool:
        """假完成：模型自称完成但断言未过。

        这是 Ralph 原则要解决的核心问题 —— 早期 Agent 最致命的缺陷是
        "模型说完成了就停了，但任务远未达标"。
        """
        return self.claimed_done and not self.converged

    @property
    def needs_human(self) -> bool:
        return bool(self.pending_human)

    @property
    def failed_ids(self) -> tuple[str, ...]:
        return tuple(o.assertion_id for o in self.failed)

    def outcome_for(self, assertion_id: str) -> AssertionOutcome | None:
        return next(
            (o for o in self.outcomes if o.assertion_id == assertion_id), None
        )


class BaseVerifier(ABC):
    """单类断言的验证器。

    契约：verify() 不得抛异常。单条断言的验证失败不能让整轮裁决崩掉 ——
    那会让 Loop 进 FAILED 而非给出可修正的反馈。
    """

    kind: ClassVar[AssertionKind]

    @abstractmethod
    def _verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        """子类实现。允许抛异常，由 verify() 兜底。"""

    def verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        import time

        start = time.perf_counter()
        try:
            outcome = self._verify(assertion, ctx)
        except Exception as exc:
            return AssertionOutcome(
                assertion_id=assertion.id,
                kind=assertion.kind,
                passed=False,
                evidence=truncate_evidence(f"{type(exc).__name__}: {exc}"),
                errored=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        if outcome.duration_ms == 0:
            elapsed = int((time.perf_counter() - start) * 1000)
            return AssertionOutcome(**{**outcome.__dict__, "duration_ms": elapsed})
        return outcome


def compute_score(
    outcomes: tuple[AssertionOutcome, ...], goal: Goal
) -> float:
    """加权得分，0-100。

    **不用于收敛判定** —— 只喂给趋势图与收益递减检测。
    出错的断言不计入分母：否则"没测出来"会被当成"很差"，
    让 Loop 朝错误方向修正。
    """
    total_weight = 0.0
    weighted = 0.0

    for outcome in outcomes:
        if outcome.errored:
            continue
        assertion = goal.assertion_by_id(outcome.assertion_id)
        weight = assertion.weight if assertion else 1.0
        total_weight += weight
        weighted += outcome.value * weight

    if total_weight == 0:
        return 0.0
    return round(weighted / total_weight * 100.0, 2)


def judge(
    outcomes: tuple[AssertionOutcome, ...],
    goal: Goal,
    *,
    claimed_done: bool = False,
) -> Verdict:
    """从断言结果构造裁决。

    这是 Ralph 原则的落点：converged 只看 blocking 断言是否全过，
    与 claimed_done 完全无关。
    """
    passed_ids = tuple(o.assertion_id for o in outcomes if o.passed)
    failed = tuple(o for o in outcomes if not o.passed and not o.pending_human)
    pending = tuple(o.assertion_id for o in outcomes if o.pending_human)
    errored = tuple(o.assertion_id for o in outcomes if o.errored)

    # 收敛条件：全部 blocking 断言都在 passed 集合里。
    # 出错的断言不算通过 —— "没测出来"不能当"通过"，那是掩盖故障。
    converged = goal.blocking_ids.issubset(set(passed_ids))

    return Verdict(
        converged=converged,
        passed=passed_ids,
        failed=failed,
        score=compute_score(outcomes, goal),
        claimed_done=claimed_done,
        pending_human=pending,
        errored=errored,
        outcomes=outcomes,
    )
