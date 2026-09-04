"""LLM-as-Judge：可信度最低但覆盖面最广的一类评估器。

六条可信度措施（见 docs/05）：
1. 版本锁定 + temperature=0 + seed        → runner.JudgeConfig
2. 结构化输出而非自由文本打分              → prompts.JUDGE_OUTPUT_SCHEMA
3. 双向投票消除位置偏差                    → pairwise.PairwiseJudge
4. 与人工标注对齐（Cohen's κ）             → kappa.weighted_kappa
5. 元评测（对抗集）                        → meta.MetaEvaluation
6. 生成模型与 Judge 模型强制隔离            → runner.enforce_isolation
"""

from ariadne.eval_module.judge.kappa import (
    KAPPA_BLOCKING_MIN,
    KappaGateError,
    KappaReport,
    KappaVerdict,
    bucketize,
    enforce_gate,
    weighted_kappa,
)
from ariadne.eval_module.judge.meta import (
    META_ACCURACY_MIN,
    AdversarialCase,
    CaseOutcome,
    Expectation,
    MetaEvaluation,
    MetaReport,
    builtin_meta_evaluation,
)
from ariadne.eval_module.judge.pairwise import (
    PairwiseJudge,
    PairwiseVerdict,
    PairwiseVote,
    Winner,
    aggregate_votes,
)
from ariadne.eval_module.judge.prompts import (
    JUDGE_OUTPUT_SCHEMA,
    PAIRWISE_OUTPUT_SCHEMA,
    available_dimensions,
    pairwise_prompt,
    system_prompt,
    user_prompt,
)
from ariadne.eval_module.judge.runner import (
    JudgeClient,
    JudgeConfig,
    JudgeEvaluator,
    JudgeIsolationError,
    JudgeResponse,
    enforce_isolation,
    parse_judge_output,
)

__all__ = [
    "JUDGE_OUTPUT_SCHEMA",
    "KAPPA_BLOCKING_MIN",
    "META_ACCURACY_MIN",
    "PAIRWISE_OUTPUT_SCHEMA",
    "AdversarialCase",
    "CaseOutcome",
    "Expectation",
    "JudgeClient",
    "JudgeConfig",
    "JudgeEvaluator",
    "JudgeIsolationError",
    "JudgeResponse",
    "KappaGateError",
    "KappaReport",
    "KappaVerdict",
    "MetaEvaluation",
    "MetaReport",
    "PairwiseJudge",
    "PairwiseVerdict",
    "PairwiseVote",
    "Winner",
    "aggregate_votes",
    "available_dimensions",
    "bucketize",
    "builtin_meta_evaluation",
    "enforce_gate",
    "enforce_isolation",
    "pairwise_prompt",
    "parse_judge_output",
    "system_prompt",
    "user_prompt",
    "weighted_kappa",
]
