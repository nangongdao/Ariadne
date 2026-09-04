"""振荡与停滞检测。

Loop 的第二大失效模式不是"改不好"，而是**原地打转**：第 3 轮改回了
第 1 轮的错误。检测基于两个指纹：

- output_fp：归一化后的输出哈希 —— 检测"又输出了同样的东西"
- failure_fp：失败断言集合的签名 —— 检测"又犯了同样的错"

处置按严重度递增：升级策略 → 判 STALLED。

STALLED 是一个**有价值的失败**：它明确告诉用户"你的断言设计给不出
有效反馈信号"，而不是默默烧完 10 轮预算。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

FP_LENGTH = 16

# 归一化：折叠空白、去掉行尾空格。
# 不做更激进的归一化（如去标点）—— 那会把"只改了标点"的真实改动误判为无变化
_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class OscillationVerdict(StrEnum):
    NONE = "none"
    # 连续 2 轮同一失败签名 → 升级策略
    ESCALATE = "escalate"
    # 输出重复出现过 → 明确的振荡
    OSCILLATING = "oscillating"
    # 连续 3 轮同一签名 或 收益递减 → 终止
    STALLED = "stalled"

    @property
    def should_terminate(self) -> bool:
        return self is OscillationVerdict.STALLED

    @property
    def should_escalate(self) -> bool:
        return self in (OscillationVerdict.ESCALATE, OscillationVerdict.OSCILLATING)


def normalize_output(text: str) -> str:
    """归一化输出以便比较。

    只折叠空白：模型在语义相同时常有细微空白差异，
    但去标点/去大小写会掩盖真实改动。
    """
    lines = [_WHITESPACE.sub(" ", line).rstrip() for line in text.strip().splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines))


def output_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_output(text).encode("utf-8")).hexdigest()[
        :FP_LENGTH
    ]


def failure_fingerprint(failed_ids: Iterable[str]) -> str:
    """失败断言集合的签名。

    排序后哈希：断言求值顺序不该影响签名，否则同一组失败会产生
    不同签名，振荡检测完全失效。
    """
    joined = "|".join(sorted(failed_ids))
    if not joined:
        return ""
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:FP_LENGTH]


@dataclass(frozen=True)
class IterationTrace:
    """单轮的检测输入。"""

    iteration: int
    output_fp: str
    failure_fp: str
    score: float
    failed_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OscillationReport:
    verdict: OscillationVerdict
    reason: str = ""
    # 连续同签名的轮数
    repeat_count: int = 0
    # 与之重复的历史轮次（振荡时非空）
    duplicate_of: int | None = None
    # 建议在 critique 的 forbidden 中列出的历史尝试摘要
    forbid_hints: tuple[str, ...] = ()


@dataclass
class OscillationDetector:
    """振荡检测器。

    escalate_after / stall_after 的默认值来自 docs/03：
    连续 2 轮同签名升级策略，连续 3 轮判 STALLED。
    """

    stall_threshold: float = 2.0
    stall_patience: int = 2
    escalate_after: int = 2
    stall_after: int = 3

    _history: list[IterationTrace] = field(default_factory=list)

    def record(self, trace: IterationTrace) -> OscillationReport:
        """记录一轮并给出判定。"""
        previous = list(self._history)
        self._history.append(trace)

        # 输出重复：最明确的振荡信号。
        # 出现 1-2 次先升级策略；反复重现同一输出（>= stall_after）说明
        # 模型完全卡死，判 STALLED 而非一直升级到烧满预算。
        duplicate_count = sum(1 for h in previous if h.output_fp == trace.output_fp)
        if duplicate_count >= self.stall_after:
            return OscillationReport(
                verdict=OscillationVerdict.STALLED,
                reason=(
                    f"输出已连续重复 {duplicate_count} 次，模型完全卡死。"
                    "反馈信号无法驱动有效修正"
                ),
                repeat_count=duplicate_count,
                forbid_hints=self._forbid_hints(),
            )
        if duplicate_count > 0:
            return OscillationReport(
                verdict=OscillationVerdict.OSCILLATING,
                reason="输出与之前轮次完全相同，已在原地打转",
                duplicate_of=next(
                    h.iteration for h in previous if h.output_fp == trace.output_fp
                ),
                forbid_hints=self._forbid_hints(),
            )

        repeat = self._consecutive_failure_repeats()
        if trace.failure_fp and repeat >= self.stall_after:
            return OscillationReport(
                verdict=OscillationVerdict.STALLED,
                reason=(
                    f"连续 {repeat} 轮失败签名相同（{trace.failed_ids}）。"
                    "反馈信号无效：断言给不出可用于修正的信息"
                ),
                repeat_count=repeat,
                forbid_hints=self._forbid_hints(),
            )

        if trace.failure_fp and repeat >= self.escalate_after:
            return OscillationReport(
                verdict=OscillationVerdict.ESCALATE,
                reason=f"连续 {repeat} 轮失败签名相同，需要换解法",
                repeat_count=repeat,
                forbid_hints=self._forbid_hints(),
            )

        diminishing = self._diminishing_returns()
        if diminishing is not None:
            return OscillationReport(
                verdict=OscillationVerdict.STALLED,
                reason=diminishing,
                forbid_hints=self._forbid_hints(),
            )

        return OscillationReport(verdict=OscillationVerdict.NONE)

    def _consecutive_failure_repeats(self) -> int:
        """从最新一轮往回数，连续相同 failure_fp 的轮数。"""
        if not self._history:
            return 0
        latest = self._history[-1].failure_fp
        if not latest:
            return 0

        count = 0
        for trace in reversed(self._history):
            if trace.failure_fp != latest:
                break
            count += 1
        return count

    def _diminishing_returns(self) -> str | None:
        """收益递减检测。

        连续 stall_patience 轮得分提升低于阈值 → 提前终止省成本。
        需要至少 patience+1 轮数据才能算出 patience 个增量。
        """
        needed = self.stall_patience + 1
        if len(self._history) < needed:
            return None

        window = self._history[-needed:]
        gains = [
            window[i + 1].score - window[i].score for i in range(len(window) - 1)
        ]
        if all(gain < self.stall_threshold for gain in gains):
            formatted = ", ".join(f"{g:+.1f}" for g in gains)
            return (
                f"连续 {self.stall_patience} 轮得分提升低于 {self.stall_threshold}"
                f"（{formatted}），收益递减"
            )
        return None

    def _forbid_hints(self) -> tuple[str, ...]:
        """历史失败尝试摘要，供 critique 的 forbidden 使用。

        把"这些路走过了"显式告诉模型，是防振荡的关键手段。
        """
        hints: list[str] = []
        seen: set[str] = set()
        for trace in self._history:
            if not trace.failed_ids:
                continue
            key = "+".join(sorted(trace.failed_ids))
            if key in seen:
                continue
            seen.add(key)
            hints.append(f"轮次 {trace.iteration}：{key} 未通过（得分 {trace.score:.0f}）")
        return tuple(hints)

    @property
    def history(self) -> tuple[IterationTrace, ...]:
        return tuple(self._history)

    def score_trend(self) -> tuple[float, ...]:
        return tuple(t.score for t in self._history)

    def restore(self, history: Sequence[IterationTrace]) -> None:
        """从检查点恢复历史。

        不恢复会让振荡检测在崩溃后失效：新 Worker 看不到历史，
        会以为每轮都是第一次见到该失败签名。
        """
        self._history = list(history)
