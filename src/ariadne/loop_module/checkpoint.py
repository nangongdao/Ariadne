"""检查点与崩溃恢复。

四条铁律之"状态外置"：每轮 `JUDGING` 结束落检查点，进程崩溃后从检查点
续跑，绝不从零开始。恢复语义见 docs/03 第 9 节：

- 接管方读最新 Checkpoint，从 `iteration + 1` 继续，**不重跑已完成轮次**
- 累计预算从 Checkpoint 恢复 —— 重置预算是最容易出的账单事故
- 振荡检测历史也要恢复，否则新 Worker 看不到历史，会把第 N 次见到的
  失败签名当成第 1 次，振荡检测在崩溃后失效

存储抽象成 Protocol：单元测试用内存实现，生产用 Postgres（M3-spec 第 9 节，
`loop_checkpoints` 表，每轮一条不可变）。不 mock 数据库是项目测试哲学，
但 CheckpointStore 的 Postgres 实现属集成测试范畴，单元测试用内存桩即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from ariadne.loop_module.budget import BudgetUsage
from ariadne.loop_module.critique import Critique
from ariadne.loop_module.fingerprint import IterationTrace
from ariadne.loop_module.state_machine import LoopState
from ariadne.loop_module.verifier.base import Verdict


@dataclass(frozen=True)
class Checkpoint:
    """单轮检查点。不可变 —— 恢复语义要求"读到什么就是什么"。

    刻意比 docs/03 的最小定义多两个字段：history 与 critique_history。
    振荡检测需要全部历史 trace，critique 历史用于重建上下文的"历史失败摘要"段。
    不存它们会让恢复后的 Loop 丢失振荡检测能力与上下文连续性。
    """

    loop_id: str
    iteration: int
    state: LoopState
    usage: BudgetUsage
    output_fp: str
    failure_fp: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 恢复所需的可选快照。首轮为空。
    verdict: Verdict | None = None
    critique: Critique | None = None
    last_output: str = ""
    previous_output: str = ""
    history: tuple[IterationTrace, ...] = ()
    critique_history: tuple[Critique, ...] = ()
    # S3 对象键，不存内容本身（见 docs/03 第 9 节）
    artifact_refs: tuple[str, ...] = ()


class CheckpointStore(Protocol):
    """检查点存储。

    契约：save 幂等 —— 同一 (loop_id, iteration) 重复写不产生副作用。
    Worker 接管后可能重写最后一轮的检查点，幂等保证不会留下两条。
    """

    async def save(self, checkpoint: Checkpoint, *, project_id: UUID) -> None: ...

    async def latest(self, loop_id: str, *, project_id: UUID) -> Checkpoint | None: ...


class InMemoryCheckpointStore:
    """内存实现。仅用于测试与单机单 Loop。

    按 iteration 索引，latest 取最大 iteration。生产用 PostgresStore
    （落 loop_checkpoints 表，集成测试覆盖）。
    """

    def __init__(self) -> None:
        self._by_loop: dict[str, dict[int, Checkpoint]] = {}

    async def save(self, checkpoint: Checkpoint, *, project_id: UUID) -> None:
        per_loop = self._by_loop.setdefault(checkpoint.loop_id, {})
        per_loop[checkpoint.iteration] = checkpoint

    async def latest(self, loop_id: str, *, project_id: UUID) -> Checkpoint | None:
        per_loop = self._by_loop.get(loop_id)
        if not per_loop:
            return None
        return per_loop[max(per_loop)]

    def all(self, loop_id: str) -> tuple[Checkpoint, ...]:
        per_loop = self._by_loop.get(loop_id, {})
        return tuple(per_loop[i] for i in sorted(per_loop))


__all__ = [
    "Checkpoint",
    "CheckpointStore",
    "InMemoryCheckpointStore",
]
