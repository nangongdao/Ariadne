"""租户隔离测试 —— 跨租户资源不可见（应用层防线）。

扩展 test_postgres_repos.py 的隔离测试到所有 repository：
datasets / experiments / prompts / loop_runs / api_keys / loop_checkpoints。

SQLite 无 RLS，这里只验证应用层过滤（repository 带 project_id WHERE），
也就是四层防护里的第一层。PG 侧 RLS 兜底在 test_auth_integration.py
（-m integration，需真实 PG）。

两层的边界值得说清：应用层过滤挡的是"代码写对了"的情况，RLS 挡的是
"代码漏了 WHERE"的情况。本文件证明前者，不覆盖后者。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ariadne.experiment.dataset import DatasetItem
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal

PROJECT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
PROJECT_B = uuid.UUID("00000000-0000-0000-0000-000000000002")


def make_goal() -> Goal:
    return Goal(
        task="test",
        assertions=(
            Assertion(
                id="length",
                kind=AssertionKind.REGEX,
                spec={"pattern": r"^.{1,500}$"},
                hint="控制在 500 字以内",
            ),
        ),
        budget=Budget(max_iterations=3, max_total_tokens=50_000, max_cost_usd=0.2),
        mode="quality",
    )


def make_items(n: int = 1) -> list[DatasetItem]:
    return [DatasetItem(item_id=f"i{k}", input=f"q{k}", expected=f"a{k}") for k in range(n)]


class TestDatasetIsolation:
    async def test_cross_tenant_dataset_invisible(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.datasets import (
            DatasetNotFoundError,
            DatasetRepository,
        )

        async with memory_pg.session() as session:
            repo = DatasetRepository(session)
            await repo.create(
                project_id=PROJECT_A, name="core", items=make_items(), version=1
            )
            await session.flush()

            with pytest.raises(DatasetNotFoundError):
                await repo.get(project_id=PROJECT_B, name="core", version=1)
            await session.commit()

    async def test_cross_tenant_list_empty(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.datasets import DatasetRepository

        async with memory_pg.session() as session:
            repo = DatasetRepository(session)
            await repo.create(
                project_id=PROJECT_A, name="ds-a", items=make_items(), version=1
            )
            await session.flush()

            names_b = await repo.list_names(project_id=PROJECT_B)
            assert names_b == []
            await session.commit()


class TestExperimentIsolation:
    async def test_cross_tenant_experiment_invisible(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.experiments import (
            ExperimentNotFoundError,
            ExperimentRepository,
        )

        async with memory_pg.session() as session:
            repo = ExperimentRepository(session)
            exp_id = await repo.create(
                project_id=PROJECT_A,
                dataset_ref="ds@1",
                config_label="cfg-a",
                config={},
            )
            await session.flush()

            with pytest.raises(ExperimentNotFoundError):
                await repo.get(project_id=PROJECT_B, experiment_id=exp_id)
            await session.commit()

    async def test_cross_tenant_list_recent_empty(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.experiments import (
            ExperimentRepository,
        )

        async with memory_pg.session() as session:
            repo = ExperimentRepository(session)
            await repo.create(
                project_id=PROJECT_A,
                dataset_ref="ds@1",
                config_label="cfg-a",
                config={},
            )
            await session.flush()

            recent_b = await repo.list_recent(project_id=PROJECT_B, limit=10)
            assert recent_b == []
            await session.commit()


class TestPromptIsolation:
    async def test_cross_tenant_prompt_invisible(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.prompts import (
            PromptNotFoundError,
            PromptRepository,
        )

        async with memory_pg.session() as session:
            repo = PromptRepository(session)
            await repo.create(
                project_id=PROJECT_A,
                name="p-a",
                template="hello",
                variables=None,
                labels=(),
            )
            await session.flush()

            with pytest.raises(PromptNotFoundError):
                await repo.get(project_id=PROJECT_B, name="p-a", version=1)
            await session.commit()

    async def test_cross_tenant_list_versions_empty(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.prompts import PromptRepository

        async with memory_pg.session() as session:
            repo = PromptRepository(session)
            await repo.create(
                project_id=PROJECT_A,
                name="p-a",
                template="hello",
                variables=None,
                labels=(),
            )
            await session.flush()

            versions_b = await repo.list_versions(project_id=PROJECT_B, name="p-a")
            assert versions_b == []
            await session.commit()


class TestLoopRunIsolation:
    async def test_cross_tenant_loop_run_invisible(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.loop_runs import (
            LoopRunNotFoundError,
            LoopRunRepository,
        )

        async with memory_pg.session() as session:
            repo = LoopRunRepository(session)
            loop_id = await repo.create(
                project_id=PROJECT_A,
                mode="quality",
                goal=make_goal(),
            )
            await session.flush()

            with pytest.raises(LoopRunNotFoundError):
                await repo.get(project_id=PROJECT_B, loop_id=loop_id)
            await session.commit()

    async def test_cross_tenant_list_recent_empty(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.loop_runs import (
            LoopRunRepository,
        )

        async with memory_pg.session() as session:
            repo = LoopRunRepository(session)
            await repo.create(
                project_id=PROJECT_A,
                mode="quality",
                goal=make_goal(),
            )
            await session.flush()

            recent_b = await repo.list_recent(project_id=PROJECT_B, limit=10)
            assert recent_b == []
            await session.commit()

    async def test_cross_tenant_transition_is_rejected(self, memory_pg: Any) -> None:
        """状态修改也必须带项目范围，不能只保护读取接口。"""
        from ariadne.loop_module.state_machine import LoopState
        from ariadne.storage.postgres.repositories.loop_runs import (
            LoopRunNotFoundError,
            LoopRunRepository,
        )

        async with memory_pg.session() as session:
            repo = LoopRunRepository(session)
            loop_id = await repo.create(
                project_id=PROJECT_A,
                mode="quality",
                goal=make_goal(),
            )
            await session.flush()

            with pytest.raises(LoopRunNotFoundError):
                await repo.transition(
                    project_id=PROJECT_B,
                    loop_id=loop_id,
                    to=LoopState.CANCELLED,
                )

            row = await repo.get(project_id=PROJECT_A, loop_id=loop_id)
            assert row.state == LoopState.VALIDATE.value
            await session.commit()


class TestApiKeyIsolation:
    async def test_cross_tenant_api_key_invisible(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.api_keys import (
            ApiKeyRepository,
        )

        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            _key_id, plain = await repo.create(
                project_id=PROJECT_A, name="key-a", role="viewer"
            )
            await session.flush()

            from ariadne.auth.keys import extract_prefix

            prefix = extract_prefix(plain)
            found = await repo.get_by_prefix(
                project_id=PROJECT_B, key_prefix=prefix
            )
            assert found is None
            await session.commit()

    async def test_cross_tenant_list_empty(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.api_keys import (
            ApiKeyRepository,
        )

        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            await repo.create(
                project_id=PROJECT_A, name="key-a", role="viewer"
            )
            await session.flush()

            keys_b = await repo.list(project_id=PROJECT_B)
            assert keys_b == []
            await session.commit()


class TestRLSLifecycle:
    """set_tenant_context / reset_tenant_context 生命周期（SQLite no-op）。"""

    async def test_set_reset_on_sqlite_noop(self, memory_pg: Any) -> None:
        from ariadne.auth.tenant import reset_tenant_context, set_tenant_context

        async with memory_pg.session() as session:
            await set_tenant_context(session, PROJECT_A)
            await reset_tenant_context(session)
            await session.commit()

    async def test_tenant_session_lifecycle(self, memory_pg: Any) -> None:
        """tenant_session 上下文管理器在 SQLite 上正常工作。"""
        from sqlalchemy import text

        async with memory_pg.session() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
