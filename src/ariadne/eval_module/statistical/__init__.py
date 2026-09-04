"""统计类评估器：重合度、编辑距离、（M2 后续）嵌入相似度。

需要 expected（参考答案）。适合回归检测，不适合质量评判 ——
"语义对但表述不同"会被误判低分。
"""

from ariadne.eval_module.statistical.overlap import (
    EditDistanceEvaluator,
    RougeLEvaluator,
    TokenF1Evaluator,
    tokenize,
)

__all__ = [
    "EditDistanceEvaluator",
    "RougeLEvaluator",
    "TokenF1Evaluator",
    "tokenize",
]
