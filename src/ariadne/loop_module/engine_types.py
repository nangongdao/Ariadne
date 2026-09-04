"""Loop Engine 的类型与协议定义。

从 engine.py 拆出（2026-09-04）：协议（LLM 客户端、时钟、幂等、事件）
与数据类（LLMResponse、IterationResult、LoopOutcome）独立成模块，
让 engine.py 聚焦状态机主循环本身，符合 800 行文件上限约定。

所有外部依赖抽象成 Protocol，单元测试用纯桩即可跑通收敛/假完成/预算
熔断/振荡/崩溃恢复全部路径 —— engine 的正确性不该依赖真实 provider
或容器。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from ariadne.loop_module.budget import BudgetGuard, BudgetUsage
from ariadne.loop_module.checkpoint import CheckpointStore
from ariadne.loop_module.critique import Critique, CritiqueSynthesizer
from ariadne.loop_module.fingerprint import OscillationDetector
from ariadne.loop_module.goal import Goal
from ariadne.loop_module.modes import BaseLoopMode, LoopModeFactory
from ariadne.loop_module.state_machine import LoopEvent, LoopState
from ariadne.loop_module.verifier.base import Verdict
from ariadne.loop_module.verifier.builtin import MetricProvider

if TYPE_CHECKING:
    from ariadne.harness_module.audit import AuditSink
    from ariadne.harness_module.evaluator import HarnessEvaluator
    from ariadne.loop_module.artifact import ArtifactWriter
    from ariadne.loop_module.verifier.command import CommandRunner


@dataclass(frozen=True)
class LLMResponse:
    """LLM 调用结果。

    usage 不可缺省：预算结算必须按真实用量，估算会让预算熔断失准。
    rate_limit_quota 可选：Anthropic 等 provider 返回剩余配额，用于预测性调度。
    """

    output: str
    input_tokens: int
    output_tokens: int
    # 模型自称完成。**仅记录用于假完成统计，不参与收敛判定**。
    claimed_done: bool = False
    model: str = ""
    cost_usd: Decimal = Decimal(0)
    # 速率限制配额信息（主动余量调度）。None 表示 provider 不返回配额头。
    rate_limit_quota: Any = None  # typing.Any 避免循环导入，实际类型是 RateLimitQuota | None


class LLMClient(Protocol):
    """LLM 调用抽象。

    抽象成 Protocol 让单元测试用脚本桩跑通收敛路径，生产用真实 provider
    adapter。engine 不关心 provider 细节，只管输出与用量。
    """

    async def complete(self, prompt: str, *, model: str) -> LLMResponse: ...


class Clock(Protocol):
    """时钟抽象。测试用可控时钟验证墙钟熔断，生产用 time.monotonic。"""

    def monotonic(self) -> float: ...


class MonotonicClock:
    def monotonic(self) -> float:
        import time

        return time.monotonic()


class IdempotencyStore(Protocol):
    """幂等键存储。防止 Worker 接管后副作用重复执行（docs/03 第 9 节）。

    三个方法而非只有 try_acquire：跳过执行后引擎必须报一个结果，
    而裸租约拿不回上次结果 —— 报失败会让 Loop 去改没坏的代码，报通过
    是伪造。语义细节见 loop_module.idempotency 的模块 docstring。
    """

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool: ...

    async def recall(self, key: str) -> str | None: ...

    async def remember(self, key: str, payload: str, ttl_seconds: int) -> None: ...


class NullIdempotencyStore:
    """不幂等。仅用于无副作用的纯生成场景。

    try_acquire 恒真 + recall 恒空 = 每次都执行、从不复用，
    与"没有幂等守卫"等价。
    """

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool:
        return True

    async def recall(self, key: str) -> str | None:
        return None

    async def remember(self, key: str, payload: str, ttl_seconds: int) -> None:
        return None


class EventSink(Protocol):
    """SSE/进度事件出口。engine 每次状态变化推送，前端据此渲染进化视图。"""

    async def emit(self, event: LoopEvent) -> None: ...


class NullEventSink:
    async def emit(self, event: LoopEvent) -> None: ...


@dataclass
class LoopConfig:
    """Engine 装配。

    刻意把所有可注入依赖放一个 dataclass：构造 engine 的地方（worker/API）
    只需组装这一个对象，且测试能只替换需要的部分。
    """

    goal: Goal
    loop_id: str
    project_id: UUID
    budget_guard: BudgetGuard
    llm: LLMClient
    checkpoint_store: CheckpointStore
    mode: BaseLoopMode = field(default_factory=lambda: LoopModeFactory("quality"))
    metric_provider: MetricProvider | None = None
    critique: CritiqueSynthesizer = field(default_factory=CritiqueSynthesizer)
    oscillator: OscillationDetector = field(default_factory=OscillationDetector)
    idempotency: IdempotencyStore = field(default_factory=NullIdempotencyStore)
    event_sink: EventSink = field(default_factory=NullEventSink)
    clock: Clock = field(default_factory=MonotonicClock)
    # 真实模型名。None 时用 mode.base_model（测试用桩模式名）。
    # 生产装配（worker）传入 settings.llm 的 model / degraded_model。
    model: str | None = None
    degraded_model: str | None = None
    # COMMAND 类断言的工作目录。None 时 command 断言记 errored（见 CommandVerifier）
    artifact_path: Path | None = None
    # 副作用工具执行器。模型在输出里用 ```ariadne-tool 围栏块显式请求
    # 调用（契约见 loop_module.tools），引擎在产出物落盘后执行。
    # None 时模型请求工具按执行失败处理 —— 绝不静默忽略，否则模型以为
    # 副作用已发生而断言在验证磁盘，Loop 只会空转烧预算。
    tool_executor: Callable[[str, dict[str, object]], Awaitable[str]] | None = None
    # M4 Harness 规则引擎。None 时 _precheck 行为同 M3（硬编码放行）。
    harness: HarnessEvaluator | None = None
    # M4 审计 sink。None 时用 NullAuditSink（不记审计）。
    audit_sink: AuditSink | None = None
    # COMMAND 断言的底层执行器。None 时用 RestrictedRunner（受限子进程）。
    # 生产装配（worker）在沙箱可用时传入 SandboxRunner —— 受限子进程按自己的
    # 文档防不住内核层逃逸，不该是多租户下的默认。
    command_runner: CommandRunner | None = None
    # 产出物落盘器。把模型输出物化成 artifact_path 下的文件，供 COMMAND 断言验证。
    # None 且存在 COMMAND 断言 + artifact_path 时，engine 自动装 FencedCodeWriter
    # —— 见 _build_artifact_writer。要显式关闭请传 NullArtifactWriter。
    artifact_writer: ArtifactWriter | None = None


@dataclass
class IterationResult:
    """单轮可见结果。供 SSE 推送与进化视图消费。"""

    iteration: int
    state: LoopState
    verdict: Verdict | None
    critique: Critique | None
    usage: BudgetUsage
    output_fp: str
    failure_fp: str
    false_completion: bool


@dataclass
class LoopOutcome:
    """Loop 终态结果。

    last_rate_limit_quota 用于主动余量调度：并行池读取最后一次 LLM 调用
    的配额信息，预测配额耗尽前主动降并发（2026-09-03）。
    """

    loop_id: str
    final_state: LoopState
    iterations: int
    usage: BudgetUsage
    verdict: Verdict | None
    converged: bool
    # 最后一次 LLM 调用的速率限制配额（主动余量调度）。
    # None 表示未调用 LLM 或 provider 不返回配额头。
    last_rate_limit_quota: Any = None

    @property
    def success(self) -> bool:
        return self.final_state is LoopState.CONVERGED


__all__ = [
    "Clock",
    "EventSink",
    "IdempotencyStore",
    "IterationResult",
    "LLMClient",
    "LLMResponse",
    "LoopOutcome",
    "MonotonicClock",
    "NullEventSink",
    "NullIdempotencyStore",
]