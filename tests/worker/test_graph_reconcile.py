"""GraphWorkerV2 补偿扫描回归测试。

覆盖两个曾被单测遗漏的生产语义：扫描要在租户会话中读到 graph_runs，且
回收必须是条件 UPDATE 后直接执行，而不是只重复写一条 Redis 消息。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from ariadne.storage.postgres.graph_models import GraphRow
from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository
from ariadne.worker.graph_worker_v2 import GraphWorkerV2


@pytest.mark.asyncio
async def test_reconcile_reclaims_and_executes_expired_run(
    memory_pg,
    settings,
    fake_queue,
) -> None:
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

    async with memory_pg.session() as session:
        session.add(
            GraphRow(
                id=graph_id,
                project_id=project_id,
                name="reconcile-graph",
                version=1,
                graph={"version": "1", "graph": {"version": "1", "nodes": [], "edges": []}},
                validation_errors=[],
                is_active=True,
                description="",
            )
        )
        repo = GraphRunRepository(session)
        run_id = await repo.create(project_id, graph_id, {})
        await repo.begin_lease(run_id, "crashed-worker", lease_duration_s=1)
        await session.execute(
            text(
                """
                UPDATE graph_runs
                SET lease_expires_at = :expired
                WHERE id = :run_id
                """
            ),
            {
                "run_id": str(run_id),
                "expired": datetime.now(UTC).replace(tzinfo=None)
                - timedelta(minutes=5),
            },
        )

    worker = GraphWorkerV2(memory_pg, settings, fake_queue)  # type: ignore[arg-type]
    await worker._reconcile()

    # _reconcile schedules the reclaimed execution; wait for that task without
    # relying on a fixed sleep window.
    tasks = list(worker._tasks)
    if tasks:
        await asyncio.gather(*tasks)

    async with memory_pg.session() as session:
        run = await GraphRunRepository(session).by_id(run_id)
        assert run.state == "COMPLETED"

    # FakeQueue intentionally has no enqueue() method. Reaching here proves the
    # path did not fall back to the old duplicate-message implementation.
    assert fake_queue.published == []
    await worker.shutdown()
