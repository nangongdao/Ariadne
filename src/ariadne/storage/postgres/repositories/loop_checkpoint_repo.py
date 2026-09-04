"""Loop 检查点仓储 —— CheckpointStore 的 Postgres 实现。

把 domain Checkpoint（dataclass）序列化到 loop_checkpoints 表，反序列化回来。
满足 CheckpointStore 契约：save 幂等（主键 upsert），latest 取最大 iteration。

序列化用 dataclasses.asdict 递归转 dict；枚举（LoopState/AssertionKind）转字符串。
反序列化重建：frozen dataclass 用构造器，枚举用 LoopState(value) 还原。

不 mock 数据库是项目测试哲学：用 aiosqlite 跑真实 SQL 验证建表与往返，
Postgres 特有行为（JSONB 操作符、部分索引）留给 -m integration。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.loop_module.budget import BudgetUsage
from ariadne.loop_module.checkpoint import Checkpoint
from ariadne.loop_module.critique import Critique
from ariadne.loop_module.fingerprint import IterationTrace
from ariadne.loop_module.state_machine import LoopState
from ariadne.loop_module.verifier.base import AssertionOutcome, Verdict


def _serialize_verdict(verdict: Verdict | None) -> dict[str, Any] | None:
    if verdict is None:
        return None
    d = asdict(verdict)
    # outcomes 里的 kind 是 AssertionKind 枚举，转字符串
    d["outcomes"] = [_serialize_outcome(o) for o in verdict.outcomes]
    d["failed"] = [_serialize_outcome(o) for o in verdict.failed]
    return d


def _serialize_outcome(outcome: AssertionOutcome) -> dict[str, Any]:
    d = asdict(outcome)
    d["kind"] = outcome.kind.value
    return d


def _deserialize_outcome(d: dict[str, Any]) -> AssertionOutcome:
    from ariadne.loop_module.goal import AssertionKind

    return AssertionOutcome(
        assertion_id=d["assertion_id"],
        kind=AssertionKind(d["kind"]),
        passed=d["passed"],
        value=d.get("value", 0.0),
        evidence=d.get("evidence", ""),
        pending_human=d.get("pending_human", False),
        errored=d.get("errored", False),
        duration_ms=d.get("duration_ms", 0),
    )


def _deserialize_verdict(d: dict[str, Any] | None) -> Verdict | None:
    if d is None:
        return None
    outcomes = tuple(_deserialize_outcome(o) for o in d.get("outcomes", []))
    failed = tuple(_deserialize_outcome(o) for o in d.get("failed", []))
    return Verdict(
        converged=d["converged"],
        passed=tuple(d.get("passed", [])),
        failed=failed,
        score=d.get("score", 0.0),
        claimed_done=d.get("claimed_done", False),
        pending_human=tuple(d.get("pending_human", [])),
        errored=tuple(d.get("errored", [])),
        outcomes=outcomes,
    )


def _serialize_critique(critique: Critique | None) -> dict[str, Any] | None:
    if critique is None:
        return None
    return asdict(critique)


def _deserialize_critique(d: dict[str, Any] | None) -> Critique | None:
    if d is None:
        return None
    return Critique(
        failures=tuple(d.get("failures", [])),
        evidence=tuple(d.get("evidence", [])),
        directives=tuple(d.get("directives", [])),
        forbidden=tuple(d.get("forbidden", [])),
        escalation=d.get("escalation", ""),
    )


def _serialize_trace(trace: IterationTrace) -> dict[str, Any]:
    return asdict(trace)


def _deserialize_trace(d: dict[str, Any]) -> IterationTrace:
    return IterationTrace(
        iteration=d["iteration"],
        output_fp=d["output_fp"],
        failure_fp=d["failure_fp"],
        score=d.get("score", 0.0),
        failed_ids=tuple(d.get("failed_ids", [])),
    )


def serialize_checkpoint(checkpoint: Checkpoint) -> dict[str, Any]:
    """Checkpoint → 可 JSON 序列化的 dict（存 loop_checkpoints 列）。"""
    return {
        "verdict": _serialize_verdict(checkpoint.verdict),
        "critique": _serialize_critique(checkpoint.critique),
        "last_output": checkpoint.last_output,
        "previous_output": checkpoint.previous_output,
        "history": [_serialize_trace(t) for t in checkpoint.history],
        "critique_history": [
            c for c in (_serialize_critique(c) for c in checkpoint.critique_history) if c
        ],
    }


def deserialize_checkpoint(
    row_state: str,
    row_iteration: int,
    row_output_fp: str,
    row_failure_fp: str,
    row_tokens: int,
    row_cost_usd: float,
    row_verdict: dict[str, Any],
    row_critique: dict[str, Any] | None,
    loop_id: str,
) -> Checkpoint:
    """从行数据重建 Checkpoint。"""
    return Checkpoint(
        loop_id=loop_id,
        iteration=row_iteration,
        state=LoopState(row_state),
        usage=BudgetUsage(
            total_tokens=row_tokens,
            cost_micro_usd=int(row_cost_usd * 1_000_000),
        ),
        output_fp=row_output_fp,
        failure_fp=row_failure_fp,
        verdict=_deserialize_verdict(row_verdict),
        critique=_deserialize_critique(row_critique),
        last_output=row_verdict.get("last_output", ""),
        previous_output=row_verdict.get("previous_output", ""),
        history=tuple(
            _deserialize_trace(t) for t in row_verdict.get("history", [])
        ),
        critique_history=tuple(
            c for c in (
                _deserialize_critique(x) for x in row_verdict.get("critique_history", [])
            ) if c
        ),
    )


class LoopCheckpointRepository:
    """CheckpointStore 的 Postgres 实现。

    所有方法带 session，由调用方（Worker/API）管理事务边界。
    loop_id 用 str（domain 层），表里是 UUID —— 转换在 _to_uuid。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, checkpoint: Checkpoint, *, project_id: uuid.UUID) -> None:
        from ariadne.storage.postgres.loop_models import LoopCheckpointRow

        payload = serialize_checkpoint(checkpoint)
        loop_uuid = _to_uuid(checkpoint.loop_id)
        # 把额外字段塞进 verdict JSON（供反序列化时取回）。
        # verdict 列本身存裁决，扩展字段放一起避免加列。
        verdict_json = {**payload["verdict"], **{
            "last_output": checkpoint.last_output,
            "previous_output": checkpoint.previous_output,
            "history": payload["history"],
            "critique_history": payload["critique_history"],
        }}

        # 幂等写：主键 (loop_id, iteration) 冲突时覆盖。
        # Postgres 用 ON CONFLICT；SQLite 用 INSERT OR REPLACE（见 _upsert）。
        await self._upsert(LoopCheckpointRow, {
            "loop_id": loop_uuid,
            "iteration": checkpoint.iteration,
            "project_id": project_id,
            "state": checkpoint.state.value,
            "verdict": verdict_json,
            "critique": payload["critique"],
            "artifact_refs": list(checkpoint.artifact_refs),
            "cumulative_tokens": checkpoint.usage.total_tokens,
            "cumulative_cost_usd": float(checkpoint.usage.cost_usd),
            "output_fp": checkpoint.output_fp,
            "failure_fp": checkpoint.failure_fp,
        })

    async def latest(self, loop_id: str, *, project_id: uuid.UUID) -> Checkpoint | None:
        from ariadne.storage.postgres.loop_models import LoopCheckpointRow

        loop_uuid = _to_uuid(loop_id)
        stmt = (
            select(LoopCheckpointRow)
            .where(
                LoopCheckpointRow.loop_id == loop_uuid,
                LoopCheckpointRow.project_id == project_id,
            )
            .order_by(desc(LoopCheckpointRow.iteration))
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return deserialize_checkpoint(
            row_state=row.state,
            row_iteration=row.iteration,
            row_output_fp=row.output_fp,
            row_failure_fp=row.failure_fp,
            row_tokens=row.cumulative_tokens,
            row_cost_usd=float(row.cumulative_cost_usd),
            row_verdict=row.verdict,
            row_critique=row.critique,
            loop_id=loop_id,
        )

    async def _upsert(self, model: type, values: dict[str, Any]) -> None:
        """跨方言 upsert。Postgres ON CONFLICT / SQLite INSERT OR REPLACE。"""
        from sqlalchemy import insert

        stmt = insert(model).values(**values)
        # Postgres: ON CONFLICT (loop_id, iteration) DO UPDATE
        # SQLite: INSERT OR REPLACE（通过 dialect 判断）
        if self._session.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(model).values(**values)
            update_cols = {c: stmt.excluded[c] for c in values if c not in ("loop_id", "iteration")}
            stmt = stmt.on_conflict_do_update(
                index_elements=["loop_id", "iteration"], set_=update_cols
            )
        else:
            # SQLite：INSERT OR REPLACE 会先删旧行再插，语义等价于覆盖
            stmt = stmt.prefix_with("OR REPLACE")
        await self._session.execute(stmt)


def _to_uuid(loop_id: str) -> uuid.UUID:
    """str → UUID。engine 用 str loop_id，表用 UUID。"""
    try:
        return uuid.UUID(loop_id)
    except ValueError as exc:
        raise ValueError(f"loop_id 必须是合法 UUID 字符串，收到 {loop_id!r}") from exc


__all__ = [
    "LoopCheckpointRepository",
    "deserialize_checkpoint",
    "serialize_checkpoint",
]
