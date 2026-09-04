"""loop_runs 仓储。

Loop 运行实例的读写。承载可变运行态（state/iteration/租约）与元数据。
与 CheckpointRepository（每轮不可变快照）分工：本仓库管"当前状态在哪"，
检查点仓库管"崩溃后从哪续跑"。

状态流转用显式方法而非直接改字段：与 ExperimentRepository 同原则，
散落的 `run.state = "x"` 会让"什么时候能转到什么状态"无从追溯。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.loop_module.goal import Goal
from ariadne.loop_module.state_machine import LoopState, is_terminal
from ariadne.storage.postgres.loop_models import LoopRun

TERMINAL_WAIT_SECONDS = 900


class LoopRunNotFoundError(LookupError):
    pass


class LoopRunConflictError(ValueError):
    """状态冲突。如取消已完成/终态的 Loop。"""


class LoopRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        mode: str,
        goal: Goal,
        spec_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """插入一条 Loop 运行记录，初始状态 VALIDATE。

        loop_runs.goal 存序列化后的 Goal —— API 创建时已完成可验证性校验，
        Worker 端用同一序列化重建 Goal 并重新过 VALIDATE（两层校验）。
        """
        row = LoopRun(
            id=uuid.uuid4(),
            project_id=project_id,
            spec_id=spec_id,
            mode=mode,
            goal=_goal_to_dict(goal),
            state=LoopState.VALIDATE.value,
            iteration=0,
            cumulative_tokens=0,
            cumulative_cost_usd=0,
            worker_id=None,
            lease_expires_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def _load(
        self, *, project_id: uuid.UUID, loop_id: uuid.UUID
    ) -> LoopRun:
        row = (
            await self._session.execute(
                select(LoopRun).where(
                    LoopRun.id == loop_id,
                    LoopRun.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise LoopRunNotFoundError(f"Loop 不存在: {loop_id}")
        return row

    async def get(
        self, *, project_id: uuid.UUID, loop_id: uuid.UUID
    ) -> LoopRun:
        return await self._load(project_id=project_id, loop_id=loop_id)

    async def by_id(self, loop_id: uuid.UUID) -> LoopRun:
        """按 id 取行（Worker 用，不限定 project）。"""
        return await self._by_id(loop_id)

    async def finish(
        self,
        *,
        loop_id: uuid.UUID,
        final_state: LoopState,
        error: str = "",
        worker_id: str,
    ) -> None:
        """终态落库：写 final_state/error + 释放租约。"""
        row = await self._by_id(loop_id)
        row.final_state = final_state.value
        row.state = final_state.value
        row.error = error
        row.finished_at = datetime.now(UTC)
        row.worker_id = None
        row.lease_expires_at = None
        await self._session.flush()

    async def _by_id(self, loop_id: uuid.UUID) -> LoopRun:
        row = (
            await self._session.execute(select(LoopRun).where(LoopRun.id == loop_id))
        ).scalar_one_or_none()
        if row is None:
            raise LoopRunNotFoundError(f"Loop 不存在: {loop_id}")
        return row

    async def transition(
        self,
        *,
        loop_id: uuid.UUID,
        to: LoopState,
        error: str = "",
        project_id: uuid.UUID | None = None,
    ) -> LoopRun:
        """推进 Loop 状态。非法转移（已终态再转）显式报错。"""
        # API 调用必须带 project_id；仅 Worker/迁移等内部流程才允许
        # 使用全局 loop_id。否则在数据库 owner 绕过 RLS 时会产生跨租户修改。
        row = (
            await self._load(project_id=project_id, loop_id=loop_id)
            if project_id is not None
            else await self._by_id(loop_id)
        )
        if is_terminal(LoopState(row.state)):
            raise LoopRunConflictError(
                f"Loop {loop_id} 已是终态 {row.state}，不能再转移到 {to.value}"
            )
        row.state = to.value
        if to is LoopState.CANCELLED or is_terminal(to):
            row.final_state = to.value
            row.finished_at = datetime.now(UTC)
        if error:
            row.error = error
        await self._session.flush()
        return row

    async def begin_lease(
        self, *, loop_id: uuid.UUID, worker_id: str, duration_seconds: int
    ) -> LoopRun:
        """Worker 接管时获取租约。

        租约 = worker_id + 过期时间。崩溃的 Worker 租约过期后由其他
        Worker 扫描接管（见 worker/loop_worker.py 的 reclaim）。

        条件更新而非直接赋值：WHERE 里带上"无人持有 / 已过期 / 本人续持"，
        由数据库判定归属。原实现无条件覆盖 worker_id —— 调用方虽然在读到
        行之后检查过租约，但检查与写入是两条语句，两个 Worker 同时通过检查
        就都能拿到租约并并发执行同一个 Loop。抢不到时抛
        LoopRunConflictError，调用方据此跳过。
        """
        now = datetime.now(UTC)
        result = await self._session.execute(
            update(LoopRun)
            .where(
                LoopRun.id == loop_id,
                or_(
                    LoopRun.worker_id.is_(None),
                    LoopRun.worker_id == worker_id,
                    LoopRun.lease_expires_at.is_(None),
                    LoopRun.lease_expires_at <= now,
                ),
            )
            .values(
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=duration_seconds),
            )
            # 默认的 synchronize_session="evaluate" 会在 Python 侧重算 WHERE
            # 来更新已加载对象。SQLite 把 DateTime(timezone=True) 读回成
            # naive，与这里 aware 的 now 相比会抛 TypeError —— 判定必须只在
            # 数据库侧发生。下面重新查一次行，identity map 里的过期值随之刷新。
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            # 行不存在与租约被占用要分开：前者是队列残留，后者是并发竞争
            row = await self._by_id(loop_id)
            raise LoopRunConflictError(
                f"Loop {loop_id} 的租约由 {row.worker_id} 持有至 "
                f"{row.lease_expires_at}，{worker_id} 无法接管"
            )
        row = await self._by_id(loop_id)
        # UPDATE 绕过了 ORM 的对象同步，identity map 里的实例还留着旧
        # worker_id/lease_expires_at。不刷新的话调用方读到的是抢锁前的值。
        await self._session.refresh(row)
        return row

    async def extend_lease(
        self, *, loop_id: uuid.UUID, worker_id: str, duration_seconds: int
    ) -> None:
        """续租。长 Loop 处理期间定期调用，防止租约过期被误接管。"""
        row = await self._by_id(loop_id)
        if row.worker_id != worker_id:
            raise LoopRunConflictError(
                f"Loop {loop_id} 的租约已被 {row.worker_id} 持有（当前 {worker_id}）"
            )
        row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=duration_seconds)
        await self._session.flush()

    async def release_lease(self, *, loop_id: uuid.UUID, worker_id: str) -> None:
        """处理完成（或调出后）释放租约。

        只释放自己持有的：原实现无条件清空，于是"租约超时被接管、原
        Worker 随后才走到清理"这条时序里，原 Worker 会把接管者的租约一起
        清掉，Loop 随即被第三个 Worker 认领。不是自己的就静默返回 ——
        释放是清理动作，撞上归属不符时无事可做，报错只会淹没真实原因。
        """
        row = await self._by_id(loop_id)
        if row.worker_id != worker_id:
            return
        row.worker_id = None
        row.lease_expires_at = None
        await self._session.flush()

    async def list_recent(
        self,
        *,
        project_id: uuid.UUID,
        limit: int = 50,
        state: LoopState | None = None,
        mode: str | None = None,
    ) -> list[LoopRun]:
        stmt = select(LoopRun).where(LoopRun.project_id == project_id)
        if state is not None:
            stmt = stmt.where(LoopRun.state == state.value)
        if mode is not None:
            stmt = stmt.where(LoopRun.mode == mode)
        stmt = stmt.order_by(LoopRun.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_idle_runs(
        self,
        *,
        project_id: uuid.UUID | None = None,
        limit: int = 20,
        idle_seconds: int = TERMINAL_WAIT_SECONDS,
    ) -> list[LoopRun]:
        """找可被接管的运行中 Loop：租约过期或从未被认领。

        按"长时间未推进且已过期"排序 —— 最可能崩的优先。

        project_id 显式传入时按租户过滤：生产库 loop_runs 有 RLS，跨租户
        查询必须逐项目设置 ariadne.project_id 变量才能看到行（生产补偿
        扫描见 worker/loop_worker._reconcile）。None 不加过滤 —— 仅限
        SQLite 单测与 owner 连接，app 角色在 PG 下用它只会查到零行。

        HUMAN_PENDING 排除：它在等人工审批，重新入队只是无效往返
        （engine 恢复到 HUMAN_PENDING 后直接返回，不会推进）。

        idle_seconds 是"卡住"的无进展阈值。入队失败的 Loop 永远停在
        VALIDATE 且无租约，补偿扫描靠它把"刚创建还没被认领"与"永远
        不会被认领"区分开。
        """
        lease_floor = datetime.now(UTC) - timedelta(seconds=idle_seconds)
        stmt = (
            select(LoopRun)
            .where(
                LoopRun.final_state.is_(None),
                LoopRun.state != LoopState.HUMAN_PENDING.value,
                LoopRun.lease_expires_at.is_(None)
                | (LoopRun.lease_expires_at < datetime.now(UTC)),
                LoopRun.updated_at < lease_floor,
            )
            .order_by(LoopRun.updated_at.asc())
            .limit(limit)
        )
        if project_id is not None:
            stmt = stmt.where(LoopRun.project_id == project_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def update_metrics(
        self,
        *,
        loop_id: uuid.UUID,
        iteration: int,
        tokens: int,
        cost_usd: float,
    ) -> None:
        row = await self._by_id(loop_id)
        row.iteration = iteration
        row.cumulative_tokens = tokens
        row.cumulative_cost_usd = cost_usd
        await self._session.flush()


def _goal_to_dict(goal: Goal) -> dict[str, Any]:
    """Goal → 可 JSON 序列化 dict。

    与 loop_checkpoint_repo 的序列化同模式：枚举转字符串，
    dataclasses.asdict 转嵌套 dict。
    """
    from dataclasses import asdict

    d = asdict(goal)
    d["assertions"] = [_assertion_to_dict(a) for a in goal.assertions]
    return d


def _assertion_to_dict(assertion: Any) -> dict[str, Any]:
    from dataclasses import asdict

    d = asdict(assertion)
    d["kind"] = assertion.kind.value
    return d


def goal_from_dict(data: dict[str, Any]) -> Goal:
    """dict → Goal。Worker/API 重建用。

    用断言类型的 kind 值反查枚举，保证与创建时一致。
    """
    from ariadne.loop_module.goal import Assertion, AssertionKind, Budget

    budget = data.get("budget", {})
    assertions = []
    for raw in data.get("assertions", []):
        assertions.append(
            Assertion(
                id=raw["id"],
                kind=AssertionKind(raw["kind"]),
                spec=raw.get("spec", {}),
                weight=raw.get("weight", 1.0),
                blocking=raw.get("blocking", True),
                hint=raw.get("hint", ""),
            )
        )
    return Goal(
        task=data["task"],
        assertions=tuple(assertions),
        budget=Budget(
            max_iterations=budget.get("max_iterations", 10),
            max_total_tokens=budget.get("max_total_tokens", 200_000),
            max_cost_usd=budget.get("max_cost_usd", 1.0),
            max_tokens_per_iteration=budget.get("max_tokens_per_iteration", 32_000),
            max_wall_clock_seconds=budget.get("max_wall_clock_seconds", 900),
        ),
        mode=data.get("mode", "quality"),
        stall_threshold=data.get("stall_threshold", 2.0),
        stall_patience=data.get("stall_patience", 2),
        # JSON 没有 tuple，往返后是 list[list[str, str]] —— 转回 tuple
        # 才能保持 Goal 的 frozen 语义（否则 hash/相等比较会炸）
        workspace=tuple(
            (str(path), str(content))
            for path, content in data.get("workspace", ())
        ),
    )


def to_row_dict(row: LoopRun) -> dict[str, Any]:
    """LoopRun → API 响应 dict。"""
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "mode": row.mode,
        "goal": row.goal,
        "state": row.state,
        "iteration": row.iteration,
        "cumulative_tokens": row.cumulative_tokens,
        "cumulative_cost_usd": float(row.cumulative_cost_usd),
        "final_state": row.final_state,
        "worker_id": row.worker_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


__all__ = [
    "LoopRunConflictError",
    "LoopRunNotFoundError",
    "LoopRunRepository",
    "goal_from_dict",
    "to_row_dict",
]
