"""graph_runs 仓储。

Graph 执行 Job 的状态管理和结果持久化。
阶段 2：增加租约机制（begin_lease / extend_lease / release_lease）。
阶段 3：增加取消机制（cancel / is_cancelled）和检查点恢复（save_checkpoint）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.graph_models import GraphRun


class GraphRunNotFoundError(LookupError):
    """Graph run 不存在。"""


class GraphRunRepository:
    """Graph 运行实例仓储（阶段 3：支持检查点恢复）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """暴露 session 给 Worker（用于在执行过程中保存检查点）。"""
        return self._session

    async def create(
        self,
        project_id: uuid.UUID,
        graph_id: uuid.UUID,
        inputs: dict[str, Any],
    ) -> uuid.UUID:
        """创建 graph_run 记录，初始状态 PENDING。"""
        run = GraphRun(
            id=uuid.uuid4(),
            project_id=project_id,
            graph_id=graph_id,
            state="PENDING",
            inputs=inputs,
            outputs=None,
            node_states=None,
            checkpoint=None,
            errors=None,
            created_at=datetime.now(UTC),
            finished_at=None,
            updated_at=datetime.now(UTC),
        )
        self._session.add(run)
        await self._session.flush()
        return run.id

    async def get(
        self,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> GraphRun:
        """查询 graph_run（需要 project_id 用于 RLS）。"""
        stmt = select(GraphRun).where(
            GraphRun.id == graph_run_id,
            GraphRun.project_id == project_id,
        )
        result = await self._session.execute(stmt)
        run = result.scalar_one_or_none()
        if run is None:
            raise GraphRunNotFoundError(f"Graph run 不存在: {graph_run_id}")
        return run

    async def by_id(self, graph_run_id: uuid.UUID) -> GraphRun:
        """按 id 查询（Worker 用，不限定 project_id）。"""
        stmt = select(GraphRun).where(GraphRun.id == graph_run_id)
        result = await self._session.execute(stmt)
        run = result.scalar_one_or_none()
        if run is None:
            raise GraphRunNotFoundError(f"Graph run 不存在: {graph_run_id}")
        return run

    async def transition(
        self,
        graph_run_id: uuid.UUID,
        to_state: str,
    ) -> None:
        """状态转移（PENDING → RUNNING → COMPLETED/FAILED/TIMEOUT）。"""
        stmt = (
            update(GraphRun)
            .where(GraphRun.id == graph_run_id)
            .values(state=to_state, updated_at=datetime.now(UTC))
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def finish(
        self,
        graph_run_id: uuid.UUID,
        final_state: str,
        outputs: dict[str, Any] | None,
        node_states: dict[str, str],
        errors: list[str],
    ) -> None:
        """标记终态并写入结果。"""
        stmt = (
            update(GraphRun)
            .where(GraphRun.id == graph_run_id)
            .values(
                state=final_state,
                outputs=outputs,
                node_states=node_states,
                errors=errors,
                finished_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def list_runs(
        self,
        project_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GraphRun]:
        """列出项目的 graph runs（按创建时间倒序）。"""
        stmt = (
            select(GraphRun)
            .where(GraphRun.project_id == project_id)
            .order_by(GraphRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ==================== 阶段 2：租约机制 ====================

    async def begin_lease(
        self,
        graph_run_id: uuid.UUID,
        worker_id: str,
        lease_duration_s: int = 300,
    ) -> bool:
        """原子认领租约（PENDING → RUNNING，设置 worker_id 和 lease_expires_at）。

        返回 True 表示成功认领，False 表示已被其他 Worker 认领。
        """
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=lease_duration_s)

        stmt = (
            update(GraphRun)
            .where(
                GraphRun.id == graph_run_id,
                GraphRun.state == "PENDING",
                GraphRun.worker_id.is_(None),  # 未被认领
            )
            .values(
                state="RUNNING",
                worker_id=worker_id,
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0)) > 0

    async def extend_lease(
        self,
        graph_run_id: uuid.UUID,
        worker_id: str,
        lease_duration_s: int = 300,
    ) -> bool:
        """续约（延长 lease_expires_at）。

        只有当前持有租约的 Worker 才能续约。
        返回 True 表示续约成功，False 表示租约已失效或被其他 Worker 持有。
        """
        now = datetime.now(UTC)
        new_lease_expires_at = now + timedelta(seconds=lease_duration_s)

        stmt = (
            update(GraphRun)
            .where(
                GraphRun.id == graph_run_id,
                GraphRun.worker_id == worker_id,
                GraphRun.state == "RUNNING",
            )
            .values(
                lease_expires_at=new_lease_expires_at,
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0)) > 0

    async def release_lease(
        self,
        graph_run_id: uuid.UUID,
        worker_id: str,
    ) -> None:
        """释放租约（清空 worker_id 和 lease_expires_at）。

        用于 Worker 主动放弃任务（如检测到取消标志）。
        """
        stmt = (
            update(GraphRun)
            .where(
                GraphRun.id == graph_run_id,
                GraphRun.worker_id == worker_id,
            )
            .values(
                worker_id=None,
                lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def find_expired_leases(self, limit: int = 50) -> list[GraphRun]:
        """查找租约过期的运行中任务（用于补偿扫描）。

        返回 state=RUNNING 且 lease_expires_at < now 的任务。
        比较交给数据库（NOW()）而非 Python datetime：SQLite/Postgres 的
        时区语义不同，Python 侧 aware 与库内 naive 比较会 TypeError。
        """
        stmt = (
            select(GraphRun)
            .where(
                GraphRun.state == "RUNNING",
                GraphRun.lease_expires_at < func.now(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def reclaim_expired_lease(
        self,
        graph_run_id: uuid.UUID,
        new_worker_id: str,
        lease_duration_s: int = 300,
    ) -> bool:
        """回收过期租约（用于补偿扫描）。

        将过期的租约重新分配给新 Worker。
        返回 True 表示成功回收，False 表示租约已被其他 Worker 更新。
        """
        now = datetime.now(UTC)
        new_lease_expires_at = now + timedelta(seconds=lease_duration_s)

        stmt = (
            update(GraphRun)
            .where(
                GraphRun.id == graph_run_id,
                GraphRun.state == "RUNNING",
                GraphRun.lease_expires_at < func.now(),  # 确保仍然过期
            )
            .values(
                worker_id=new_worker_id,
                lease_expires_at=new_lease_expires_at,
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0)) > 0

    # ==================== 阶段 3：取消机制 ====================

    async def cancel(self, graph_run_id: uuid.UUID) -> bool:
        """标记 graph_run 为已取消。

        设置 cancelled_at 时间戳，Worker 会检测此字段并优雅退出。
        只能取消 PENDING 或 RUNNING 状态的任务。
        返回 True 表示成功标记，False 表示任务已完成或不存在。
        """
        now = datetime.now(UTC)
        stmt = (
            update(GraphRun)
            .where(
                GraphRun.id == graph_run_id,
                GraphRun.state.in_(["PENDING", "RUNNING"]),
                GraphRun.cancelled_at.is_(None),  # 避免重复取消
            )
            .values(
                cancelled_at=now,
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0)) > 0

    async def is_cancelled(self, graph_run_id: uuid.UUID) -> bool:
        """检查 graph_run 是否被取消。

        Worker 在执行过程中定期调用此方法检查取消标志。
        """
        stmt = select(GraphRun.cancelled_at).where(GraphRun.id == graph_run_id)
        result = await self._session.execute(stmt)
        cancelled_at = result.scalar_one_or_none()
        return cancelled_at is not None

    # ==================== 阶段 3：检查点恢复 ====================

    async def save_checkpoint(
        self,
        graph_run_id: uuid.UUID,
        node_states: dict[str, str],
        node_outputs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """保存节点执行检查点（阶段 3-2）。

        每个节点完成后调用，同时更新兼容状态索引 ``node_states`` 和恢复
        载荷 ``checkpoint``。只有状态而没有输出不足以恢复数据流：下游节点
        仍需要读取已完成上游节点的端口值。

        Args:
            graph_run_id: Graph run ID
            node_states: 节点状态字典 {node_id: "completed" | "failed" | "skipped"}
            node_outputs: 已完成节点的输出 {node_id: {port_name: value}}。传 None
                时只更新状态，兼容旧调用方。
        """
        values: dict[str, Any] = {
            "node_states": dict(node_states),
            "updated_at": datetime.now(UTC),
        }
        if node_outputs is not None:
            values["checkpoint"] = {
                "completed_nodes": [
                    node_id
                    for node_id, state in node_states.items()
                    if state == "completed"
                ],
                "node_outputs": {
                    node_id: dict(output)
                    for node_id, output in node_outputs.items()
                },
            }

        stmt = (
            update(GraphRun)
            .where(GraphRun.id == graph_run_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_checkpoint(
        self,
        graph_run_id: uuid.UUID,
    ) -> dict[str, str] | None:
        """读取检查点（恢复时使用）。

        返回 node_states 或 None（无检查点）。
        """
        stmt = select(GraphRun.node_states).where(GraphRun.id == graph_run_id)
        result = await self._session.execute(stmt)
        node_states = result.scalar_one_or_none()
        return node_states if node_states else None

    async def get_checkpoint_outputs(
        self,
        graph_run_id: uuid.UUID,
    ) -> dict[str, dict[str, Any]] | None:
        """读取检查点中的节点输出，兼容无 ``checkpoint`` 的旧记录。"""
        stmt = select(GraphRun.checkpoint).where(GraphRun.id == graph_run_id)
        result = await self._session.execute(stmt)
        checkpoint = result.scalar_one_or_none()
        if not isinstance(checkpoint, dict):
            return None

        raw_outputs = checkpoint.get("node_outputs")
        if not isinstance(raw_outputs, dict):
            return None

        outputs: dict[str, dict[str, Any]] = {}
        for node_id, output in raw_outputs.items():
            if isinstance(node_id, str) and isinstance(output, dict):
                outputs[node_id] = dict(output)
        return outputs or None


__all__ = ["GraphRunNotFoundError", "GraphRunRepository"]
