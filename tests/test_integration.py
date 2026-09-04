"""集成测试：连真实 ClickHouse + Redis，不 mock 数据库。

刻意不 mock 的理由：mock 掉的 SQL 语法错误、类型不匹配、物化视图不刷新
只有真实库能发现。M1 验收清单里的幂等、吞吐、树嵌套三项都靠这里验证。

运行：
    docker compose up -d clickhouse redis
    uv run pytest -m integration
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from ariadne.config import (
    ApiSettings,
    ClickHouseSettings,
    PayloadSettings,
    RedisSettings,
    Settings,
)
from ariadne.storage.clickhouse import ClickHouseStore
from ariadne.storage.queue import SpanQueue
from ariadne.worker.collector import CollectorWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

PID = UUID("00000000-0000-0000-0000-000000000002")
DDL_DIR_NAME = "deploy/clickhouse"


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    payload_dir = tmp_path_factory.mktemp("payloads")
    # Redis 端口跟随配置（ARIADNE_REDIS_URL，compose 里常映射到 6380），
    # 只把库号固定到 15 做测试隔离，避免碰坏开发用的 0 号库。字面写 6379
    # 撞到的是本机原生 Redis 3.0.504 —— 不支持 Streams，XADD 直接报错。
    _redis_host_port = RedisSettings().url.rsplit("/", 1)[0]
    return Settings(
        env="test",
        log_level="WARNING",
        clickhouse=ClickHouseSettings(database="ariadne", batch_max_interval_ms=50),
        redis=RedisSettings(
            url=f"{_redis_host_port}/15",
            stream_key="q:collect:test",
            consumer_group="test-workers",
            visibility_timeout_ms=1000,
        ),
        payload=PayloadSettings(local_dir=str(payload_dir)),
        api=ApiSettings(default_project_id=PID),
    )


@pytest.fixture(scope="module")
def store(settings: Settings, request: pytest.FixtureRequest) -> Iterator[ClickHouseStore]:
    from pathlib import Path

    ch = ClickHouseStore(settings.clickhouse)
    if not ch.ping():
        pytest.skip("ClickHouse 不可用，先 docker compose up -d clickhouse")

    ddl_dir: Path = Path(str(request.config.rootpath)) / DDL_DIR_NAME
    ch.migrate(ddl_dir)
    yield ch
    # 只清本测试项目的数据，不动其他。rollup 是物化视图的目标表（独立物理表），
    # DELETE spans 不会回滚已聚合进去的行，必须逐表清。
    # mutations_sync=1 等 mutation 真正落地：默认 0 是发出即返回，
    # 下一次运行开始时残余还在，聚合值会跨运行累积。
    for table in ("spans", "trace_rollup", "cost_rollup"):
        ch.client.command(
            f"ALTER TABLE {table} DELETE WHERE project_id = {{pid:UUID}}",
            parameters={"pid": PID},
            settings={"mutations_sync": 1},
        )
    ch.close()


@pytest.fixture
async def queue(settings: Settings) -> AsyncIterator[SpanQueue]:
    q = SpanQueue(settings.redis)
    if not await q.ping():
        pytest.skip("Redis 不可用，先 docker compose up -d redis")
    await q.redis.delete(settings.redis.stream_key)
    await q.ensure_group()
    yield q
    await q.redis.delete(settings.redis.stream_key)
    await q.close()


def span_payload(span_id: str, parent: str = "", **extra: Any) -> dict[str, Any]:
    base = {
        "trace_id": "1" * 32,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": f"span-{span_id[:4]}",
        "kind": "llm",
        "provider": "openai",
        "model": "gpt-4o",
        "started_at": datetime.now(UTC).isoformat(),
        "duration_ms": 100,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }
    return {**base, **extra}


async def drain(worker: CollectorWorker, *, timeout: float = 10.0) -> None:
    """跑 worker 直到队列排空。"""
    task = asyncio.create_task(worker.start())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        if worker.stats["written"] > 0 and await worker._queue.pending_count() == 0:
            break
    await worker.stop()
    await asyncio.wait_for(task, timeout=5)


class TestPipeline:
    async def test_end_to_end_write_and_query(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """入队 → 加工 → 落库 → 可查。"""
        trace_id = "a" * 32
        await queue.publish({
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [span_payload("b" * 16, trace_id=trace_id)],
        })

        worker = CollectorWorker(settings)
        await drain(worker)
        await worker.close()

        rows = store.query(
            "SELECT span_id, cost_usd, provider FROM spans "
            "WHERE project_id = {pid:UUID} AND trace_id = {tid:String}",
            {"pid": PID, "tid": trace_id},
        )
        assert len(rows) == 1
        # 成本由服务端计算：1000 in + 500 out on gpt-4o
        assert float(rows[0]["cost_usd"]) == pytest.approx(0.0075, rel=1e-6)

    async def test_replay_is_idempotent(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """同一批投递两次，去重后行数不变（ReplacingMergeTree）。"""
        trace_id = "c" * 32
        batch = {
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [span_payload("d" * 16, trace_id=trace_id)],
        }
        await queue.publish(batch)
        await queue.publish(batch)

        worker = CollectorWorker(settings)
        await drain(worker)
        await worker.close()

        # FINAL 强制去重视图
        rows = store.query(
            "SELECT count() AS c FROM spans FINAL "
            "WHERE project_id = {pid:UUID} AND trace_id = {tid:String}",
            {"pid": PID, "tid": trace_id},
        )
        assert int(rows[0]["c"]) == 1

    async def test_unacked_message_reclaimed(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """模拟 Worker 崩溃：消息未 ACK 时应被另一 Worker 回收。"""
        trace_id = "e" * 32
        await queue.publish({
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [span_payload("f" * 16, trace_id=trace_id)],
        })

        # 第一个消费者读走但不 ACK
        taken = await queue.consume("crashed-worker", count=10, block_ms=500)
        assert len(taken) == 1
        assert await queue.pending_count() == 1

        # 等过可见性超时后回收
        await asyncio.sleep(settings.redis.visibility_timeout_ms / 1000 + 0.2)
        reclaimed = await queue.reclaim("healthy-worker", count=10)
        assert len(reclaimed) == 1
        await queue.ack(reclaimed[0][0])
        assert await queue.pending_count() == 0

    async def test_trace_tree_nesting(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """三层嵌套写入后查回，树结构与耗时聚合正确。"""
        from ariadne.api.tree import build_tree, flatten_tree

        trace_id = "9" * 32
        root, mid, leaf = "1" * 16, "2" * 16, "3" * 16
        await queue.publish({
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [
                span_payload(root, trace_id=trace_id, duration_ms=300, kind="internal"),
                span_payload(mid, root, trace_id=trace_id, duration_ms=200),
                span_payload(leaf, mid, trace_id=trace_id, duration_ms=50),
            ],
        })

        worker = CollectorWorker(settings)
        await drain(worker)
        await worker.close()

        rows = store.query(
            """
            SELECT span_id, parent_span_id, name, kind, operation, status, error_type,
                   provider, model_request, model_response, started_at, duration_ms,
                   input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   reasoning_tokens, cost_usd, input_preview, output_preview,
                   input_ref, output_ref, attributes, tags, loop_id, iteration
            FROM spans FINAL
            WHERE project_id = {pid:UUID} AND trace_id = {tid:String}
            """,
            {"pid": PID, "tid": trace_id},
        )
        assert len(rows) == 3

        roots = build_tree(rows)
        assert len(roots) == 1
        assert roots[0].span_id == root
        assert roots[0].self_ms == 100  # 300 - 200
        assert len(flatten_tree(roots)) == 3

    async def test_materialized_view_rollup(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """物化视图应自动聚合，trace 列表查询依赖它。

        trace_id 每次运行唯一：trace_rollup 是 AggregatingMergeTree，对它做
        绝对值断言时，固定 trace_id 会把历次运行的 span_count 累加起来
        （实测重跑三次得 15 而非 5）。唯一 trace_id 让断言与残余数据无关，
        比依赖 teardown 清理时序更可靠。
        """
        trace_id = uuid4().hex
        await queue.publish({
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [span_payload(f"{i:016x}", trace_id=trace_id) for i in range(5)],
        })

        worker = CollectorWorker(settings)
        await drain(worker)
        await worker.close()

        rows = store.query(
            "SELECT sum(span_count) AS c, sum(total_cost_usd) AS cost "
            "FROM trace_rollup WHERE project_id = {pid:UUID} AND trace_id = {tid:String}",
            {"pid": PID, "tid": trace_id},
        )
        assert int(rows[0]["c"]) == 5


class TestProductionQueries:
    """直接跑生产端点的查询，不重写等价 SQL。

    **为什么必须连真 ClickHouse**：`tests/test_api.py` 用 `FakeStore`，它的
    `query()` 只回固定行、从不解析 SQL，所以任何"Python 侧通过、ClickHouse
    侧语法错"的查询都能全绿混过去。`list_traces` 就这样带着
    `ILLEGAL_AGGREGATION(184)` 上线过：`min(started_at) AS started_at` 的别名
    在同一 SELECT 里优先于列名，把 `dateDiff` 里的 `min(started_at)` 解析成
    `min(min(started_at))`，端点对真库必然 500，而 1883 个测试无一发现。

    所以这里调**真实 handler**而不是照抄 SQL：照抄的副本会和生产漂移，
    漂移之后测的就不是上线的那段查询了。
    """

    async def test_list_traces_handler_runs_against_clickhouse(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        from ariadne.api.routers.traces import list_traces
        from ariadne.auth.rbac import Role
        from ariadne.auth.tenant import TenantContext

        trace_id = uuid4().hex
        await queue.publish({
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [span_payload(f"{i:016x}", trace_id=trace_id) for i in range(3)],
        })
        worker = CollectorWorker(settings)
        await drain(worker)
        await worker.close()

        ctx = TenantContext(project_id=PID, role=Role.DEVELOPER)
        summaries = await list_traces(ctx=ctx, store=store, limit=50)

        found = [s for s in summaries if s.trace_id == trace_id]
        assert found, f"生产查询跑通但没返回刚写入的 trace {trace_id}"
        assert found[0].span_count == 3
        # duration_ms 是出问题那个表达式的产物，必须断言它真的算出来了
        assert found[0].duration_ms >= 0

    async def test_cursor_does_not_truncate_straddling_trace(
        self, store: ClickHouseStore
    ) -> None:
        """游标筛的是 trace 起始时刻，不能把跨游标的 trace 截断。

        **必须造出「同一 trace 有多行未合并 rollup」的状态**才能区分两种实现：
        trace_rollup 是 AggregatingMergeTree，一次插入只产生一行，而单行时
        `WHERE started_at < X` 与 `HAVING min(started_at) < X` 完全等价 ——
        用单批插入写的测试对两种实现都通过（试过，确实不报错）。
        分两次 insert_spans 才会产生两行，此时才有区别：
        - HAVING 筛聚合后的 min：整条 trace 入选，span_count = 2
        - WHERE 筛原始行：游标后那行被丢掉，span_count = 1

        断言方向是单边的：合并一旦发生，两种实现都得 2，所以这条测试
        **不会误报**（有 fix 时恒过），但在后台合并抢先时可能漏报。
        """
        from ariadne.api.routers.traces import list_traces
        from ariadne.auth.rbac import Role
        from ariadne.auth.tenant import TenantContext
        from ariadne.telemetry.models import AriadneSpan

        base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        trace_id = uuid4().hex

        def span(span_id: str, offset_s: int) -> AriadneSpan:
            return AriadneSpan(
                project_id=PID,
                trace_id=trace_id,
                span_id=span_id,
                name="straddle",
                started_at=(base + timedelta(seconds=offset_s)).isoformat(),
                duration_ms=10,
            )

        # 两次独立插入 → 两行 rollup（min=T 与 min=T+20s）
        store.insert_spans([span("c" * 16, 0)])
        store.insert_spans([span("d" * 16, 20)])

        ctx = TenantContext(project_id=PID, role=Role.DEVELOPER)
        cursor = base + timedelta(seconds=15)
        summaries = await list_traces(ctx=ctx, store=store, before=cursor, limit=200)

        found = [s for s in summaries if s.trace_id == trace_id]
        assert found, f"游标 {cursor} 晚于 trace 起始 {base}，应当入选"
        assert found[0].span_count == 2, (
            f"span_count={found[0].span_count}，期望 2 —— 游标把跨界的 trace 截断了，"
            "说明筛选跑在聚合前（WHERE）而不是聚合后（HAVING）"
        )

    async def test_cursor_excludes_trace_started_after_it(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """游标早于 trace 起始时刻 → 该 trace 必须被排除。

        与上一条互为对照：只测"不截断"会让一个恒真的 HAVING 也通过。
        """
        from ariadne.api.routers.traces import list_traces
        from ariadne.auth.rbac import Role
        from ariadne.auth.tenant import TenantContext

        base = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
        trace_id = uuid4().hex
        await queue.publish({
            "format": "native",
            "project_id": str(PID),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": [span_payload("a" * 16, trace_id=trace_id, started_at=base.isoformat())],
        })
        worker = CollectorWorker(settings)
        await drain(worker)
        await worker.close()

        ctx = TenantContext(project_id=PID, role=Role.DEVELOPER)
        summaries = await list_traces(
            ctx=ctx, store=store, before=base - timedelta(seconds=5), limit=200
        )
        assert all(s.trace_id != trace_id for s in summaries)

    async def test_only_errors_filters_on_aggregate(
        self, settings: Settings, store: ClickHouseStore
    ) -> None:
        """`only_errors` 走 HAVING sum(error_count) > 0，要能过真库且真的筛。"""
        from ariadne.api.routers.traces import list_traces
        from ariadne.auth.rbac import Role
        from ariadne.auth.tenant import TenantContext

        ctx = TenantContext(project_id=PID, role=Role.DEVELOPER)
        errored = await list_traces(ctx=ctx, store=store, only_errors=True, limit=200)
        assert all(s.error_count > 0 for s in errored)

    async def test_only_errors_combined_with_cursor(
        self, settings: Settings, store: ClickHouseStore
    ) -> None:
        """两个条件同时给 —— 拼 HAVING 时容易漏掉 AND。"""
        from ariadne.api.routers.traces import list_traces
        from ariadne.auth.rbac import Role
        from ariadne.auth.tenant import TenantContext

        ctx = TenantContext(project_id=PID, role=Role.DEVELOPER)
        rows = await list_traces(
            ctx=ctx, store=store, only_errors=True, before=datetime.now(UTC), limit=200
        )
        assert all(s.error_count > 0 for s in rows)


class TestThroughput:
    @pytest.mark.slow
    async def test_worker_throughput(
        self, settings: Settings, store: ClickHouseStore, queue: SpanQueue
    ) -> None:
        """M1 验收：单实例 ≥ 5000 spans/s。

        这里测的是"加工 + 落库"环节，不含 HTTP 开销。
        """
        total_batches, per_batch = 20, 500
        trace_id = "5" * 32
        for batch_index in range(total_batches):
            await queue.publish({
                "format": "native",
                "project_id": str(PID),
                "received_at": datetime.now(UTC).isoformat(),
                "payload": [
                    span_payload(f"{batch_index * per_batch + i:016x}", trace_id=trace_id)
                    for i in range(per_batch)
                ],
            })

        expected = total_batches * per_batch
        worker = CollectorWorker(settings)
        task = asyncio.create_task(worker.start())
        start = time.monotonic()
        # 不用 drain()：它每 200ms 轮询一次 Redis pending_count，于是 elapsed 里
        # 掺进最多 200ms 的"发现队列排空"延迟（本量级上最多 9% 的系统性低估），
        # 而这条断言声称测的是"加工 + 落库"。盯内存计数器：零 IO，不与 worker
        # 抢 Redis，停表点就是最后一条落库的时刻。
        deadline = time.monotonic() + 60
        while worker.stats["written"] < expected and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        elapsed = time.monotonic() - start
        written = worker.stats["written"]
        await worker.stop()
        await asyncio.wait_for(task, timeout=5)
        await worker.close()

        rate = written / elapsed if elapsed else 0
        print(f"\n吞吐: {written} spans / {elapsed:.2f}s = {rate:.0f} spans/s")
        assert written == expected
        assert rate >= 5000, f"吞吐未达标: {rate:.0f} spans/s"
