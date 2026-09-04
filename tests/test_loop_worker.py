"""Loop Worker 测试。

验证 Worker 的编排逻辑：
- 消费队列 → 认领租约 → 执行 engine → 终态落库
- 崩溃接管（租约过期后另一 Worker 接管）
- 租约未过期时跳过（不双跑）
- 从检查点恢复（不重跑已完成轮次）

不用真实 Redis：Queue 用假桩。不用真实 Postgres：用共享内存 SQLite
（同一 engine 所有 session 共享一个库）。engine 用 ScriptedLLM 桩。
这保证测试可离线、快速、确定性 —— 队列/LLM 是编排细节，
Worker 的正确性不依赖它们的真实实现。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ariadne.loop_module.budget import InMemoryCounter
from ariadne.loop_module.engine import LLMResponse
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.state_machine import LoopState
from ariadne.loop_module.tools import WorkspaceToolExecutor
from ariadne.storage.postgres.loop_models import LoopRun
from ariadne.storage.postgres.models import Base
from ariadne.storage.postgres.repositories.loop_runs import LoopRunRepository
from ariadne.worker.loop_worker import LoopWorker

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

_ENGINE_URL = "sqlite+aiosqlite:///file:loop_worker_test?mode=memory&cache=shared&uri=true"


# ---------- 桩 ----------


@dataclass
class FakeLoopQueue:
    """假队列：预设待处理任务，无需 Redis。"""

    loop_ids: list[str] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)
    # 载荷里的 project_id。空串模拟队列中的旧条目（无租户上下文）
    project_id: str = str(PROJECT_ID)

    async def connect(self) -> None: ...
    async def ensure_group(self) -> None: ...
    async def ping(self) -> bool:
        return True

    async def claim(self, consumer: str) -> list[tuple[str, str, str]]:
        if not self.loop_ids:
            await asyncio.sleep(0.01)
            return []
        ids, self.loop_ids = self.loop_ids, []
        self.claimed.extend(ids)
        # 模拟消息 ID：message-<loop_id>
        return [(f"msg-{loop_id}", loop_id, self.project_id) for loop_id in ids]

    async def ack(self, message_id: str) -> None:
        self.acked.append(message_id)

    async def close(self) -> None: ...


@dataclass
class ScriptedLLM:
    """按脚本输出；claimed_done 可独立设置。"""

    outputs: list[str]
    claimed_done: list[bool] | None = None
    calls: int = 0

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        idx = self.calls
        self.calls += 1
        output = self.outputs[idx] if idx < len(self.outputs) else self.outputs[-1]
        claimed = (
            self.claimed_done[idx]
            if self.claimed_done and idx < len(self.claimed_done)
            else False
        )
        return LLMResponse(
            output=output,
            input_tokens=100,
            output_tokens=200,
            claimed_done=claimed,
            model=model,
            cost_usd=Decimal("0.01"),
        )


# ---------- 共享内存 SQLite pg ----------


class SharedMemoryPg:
    """PostgresStore 同接口的内存实现（共享库）。"""

    def __init__(self, engine: Any, maker: Any) -> None:
        self._engine = engine
        self._maker = maker

    @property
    def engine(self) -> Any:
        return self._engine

    @property
    def session_maker(self) -> Any:
        return self._maker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def ping(self) -> bool:
        return True

    async def close(self) -> None: ...


@pytest.fixture
async def pg_setup() -> AsyncIterator[tuple[Any, Any]]:
    """准备共享内存 SQLite 与完整表。返回 (engine, maker)。

    共享内存库（cache=shared）在文件内测试间持久，故 org/project
    仅在首次创建；后续 fixture 调用跳过种子数据。
    """
    from sqlalchemy import select

    from ariadne.storage.postgres import harness_models  # noqa: F401 — register tables
    from ariadne.storage.postgres.models import Organization, Project

    engine = create_async_engine(_ENGINE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        existing = await sess.execute(
            select(Organization).where(Organization.id == ORG_ID)
        )
        if existing.scalars().first() is None:
            sess.add(Organization(id=ORG_ID, name="test-org"))
            sess.add(Project(id=PROJECT_ID, org_id=ORG_ID, slug="t", name="Test"))
            await sess.commit()
    yield engine, maker
    await engine.dispose()


def make_pg_factory(engine: Any, maker: Any) -> Any:
    """构造 LoopWorker 的 pg_factory：任何 settings 都返回同一共享实例。"""

    def factory(settings: Any) -> SharedMemoryPg:
        return SharedMemoryPg(engine, maker)

    return factory


def make_goal(*, mode: str = "quality") -> Goal:
    return Goal(
        task="写一个返回两数之和的 Python 函数",
        assertions=(
            Assertion(
                id="has_def",
                kind=AssertionKind.REGEX,
                spec={"pattern": r"def\s+\w+\s*\("},
                hint="输出必须包含一个函数定义",
            ),
        ),
        budget=Budget(max_iterations=5, max_total_tokens=50_000),
        mode=mode,  # type: ignore[arg-type]
    )


def make_counter_factory() -> Any:
    """InMemoryCounter 工厂。"""

    def factory(redis_url: str) -> InMemoryCounter:
        return InMemoryCounter()

    return factory


async def create_loop_row(maker: Any, goal: Goal, *, mode: str = "quality") -> uuid.UUID:
    async with maker() as sess:
        repo = LoopRunRepository(sess)
        loop_id = await repo.create(project_id=PROJECT_ID, mode=mode, goal=goal)
        await sess.commit()
    return loop_id


async def get_row(maker: Any, loop_id: uuid.UUID) -> LoopRun:
    async with maker() as sess:
        return await LoopRunRepository(sess).by_id(loop_id)


# ---------- 测试 ----------


class TestWorkerExecution:
    async def test_converged_loop_finalizes(self, pg_setup: Any) -> None:
        """正常收敛：终态落库 + final_state=CONVERGED。"""
        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.state == LoopState.CONVERGED.value
        assert row.final_state == LoopState.CONVERGED.value
        assert row.iteration == 1
        assert row.cumulative_tokens == 300
        assert row.worker_id is None

    async def test_acked_after_success(self, pg_setup: Any) -> None:
        """处理成功后 ACK 消息，避免被回收重放。"""
        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        queue = FakeLoopQueue(loop_ids=[str(loop_id)])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=queue,
        )
        await worker._poll()
        assert queue.acked == [f"msg-{loop_id}"]

    async def test_missing_project_id_skipped_and_acked(self, pg_setup: Any) -> None:
        """载荷缺 project_id：跳过并 ACK，不留 pending 堵回收通道。

        没有租户上下文就设不了 RLS 变量，读 loop_runs 必然空。留着 pending
        会永占队头，每 90s 重试一次且永不成功。
        """
        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        queue = FakeLoopQueue(loop_ids=[str(loop_id)], project_id="")
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=queue,
        )
        await worker._poll()
        assert queue.acked == [f"msg-{loop_id}"]
        assert worker._stats["skipped"] == 1
        assert worker._stats["completed"] == 0
        # loop 行没被动过：状态仍是初始态，等 resume 重新入队
        row = await get_row(maker, loop_id)
        assert row.worker_id is None

    async def test_claimed_counted(self, pg_setup: Any) -> None:
        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        queue = FakeLoopQueue(loop_ids=[str(loop_id)])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=queue,
        )
        await worker._poll()
        assert "completed" in worker._stats
        assert queue.claimed == [str(loop_id)]

    async def test_stalled_loop_finalizes_stalled(self, pg_setup: Any) -> None:
        """振荡 → STALLED 终态。"""
        engine, maker = pg_setup
        # 反复输出同一错误内容 → 振荡检测触发
        goal = Goal(
            task="写函数",
            assertions=(
                Assertion(
                    id="has_def",
                    kind=AssertionKind.REGEX,
                    spec={"pattern": r"def\s+\w+\s*\("},
                ),
            ),
            budget=Budget(max_iterations=6, max_total_tokens=50_000),
            mode="quality",
            stall_patience=2,
        )
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["no function here", "no function here", "no function here"])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.final_state in (LoopState.STALLED.value,)

    async def test_skips_loop_with_fresh_lease(self, pg_setup: Any) -> None:
        """租约未过期 → 跳过（不双跑）。"""
        engine, maker = pg_setup
        loop_id = await create_loop_row(maker, make_goal())
        async with maker() as sess:
            await LoopRunRepository(sess).begin_lease(
                loop_id=loop_id, worker_id="other-worker", duration_seconds=60
            )
            await sess.commit()

        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        # 未执行：LLM 未被调用，状态未变
        assert llm.calls == 0
        assert worker._stats["skipped"] == 1
        row = await get_row(maker, loop_id)
        assert row.state == LoopState.VALIDATE.value

    async def test_expired_lease_taken_over(self, pg_setup: Any) -> None:
        """租约过期 → 可被接管（崩溃恢复）。"""
        engine, maker = pg_setup
        loop_id = await create_loop_row(maker, make_goal())
        async with maker() as sess:
            await LoopRunRepository(sess).begin_lease(
                loop_id=loop_id, worker_id="dead-worker", duration_seconds=60
            )
            row = await LoopRunRepository(sess).by_id(loop_id)
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=100)
            await sess.commit()

        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.final_state == LoopState.CONVERGED.value
        assert row.worker_id is None

    async def test_skips_terminal_loop(self, pg_setup: Any) -> None:
        """已终态的 Loop 不重复处理。"""
        engine, maker = pg_setup
        loop_id = await create_loop_row(maker, make_goal())
        async with maker() as sess:
            await LoopRunRepository(sess).finish(
                loop_id=loop_id, final_state=LoopState.CONVERGED, worker_id="w1"
            )
            await sess.commit()

        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings(),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        assert llm.calls == 0
        assert worker._stats["skipped"] == 1


class TestPollConcurrency:
    """一轮 claim 拿到多个任务时必须并发处理。

    这不是性能测试。队列一次认领 `_MAX_CLAIM=3` 个，未 ACK 消息 idle 超过
    `RECLAIM_MIN_IDLE_MS`（90s）就被别的 Worker 回收，而单个 Loop 要跑几
    分钟。串行处理时队头之后的任务纯等待自己的回收期限到达 —— 它们还没拿
    Postgres 租约，接管者能干净地获得租约并**并发执行同一个 Loop**。

    断言用"执行是否重叠"而不是"Semaphore 是否存在"：后者在 gather 被写回
    串行 for 循环时照样通过。
    """

    async def test_claimed_loops_run_concurrently(self, pg_setup: Any) -> None:
        engine, maker = pg_setup
        ids = [await create_loop_row(maker, make_goal()) for _ in range(3)]

        in_flight = 0
        peak = 0

        class OverlapProbeLLM:
            """记录同一时刻有几个 Loop 在等 LLM。"""

            calls = 0

            async def complete(self, prompt: str, *, model: str) -> LLMResponse:
                nonlocal in_flight, peak
                type(self).calls += 1
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    # 等同伴进来（最多 1s）：有重叠就立刻走，比固定 sleep 更
                    # 确定的交错 —— 固定窗口在重载机器上可能被错过（先到者
                    # 退出时后来者还没进来），把并发误判成串行。串行实现里
                    # in_flight 永远是 1，等待只能把总耗时拉长到超时。
                    for _ in range(200):
                        if in_flight > 1:
                            break
                        await asyncio.sleep(0.005)
                finally:
                    in_flight -= 1
                return LLMResponse(
                    output="def add(a, b): return a + b",
                    input_tokens=100,
                    output_tokens=200,
                    claimed_done=False,
                    model=model,
                    cost_usd=Decimal("0.01"),
                )

        worker = LoopWorker(
            settings=make_settings(loop_concurrency=3),
            llm=OverlapProbeLLM(),  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(i) for i in ids]),
        )
        await worker._poll()

        assert peak > 1, f"三个 Loop 仍在串行执行（峰值并发 {peak}）"
        for loop_id in ids:
            assert (await get_row(maker, loop_id)).final_state == (
                LoopState.CONVERGED.value
            )

    async def test_concurrency_respects_configured_limit(self, pg_setup: Any) -> None:
        """并发上限是配置项，不是写死的数字 —— 每项都占着 LLM 连接与工作目录。"""
        engine, maker = pg_setup
        ids = [await create_loop_row(maker, make_goal()) for _ in range(4)]

        in_flight = 0
        peak = 0

        class OverlapProbeLLM:
            async def complete(self, prompt: str, *, model: str) -> LLMResponse:
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    for _ in range(200):
                        if in_flight > 1:
                            break
                        await asyncio.sleep(0.005)
                finally:
                    in_flight -= 1
                return LLMResponse(
                    output="def add(a, b): return a + b",
                    input_tokens=10,
                    output_tokens=20,
                    claimed_done=False,
                    model=model,
                    cost_usd=Decimal("0.01"),
                )

        worker = LoopWorker(
            settings=make_settings(loop_concurrency=2),
            llm=OverlapProbeLLM(),  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(i) for i in ids]),
        )
        await worker._poll()
        assert peak <= 2, f"并发超过配置上限 2（实测 {peak}）"

    async def test_one_failure_does_not_block_siblings(self, pg_setup: Any) -> None:
        """单项异常不能拖垮同批其他项，也不能让它们漏掉 ACK。"""
        engine, maker = pg_setup
        bad = await create_loop_row(maker, make_goal())
        good = await create_loop_row(maker, make_goal())

        class HalfBrokenLLM:
            async def complete(self, prompt: str, *, model: str) -> LLMResponse:
                if str(bad) in prompt or "boom" in prompt:
                    raise RuntimeError("boom")
                return LLMResponse(
                    output="def add(a, b): return a + b",
                    input_tokens=10,
                    output_tokens=20,
                    claimed_done=False,
                    model=model,
                    cost_usd=Decimal("0.01"),
                )

        queue = FakeLoopQueue(loop_ids=[str(bad), str(good)])
        worker = LoopWorker(
            settings=make_settings(loop_concurrency=2),
            llm=HalfBrokenLLM(),  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=queue,
        )
        await worker._poll()
        # good 一定跑完并 ACK；bad 走 engine 的错误路径，不影响 good
        assert f"msg-{good}" in queue.acked


def make_settings(*, loop_concurrency: int | None = None) -> Any:
    """最小 Settings（仅提供 redis url 供 worker 读取）。"""
    from pydantic import SecretStr

    from ariadne.config import (
        ApiSettings,
        ClickHouseSettings,
        RedisSettings,
        Settings,
        WorkerSettings,
    )

    worker = (
        WorkerSettings(loop_concurrency=loop_concurrency)
        if loop_concurrency is not None
        else WorkerSettings()
    )
    return Settings(
        env="test",
        log_level="WARNING",
        clickhouse=ClickHouseSettings(host="localhost", database="ariadne_test"),
        redis=RedisSettings(url="redis://localhost:6379/15"),
        api=ApiSettings(
            default_project_id=PROJECT_ID, static_api_key=SecretStr("ak_test_key")
        ),
        worker=worker,
    )


# ---------- M4: Harness 接入 Worker 测试 ----------


def make_settings_with_harness(rules_dir: str = "", audit_enabled: bool = True) -> Any:
    """带 HarnessSettings 的 Settings。"""
    from pydantic import SecretStr

    from ariadne.config import (
        ApiSettings,
        ClickHouseSettings,
        HarnessSettings,
        RedisSettings,
        Settings,
    )

    return Settings(
        env="test",
        log_level="WARNING",
        clickhouse=ClickHouseSettings(host="localhost", database="ariadne_test"),
        redis=RedisSettings(url="redis://localhost:6379/15"),
        api=ApiSettings(
            default_project_id=PROJECT_ID, static_api_key=SecretStr("ak_test_key")
        ),
        harness=HarnessSettings(rules_dir=rules_dir, audit_enabled=audit_enabled),
    )


class TestWorkerHarnessIntegration:
    """验证 Worker 装配 harness + GuardedLLMAdapter。"""

    async def test_no_harness_backward_compat(self, pg_setup: Any) -> None:
        """无 rules_dir 时 harness=None，行为同 M3。"""
        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings(),  # rules_dir="" → 无 harness
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.state == LoopState.CONVERGED.value

    async def test_harness_block_terminates_blocked(self, pg_setup: Any, tmp_path: Any) -> None:
        """Harness 规则 BLOCK → loop 终态 BLOCKED。"""
        # 写入 PII 检测规则
        rule_file = tmp_path / "input.yaml"
        rule_file.write_text(
            """\
- id: block-pii
  category: input
  hook: pre_model
  when: detect_pii(input.text).size() > 0
  action: block
  severity: critical
  message: PII detected in input
""",
            encoding="utf-8",
        )

        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        # LLM 输出含 PII（email），pre_model 规则会拦截
        llm = ScriptedLLM(outputs=["email: test@example.com"])
        worker = LoopWorker(
            settings=make_settings_with_harness(rules_dir=str(tmp_path)),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.state == LoopState.BLOCKED.value
        assert row.final_state == LoopState.BLOCKED.value

    async def test_harness_passthrough_converged(self, pg_setup: Any, tmp_path: Any) -> None:
        """Harness 规则不命中 → 正常收敛。"""
        rule_file = tmp_path / "output.yaml"
        rule_file.write_text(
            """\
- id: require-citation
  category: output
  hook: post_model
  when: count_citations(output.text) < 1
  action: warn
  severity: low
  message: no citations found
""",
            encoding="utf-8",
        )

        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings_with_harness(rules_dir=str(tmp_path)),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.state == LoopState.CONVERGED.value

    async def test_audit_disabled_no_crash(self, pg_setup: Any, tmp_path: Any) -> None:
        """audit_enabled=False 时用 NullAuditSink，不写 DB 也不崩溃。"""
        rule_file = tmp_path / "input.yaml"
        rule_file.write_text(
            """\
- id: warn-long
  category: input
  hook: pre_model
  when: input.text.size() > 1000
  action: warn
  severity: low
""",
            encoding="utf-8",
        )

        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings_with_harness(
                rules_dir=str(tmp_path), audit_enabled=False
            ),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        assert row.state == LoopState.CONVERGED.value

    async def test_bad_rules_dir_fails_open(self, pg_setup: Any) -> None:
        """rules_dir 指向不存在的目录 → fail-open，无 harness 运行。"""
        engine, maker = pg_setup
        goal = make_goal()
        loop_id = await create_loop_row(maker, goal)
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b"])
        worker = LoopWorker(
            settings=make_settings_with_harness(rules_dir="/nonexistent/path"),
            llm=llm,  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[str(loop_id)]),
        )
        await worker._poll()
        row = await get_row(maker, loop_id)
        # 无规则 → 正常收敛
        assert row.state == LoopState.CONVERGED.value

# ---------- 工作目录接线（COMMAND 断言在生产路径上可用的前提） ----------


def make_command_goal(workspace: tuple[tuple[str, str], ...] = ()) -> Goal:
    """带 COMMAND 断言的目标 —— 代码生成场景的形态。"""
    return Goal(
        task="修复 solution.py 让测试通过",
        assertions=(
            Assertion(
                id="tests_pass",
                kind=AssertionKind.COMMAND,
                spec={"cmd": "python -m pytest -q"},
            ),
        ),
        budget=Budget(max_iterations=2, max_total_tokens=50_000),
        mode="verify_execute",
        workspace=workspace,
    )


class TestWorkspaceWiring:
    """盯**装配关系**而非被装配的组件。

    R12 的教训：单元测试只证明"如果调用它，它能工作"，不证明"它被调用了"。
    `_prepare_workspace` 自己的逻辑再对，只要 `_build_engine` 不把结果传进
    LoopConfig.artifact_path，COMMAND 断言在生产路径上就依然不可用 ——
    而且失败方式是静默的（goal_validation 判 REJECTED，看起来像用户配错了）。
    """

    def test_command_goal_gets_a_workspace(self, tmp_path: Any) -> None:
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        loop_id = uuid.uuid4()
        workspace = worker._prepare_workspace(loop_id, make_command_goal())
        assert workspace is not None
        assert workspace.is_dir()
        assert workspace.name == str(loop_id)

    def test_non_command_goal_gets_no_workspace(self, tmp_path: Any) -> None:
        """REGEX/SCHEMA 断言是 output 的纯函数，建目录纯属多余 IO。"""
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        assert worker._prepare_workspace(uuid.uuid4(), make_goal()) is None

    def test_seed_files_land_in_workspace(self, tmp_path: Any) -> None:
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        goal = make_command_goal(
            workspace=(("solution.py", "X = 1\n"), ("pkg/test_a.py", "Y = 2\n")),
        )
        workspace = worker._prepare_workspace(uuid.uuid4(), goal)
        assert workspace is not None
        assert (workspace / "solution.py").read_text(encoding="utf-8") == "X = 1\n"
        assert (workspace / "pkg" / "test_a.py").read_text(encoding="utf-8") == "Y = 2\n"

    def test_traversal_in_seed_files_is_refused(self, tmp_path: Any) -> None:
        """请求体不可信 —— API 层挡一次，这里再挡一次。"""
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        goal = make_command_goal(workspace=(("../escaped.py", "EVIL = 1\n"),))
        with pytest.raises(RuntimeError, match="种子文件落盘失败"):
            worker._prepare_workspace(uuid.uuid4(), goal)
        assert not (tmp_path / "escaped.py").exists()

    def test_oversized_seed_file_is_refused(self, tmp_path: Any) -> None:
        """Worker 侧仍需限制未经 API 校验直接构造的 Goal。"""
        from ariadne.loop_module.artifact import MAX_BYTES_PER_FILE

        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        goal = make_command_goal(
            workspace=(("solution.py", "x" * (MAX_BYTES_PER_FILE + 1)),)
        )
        with pytest.raises(RuntimeError, match="种子文件落盘失败"):
            worker._prepare_workspace(uuid.uuid4(), goal)

    async def test_engine_receives_the_workspace(self, pg_setup: Any) -> None:
        """装配断言：_build_engine 必须把工作目录传进 LoopConfig。

        直接读 engine 的私有配置而非观察行为 —— 行为观察需要真跑 pytest，
        而这条要盯的就是"值有没有传过去"这一件事。
        """
        engine_, maker = pg_setup
        goal = make_command_goal(workspace=(("solution.py", "X = 1\n"),))
        worker = LoopWorker(
            settings=make_settings(),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine_, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        loop_id = uuid.uuid4()
        workspace = worker._prepare_workspace(loop_id, goal)
        pg = make_pg_factory(engine_, maker)(make_settings().postgres)
        built = await worker._build_engine(loop_id, goal, pg, PROJECT_ID, workspace)
        assert built._cfg.artifact_path == workspace, (
            "工作目录没传进 LoopConfig —— COMMAND 断言会被判 errored"
        )

    def test_terminal_state_cleans_up(self, tmp_path: Any) -> None:
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        loop_id = uuid.uuid4()
        workspace = worker._prepare_workspace(loop_id, make_command_goal())
        assert workspace is not None
        worker._cleanup_workspace(loop_id, workspace, LoopState.CONVERGED)
        assert not workspace.exists()

    def test_non_terminal_state_keeps_workspace(self, tmp_path: Any) -> None:
        """HUMAN_PENDING 等非终态要留着目录，供接管的 Worker 复用产出物。"""
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        loop_id = uuid.uuid4()
        workspace = worker._prepare_workspace(loop_id, make_command_goal())
        assert workspace is not None
        worker._cleanup_workspace(loop_id, workspace, LoopState.HUMAN_PENDING)
        assert workspace.exists()

    def test_tool_dir_cleaned_for_non_command_loop(self, tmp_path: Any) -> None:
        """无 COMMAND 断言的 Loop 没有预建目录，但工具可能创建过 —— 终态要清。"""
        worker = LoopWorker(
            settings=make_settings_with_workspace(str(tmp_path)),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        loop_id = uuid.uuid4()
        tool_dir = worker._tool_dir(loop_id)
        tool_dir.mkdir(parents=True)
        worker._cleanup_workspace(loop_id, None, LoopState.CONVERGED)
        assert not tool_dir.exists()

    async def test_engine_receives_tool_executor(self, pg_setup: Any) -> None:
        """装配断言：tool_executor 必须真的传进 LoopConfig（P0-4）。

        此前该字段"建好但从未接线"：声明、注释、测试俱在，唯独没有生产
        路径装配它。这条测试盯的就是注入本身。
        """
        engine_, maker = pg_setup
        goal = make_goal()
        worker = LoopWorker(
            settings=make_settings(),
            llm=ScriptedLLM(outputs=["x"]),  # type: ignore[arg-type]
            pg_factory=make_pg_factory(engine_, maker),
            counter_factory=make_counter_factory(),
            queue=FakeLoopQueue(loop_ids=[]),
        )
        loop_id = uuid.uuid4()
        pg = make_pg_factory(engine_, maker)(make_settings().postgres)
        built = await worker._build_engine(loop_id, goal, pg, PROJECT_ID, None)
        assert built._cfg.tool_executor is not None
        assert isinstance(built._cfg.tool_executor, WorkspaceToolExecutor)
        # 工具目录与 COMMAND 断言的工作目录是同一个约定路径
        assert built._cfg.tool_executor.workdir.name == str(loop_id)


def make_settings_with_workspace(root: str) -> Any:
    from pydantic import SecretStr

    from ariadne.config import (
        ApiSettings,
        ClickHouseSettings,
        RedisSettings,
        SandboxSettings,
        Settings,
    )

    return Settings(
        env="test",
        log_level="WARNING",
        clickhouse=ClickHouseSettings(host="localhost", database="ariadne_test"),
        redis=RedisSettings(url="redis://localhost:6379/15"),
        api=ApiSettings(
            default_project_id=PROJECT_ID, static_api_key=SecretStr("ak_test_key")
        ),
        sandbox=SandboxSettings(workspace_root=root),
    )
