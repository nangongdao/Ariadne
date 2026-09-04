"""Loop Engine —— 项目的核心差异化能力。

四条铁律（见 docs/03），任一放弃都会让 Loop 退化成"多试几次的 prompt engineering"：

1. 目标必须可验证 —— 创建时就拒绝模糊指令（goal.validate_goal）
2. 不信任模型自评 —— 只有 Verifier 的裁决才产生状态转移（verifier/）
3. 状态外置 —— 每轮落检查点，崩溃后从检查点续跑（checkpoint.py）
4. 预算硬熔断 —— 不是告警而是强制终止（budget.py）
"""

from ariadne.loop_module.budget import (
    BudgetDecision,
    BudgetGuard,
    BudgetUsage,
    BudgetVerdict,
    InMemoryCounter,
    to_micro_usd,
)
from ariadne.loop_module.checkpoint import (
    Checkpoint,
    CheckpointStore,
    InMemoryCheckpointStore,
)
from ariadne.loop_module.context import (
    ContextBuilder,
    ContextSegments,
    OutputMode,
)
from ariadne.loop_module.critique import Critique, CritiqueSynthesizer
from ariadne.loop_module.engine import (
    Clock,
    EventSink,
    IdempotencyStore,
    IterationResult,
    LLMClient,
    LLMResponse,
    LoopConfig,
    LoopEngine,
    LoopOutcome,
    MonotonicClock,
    NullEventSink,
    NullIdempotencyStore,
)
from ariadne.loop_module.fingerprint import (
    IterationTrace,
    OscillationDetector,
    OscillationReport,
    OscillationVerdict,
    failure_fingerprint,
    output_fingerprint,
)
from ariadne.loop_module.goal import (
    Assertion,
    AssertionKind,
    Budget,
    Goal,
    LoopMode,
)
from ariadne.loop_module.goal_validation import (
    GoalValidationError,
    ValidationIssue,
    ValidationReport,
    validate_goal,
)
from ariadne.loop_module.modes import (
    BaseLoopMode,
    LoopModeFactory,
    RetryDecision,
    available_modes,
    register_loop_mode,
)
from ariadne.loop_module.parallel import ConcurrencyGate
from ariadne.loop_module.state_machine import (
    SUCCESS_STATES,
    TERMINAL_STATES,
    InvalidTransitionError,
    LoopEvent,
    LoopState,
    allowed_events,
    diagnose,
    is_success,
    is_terminal,
    next_state,
    try_next_state,
)

__all__ = [
    "SUCCESS_STATES",
    "TERMINAL_STATES",
    "Assertion",
    "AssertionKind",
    "BaseLoopMode",
    "Budget",
    "BudgetDecision",
    "BudgetGuard",
    "BudgetUsage",
    "BudgetVerdict",
    "Checkpoint",
    "CheckpointStore",
    "Clock",
    "ConcurrencyGate",
    "ContextBuilder",
    "ContextSegments",
    "Critique",
    "CritiqueSynthesizer",
    "EventSink",
    "Goal",
    "GoalValidationError",
    "IdempotencyStore",
    "InMemoryCheckpointStore",
    "InMemoryCounter",
    "InvalidTransitionError",
    "IterationResult",
    "IterationTrace",
    "LLMClient",
    "LLMResponse",
    "LoopConfig",
    "LoopEngine",
    "LoopEvent",
    "LoopMode",
    "LoopModeFactory",
    "LoopOutcome",
    "LoopState",
    "MonotonicClock",
    "NullEventSink",
    "NullIdempotencyStore",
    "OscillationDetector",
    "OscillationReport",
    "OscillationVerdict",
    "OutputMode",
    "RetryDecision",
    "ValidationIssue",
    "ValidationReport",
    "allowed_events",
    "available_modes",
    "diagnose",
    "failure_fingerprint",
    "is_success",
    "is_terminal",
    "next_state",
    "output_fingerprint",
    "register_loop_mode",
    "to_micro_usd",
    "try_next_state",
    "validate_goal",
]
