"""成对比较与双向投票。

模型在成对比较中系统性偏好靠前的选项（position bias）。做法：同时评
(A,B) 与 (B,A)，只有两次结论一致才采信，不一致标 tie。

代价是调用翻倍，因此**只在成对比较模式下启用** —— 单样本打分不需要。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ariadne.eval_module.judge.prompts import (
    PAIRWISE_OUTPUT_SCHEMA,
    pairwise_prompt,
    system_prompt,
)
from ariadne.eval_module.judge.runner import JudgeClient


class Winner(StrEnum):
    A = "A"
    B = "B"
    TIE = "tie"


@dataclass(frozen=True)
class PairwiseVote:
    """单次投票结果。"""

    winner: Winner
    reasoning: str
    swapped: bool  # 该次投票是否交换了 A/B 顺序


@dataclass(frozen=True)
class PairwiseVerdict:
    """双向投票的最终结论。"""

    winner: Winner
    forward: PairwiseVote
    reverse: PairwiseVote
    # 两次结论是否一致。不一致时 winner 被强制为 tie
    consistent: bool
    cost_usd: Decimal = Decimal("0")
    error: str = ""

    @property
    def position_bias_detected(self) -> bool:
        """两次都选了"靠前的那个"→ 位置偏差的直接证据。

        forward 选 A、reverse 也选"显示在前的"（即原 B）时，
        两次结论矛盾且都偏向首位。
        """
        if self.consistent:
            return False
        return (
            self.forward.winner is Winner.A and self.reverse.winner is Winner.A
        ) or (self.forward.winner is Winner.B and self.reverse.winner is Winner.B)


def _parse_vote(content: str, *, swapped: bool) -> tuple[PairwiseVote | None, str]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        text = "\n".join(body).strip()

    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"投票输出非合法 JSON: {exc.msg}"

    if not isinstance(payload, dict):
        return None, "投票输出不是对象"

    raw = str(payload.get("winner", "")).strip()
    if raw not in {w.value for w in Winner}:
        return None, f"winner 字段非法: {raw!r}"

    return (
        PairwiseVote(
            winner=Winner(raw),
            reasoning=str(payload.get("reasoning", ""))[:1000],
            swapped=swapped,
        ),
        "",
    )


def _flip(winner: Winner) -> Winner:
    """把交换顺序后的结论翻译回原始 A/B 语义。"""
    if winner is Winner.A:
        return Winner.B
    if winner is Winner.B:
        return Winner.A
    return Winner.TIE


class PairwiseJudge:
    """双向投票的成对比较。"""

    def __init__(
        self,
        *,
        client: JudgeClient,
        model: str,
        dimension: str = "helpfulness",
        temperature: float = 0.0,
        seed: int | None = 42,
    ) -> None:
        self._client = client
        self._model = model
        self._system = system_prompt(dimension)
        self._temperature = temperature
        self._seed = seed

    def compare(self, *, task: str, output_a: str, output_b: str) -> PairwiseVerdict:
        """双向投票。两次结论不一致时返回 tie。"""
        forward_raw = self._vote(task, output_a, output_b)
        reverse_raw = self._vote(task, output_b, output_a)

        forward, err_f = _parse_vote(forward_raw.content, swapped=False)
        reverse, err_r = _parse_vote(reverse_raw.content, swapped=True)
        cost = forward_raw.cost_usd + reverse_raw.cost_usd

        if forward is None or reverse is None:
            empty = PairwiseVote(winner=Winner.TIE, reasoning="", swapped=False)
            return PairwiseVerdict(
                winner=Winner.TIE,
                forward=forward or empty,
                reverse=reverse or empty,
                consistent=False,
                cost_usd=cost,
                error=err_f or err_r,
            )

        # reverse 是交换顺序后的结论，翻译回原语义再比对
        reverse_in_original = _flip(reverse.winner)
        consistent = forward.winner is reverse_in_original

        return PairwiseVerdict(
            winner=forward.winner if consistent else Winner.TIE,
            forward=forward,
            reverse=reverse,
            consistent=consistent,
            cost_usd=cost,
        )

    def _vote(self, task: str, first: str, second: str) -> Any:
        return self._client.complete(
            system=self._system,
            user=pairwise_prompt(task=task, output_a=first, output_b=second),
            schema=PAIRWISE_OUTPUT_SCHEMA,
            model=self._model,
            temperature=self._temperature,
            seed=self._seed,
        )


def aggregate_votes(verdicts: list[PairwiseVerdict]) -> dict[str, int | float]:
    """汇总一批成对比较。

    `inconsistency_rate` 是位置偏差的量化指标：偏高说明该 Judge 在此
    任务上不可靠，应换模型或改用单样本打分。
    """
    total = len(verdicts)
    if total == 0:
        return {"total": 0, "a_wins": 0, "b_wins": 0, "ties": 0, "inconsistency_rate": 0.0}

    a_wins = sum(1 for v in verdicts if v.winner is Winner.A)
    b_wins = sum(1 for v in verdicts if v.winner is Winner.B)
    inconsistent = sum(1 for v in verdicts if not v.consistent)

    return {
        "total": total,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "ties": total - a_wins - b_wins,
        "inconsistency_rate": round(inconsistent / total, 4),
    }
