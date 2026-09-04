"""确定性评估器：正则、Schema、数值。

可信度 ★★★★★，成本极低，延迟 ms 级。**能用这类解决的，绝不用 Judge。**
"""

from __future__ import annotations

from ariadne.eval_module.deterministic.numeric import (
    ExactMatchEvaluator,
    ForbiddenTermsEvaluator,
    NumericRangeEvaluator,
    WordCountEvaluator,
    count_words,
)
from ariadne.eval_module.deterministic.regex import (
    CitationCountEvaluator,
    MarkdownStructureEvaluator,
    RegexEvaluator,
    count_citations,
)
from ariadne.eval_module.deterministic.schema import (
    JsonParsableEvaluator,
    JsonSchemaEvaluator,
    RequiredFieldsEvaluator,
)

__all__ = [
    "CitationCountEvaluator",
    "ExactMatchEvaluator",
    "ForbiddenTermsEvaluator",
    "JsonParsableEvaluator",
    "JsonSchemaEvaluator",
    "MarkdownStructureEvaluator",
    "NumericRangeEvaluator",
    "RegexEvaluator",
    "RequiredFieldsEvaluator",
    "WordCountEvaluator",
    "build_deterministic_evaluator",
    "count_citations",
    "count_words",
]


# build_deterministic_evaluator 曾是一条写死 10 个分支的 if-chain，而
# EVALUATOR_REGISTRY 里有 14 个评估器 —— 注册表被短路，四个评估器生产侧
# 不可达。现在改由 eval_module.factory 按注册表反射构造，本模块只重导出。
# 放在文件末尾且延迟导入：factory 要 import 本包的评估器类，模块顶部导入
# 会成环。
from ariadne.eval_module.factory import (
    build_deterministic_evaluator,
)

