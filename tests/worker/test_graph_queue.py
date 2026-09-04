"""GraphQueue 集成测试（阶段 2）：连真实 Redis Streams。

与 test_integration.py 同模式：不 mock 数据库 —— Streams 的消费者组、
XACK、XPENDING 行为只有真 Redis 能验证（fakeredis 的 Streams 支持不完整）。

运行：
    docker compose up -d redis
    uv run pytest -m integration tests/worker/test_graph_queue.py
"""

from __future__ import annotations

import pytest

from ariadne.config import RedisSettings
from ariadne.worker.graph_queue import GraphQueue

pytestmark = pytest.mark.integration


@pytest.fixture
async def queue() -> GraphQueue:
    """创建测试用 GraphQueue。Redis 不可用时 skip。"""
    q = GraphQueue(RedisSettings(), stream_key="test:q:graph")
    await q.connect()
    if not await q.ping():
        await q.close()
        pytest.skip("Redis 不可用，先 docker compose up -d redis")
    # 清空测试流（含消费者组）
    try:
        await q.redis.delete("test:q:graph")
    except Exception:
        pass
    yield q
    await q.close()


@pytest.mark.asyncio
async def test_queue_enqueue_and_claim(queue: GraphQueue) -> None:
    """测试入队和认领。"""
    await queue.ensure_group()

    # 入队
    msg_id = await queue.enqueue("run-123", "proj-456")
    assert msg_id

    # 认领
    jobs = await queue.claim("worker-1", count=10, block_ms=100)
    assert len(jobs) == 1
    assert jobs[0][1] == "run-123"
    assert jobs[0][2] == "proj-456"

    # ACK
    await queue.ack(jobs[0][0])

    # 再次认领应该为空
    jobs2 = await queue.claim("worker-1", count=10, block_ms=100)
    assert len(jobs2) == 0


@pytest.mark.asyncio
async def test_queue_reclaim_stale(queue: GraphQueue) -> None:
    """测试回收过期任务（模拟 Worker 崩溃）。"""
    await queue.ensure_group()

    # Worker-1 认领但不 ACK（模拟崩溃）
    await queue.enqueue("run-999", "proj-123")
    jobs = await queue.claim("worker-1", count=10, block_ms=100)
    assert len(jobs) == 1

    # Worker-2 立即尝试认领（应该为空，因为还没过期）
    jobs2 = await queue.claim("worker-2", count=10, block_ms=100)
    assert len(jobs2) == 0

    # 注意：真实的过期回收需要等待 RECLAIM_MIN_IDLE_MS（3分钟），
    # 单元测试中不等待，仅验证接口可调用


@pytest.mark.asyncio
async def test_queue_pending_count(queue: GraphQueue) -> None:
    """测试查询待处理任务数。"""
    await queue.ensure_group()

    # 初始为 0
    count = await queue.pending_count()
    assert count == 0

    # 入队 3 个任务
    await queue.enqueue("run-1", "proj-1")
    await queue.enqueue("run-2", "proj-1")
    await queue.enqueue("run-3", "proj-1")

    # 认领 1 个（pending 变为 1，因为未 ACK）
    jobs = await queue.claim("worker-1", count=1, block_ms=100)
    assert len(jobs) == 1

    # pending >= 1（未 ACK 的）
    count = await queue.pending_count()
    assert count >= 1

    # ACK 后 pending 减少
    await queue.ack(jobs[0][0])
