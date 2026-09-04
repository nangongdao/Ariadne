"""测试夹具。

单元测试用内存桩替换 ClickHouse / Redis，让 API 层（路由、认证、错误处理、
树构建）无需容器即可验证。集成测试（-m integration）才连真实容器 ——
数据库行为不 mock，这是刻意的：mock 掉的 SQL 语法错误只有真实库能发现。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from ariadne.config import ApiSettings, ClickHouseSettings, RedisSettings, Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

TEST_PROJECT = UUID("00000000-0000-0000-0000-000000000001")
TEST_ORG = UUID("00000000-0000-0000-0000-0000000000aa")
TEST_KEY = "ak_test_key"


class FakeQueue:
    """内存队列。记录 publish 的批次供断言。"""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self._counter = 0

    async def connect(self) -> None: ...
    async def ensure_group(self) -> None: ...
    async def close(self) -> None: ...

    async def ping(self) -> bool:
        return True

    async def publish(self, batch: dict[str, Any]) -> str:
        # 走一遍 JSON 序列化，暴露不可序列化的载荷
        self.published.append(json.loads(json.dumps(batch, default=str)))
        self._counter += 1
        return f"0-{self._counter}"

    async def stream_length(self) -> int:
        return len(self.published)

    async def pending_count(self) -> int:
        return 0


class FakeStore:
    """内存 ClickHouse 桩。按 SQL 关键字返回预置行。"""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def ping(self) -> bool:
        return True

    def close(self) -> None: ...

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        project_id: Any = None,
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, params or {}))
        if "FROM trace_rollup" in sql:
            return self.rows.get("trace_rollup", [])
        if "FROM cost_rollup" in sql:
            return self.rows.get("cost_rollup", [])
        if "FROM spans" in sql:
            return self.rows.get("spans", [])
        return []


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        clickhouse=ClickHouseSettings(host="localhost", database="ariadne_test"),
        redis=RedisSettings(url="redis://localhost:6379/15"),
        api=ApiSettings(
            default_project_id=TEST_PROJECT, static_api_key=SecretStr(TEST_KEY)
        ),
    )


@pytest.fixture
def fake_queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def memory_pg() -> AsyncIterator[Any]:
    """内存 Postgres 替身：aiosqlite 上跑真实 SQL。

    不 mock 数据库 —— SQL 语法错误、约束冲突、事务行为只有真引擎能发现。
    Postgres 特有行为（JSONB 操作符、RLS）留给 -m integration。

    使用临时文件而非纯内存数据库，因为后台任务（GraphWorker）需要跨连接共享数据。
    """
    import tempfile
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ariadne.storage.postgres import (  # noqa: F401 — register tables on metadata
        auth_models,
        graph_models,
        harness_models,
        model_config_models,
    )
    from ariadne.storage.postgres.graph_models import (
        GraphRun,  # noqa: F401 — ensure GraphRun is registered
    )
    from ariadne.storage.postgres.models import Base, Organization, Project

    # 创建临时文件数据库（测试结束后自动删除）
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)

    try:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as sess:
            sess.add(Organization(id=TEST_ORG, name="test-org"))
            sess.add(Project(id=TEST_PROJECT, org_id=TEST_ORG, slug="t", name="Test"))
            await sess.commit()

        class MemoryPg:
            """与 PostgresStore 同接口的最小实现。"""

            @asynccontextmanager
            async def session(self) -> AsyncIterator[Any]:
                async with maker() as s:
                    try:
                        yield s
                        await s.commit()
                    except Exception:
                        await s.rollback()
                        raise

            @asynccontextmanager
            async def tenant_session(self, project_id: UUID) -> AsyncIterator[Any]:
                """与 PostgresStore.tenant_session 同形。

                刻意真调 set/reset_tenant_context 而非直接 yield：SQLite 下两者
                都按 dialect 短路成 no-op，跑到这里才能保证「短路判断本身」不退化
                （若哪天误删 dialect 检查，SQLite 会因 SET LOCAL 语法直接报错）。
                """
                from ariadne.auth.tenant import reset_tenant_context, set_tenant_context

                async with maker() as s:
                    try:
                        await set_tenant_context(s, project_id)
                        yield s
                        await s.commit()
                    except Exception:
                        await s.rollback()
                        raise
                    finally:
                        await reset_tenant_context(s)

            async def ping(self) -> bool:
                return True

            async def close(self) -> None: ...

        yield MemoryPg()
        await engine.dispose()
    finally:
        # 清理临时文件
        if db_path.exists():
            db_path.unlink()


@pytest.fixture
def app(
    settings: Settings,
    fake_queue: FakeQueue,
    fake_store: FakeStore,
    memory_pg: Any,
) -> FastAPI:
    """构造 app 并注入桩，绕过 lifespan 里的真实连接。"""
    from ariadne.api.app import create_app
    from ariadne.api.deps import get_app_settings
    from ariadne.worker.graph_worker_singleton import init_graph_worker

    application = create_app(settings)
    application.state.queue = fake_queue
    application.state.store = fake_store
    application.state.pg = memory_pg

    # Override 依赖注入，让 execute_graph 中的 settings 参数获取到测试配置
    application.dependency_overrides[get_app_settings] = lambda: settings

    # 初始化 GraphWorker（因为测试绕过了 lifespan）
    init_graph_worker(memory_pg, settings)

    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    # 用 with 会触发 lifespan（尝试连真实 Redis），这里刻意不进入上下文
    yield TestClient(app)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-Ariadne-Key": TEST_KEY}
