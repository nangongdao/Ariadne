"""冲突消解 —— 多规则命中时选最终裁决的纯函数。

确定性是审计的前提（docs/04 第 4 节）：同样的规则集 + 同样的上下文
必须永远得到同样的裁决，否则事后无法复现"当时为什么放行了"。

排序键：action 优先级 → severity（高优先）→ rule_id 字典序。
三者都是确定性的，不依赖求值顺序或时间。
"""

from __future__ import annotations

from collections.abc import Sequence

from ariadne.harness_module.models import (
    Action,
    Decision,
    Rule,
    RuleHit,
)


def resolve(hits: Sequence[RuleHit]) -> Decision:
    """从一批命中里选最终裁决。

    无命中时返回 ALLOW（默认放行）。
    有命中时按确定性顺序选胜出者，其余记入 hits 供审计回溯。
    """
    if not hits:
        return Decision(action=Action.ALLOW, hits=(), winning_hit=None)

    ordered = sorted(hits, key=_sort_key)
    winner = ordered[0]
    return Decision(
        action=winner.rule.action,
        hits=tuple(ordered),
        winning_hit=winner,
        message=winner.message or winner.rule.message,
        rewrite_strategy=winner.rule.rewrite_strategy,
        route_target=winner.rule.route_target,
    )


def _sort_key(hit: RuleHit) -> tuple[int, int, str]:
    """排序键：动作优先级（小优先）→ 严重度（高优先）→ rule_id 字典序。"""
    return (
        hit.rule.priority,
        -hit.rule.severity.rank,  # 负号让高严重度排前
        hit.rule.id,
    )


def rule_sort_key(rule: Rule) -> tuple[int, int, str]:
    """规则排序键（供规则集加载时稳定排序用）。"""
    return (
        rule.priority,
        -rule.severity.rank,
        rule.id,
    )


__all__ = ["resolve", "rule_sort_key"]
