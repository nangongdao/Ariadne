"""Harness 约束引擎 —— AI 工作流的硬边界。

与 Loop Engine 的软目标严格区分（docs/04）：
- Loop 失败生成 critique 重试；Harness 失败直接 block。
- Harness 的 block 不能通过"多试几轮"绕过。
- 规则求值是无状态纯函数，fail-closed（超时/异常视为拒绝）。

四类规则（input/output/resource/tool）× 五卡点 × 六动作。
"""

from ariadne.harness_module.actions import ActionResult, RewriteError, apply_decision
from ariadne.harness_module.audit import (
    AuditCollector,
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
    NullAuditSink,
)
from ariadne.harness_module.evaluator import (
    CompiledRule,
    EvaluationError,
    HarnessEvaluator,
    compile_rule,
    compile_rule_with_functions,
    compile_rules,
    compile_rules_with_functions,
)
from ariadne.harness_module.functions import BUILTIN_FUNCTIONS
from ariadne.harness_module.loader import (
    RuleLoadError,
    RuleYAML,
    compile_rule_set,
    load_rule_set,
    load_rules,
)
from ariadne.harness_module.models import (
    ACTION_PRIORITY,
    Action,
    Decision,
    HarnessContext,
    HookKind,
    Rule,
    RuleCategory,
    RuleHit,
    Severity,
)
from ariadne.harness_module.resolve import resolve, rule_sort_key

__all__ = [
    "ACTION_PRIORITY",
    "BUILTIN_FUNCTIONS",
    "Action",
    "ActionResult",
    "AuditCollector",
    "AuditRecord",
    "AuditSink",
    "CompiledRule",
    "Decision",
    "EvaluationError",
    "HarnessContext",
    "HarnessEvaluator",
    "HookKind",
    "InMemoryAuditSink",
    "NullAuditSink",
    "RewriteError",
    "Rule",
    "RuleCategory",
    "RuleHit",
    "RuleLoadError",
    "RuleYAML",
    "Severity",
    "apply_decision",
    "compile_rule",
    "compile_rule_set",
    "compile_rule_with_functions",
    "compile_rules",
    "compile_rules_with_functions",
    "load_rule_set",
    "load_rules",
    "resolve",
    "rule_sort_key",
]
