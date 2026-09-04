"""Token 粗估 —— 只依赖标准库的叶子模块。

单独成模块而非留在 `loop_module.context`，是因为 Harness 规则侧也要用它：
`rules/input.yaml` 的 `token_count(input.text) > 12500` 与
`rules/output.yaml` 的 `token_count(output.text) > 200` 都走这里。

原先 `harness_module.functions.estimate_tokens` 在函数体内惰性
`from ariadne.loop_module.context import estimate_tokens`，为一个
`len(text) / 2.5` 的除法拖进整个 loop_module 包
（checkpoint → critique → eval_module → pydantic → asyncio，实测约 3.3s 冷导入）。
后果不是"启动慢一点"：这笔开销落在**首次规则求值**上，实测首次
`evaluate()` 耗时 757ms，超过 `DEFAULT_TIMEOUT_MS = 100` 后被 fail-closed
判成命中 —— worker 起来后的第一个请求会被无故拦下，且日志只显示
"cel eval timeout"，看不出真凶是 import。

放在 utils 下让两侧都能顶层 import，冷导入成本回到 0。
"""

from __future__ import annotations

# 粗略的字符→Token 比。中英混排取 2.5，偏保守（宁可高估）
CHARS_PER_TOKEN = 2.5


def estimate_tokens(text: str) -> int:
    """粗略 Token 估算。

    不引入 tiktoken：它要下载编码表且与 provider 的实际分词并不完全一致。
    预算判定用**保守高估**即可 —— 低估才是危险的（会超支）。

    Args:
        text: 待估算的文本。

    Returns:
        估算的 Token 数（下界为 1）。
    """
    return int(len(text) / CHARS_PER_TOKEN) + 1


__all__ = [
    "CHARS_PER_TOKEN",
    "estimate_tokens",
]
