"""仓储层测试。

用 aiosqlite 跑**真实 SQL** 而非 mock 数据库：mock 掉的 SQL 语法错误、
约束冲突、事务行为只有真引擎能发现。Postgres 特有行为（JSONB 操作符、
RLS）留给 -m integration 的真容器测试。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
)
from ariadne.eval_module.composite import CompositeScorer, ScoreSpec
from ariadne.experiment import (
    Dataset,
    DatasetItem,
    ExperimentRunner,
    GenerationOutput,
)
from ariadne.storage.postgres.models import Base, Organization, Project
from ariadne.storage.postgres.repositories import (
    DatasetNotFoundError,
    DatasetRepository,
    DatasetVersionConflictError,
    ExperimentRepository,
    InvalidTransitionError,
    PromptNotFoundError,
    PromptRepository,
    judge_models_of,
)

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        sess.add(Organization(id=ORG_ID, name="test-org"))
        sess.add(
            Project(id=PROJECT_ID, org_id=ORG_ID, slug="test", name="Test Project")
        )
        await sess.commit()
        yield sess

    await engine.dispose()


def items(n: int = 3) -> list[DatasetItem]:
    return [
        DatasetItem(item_id=f"i{k}", input=f"q{k}", expected=f"a{k}")
        for k in range(n)
    ]


class TestDatasetRepository:
    async def test_create_and_get(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        created = await repo.create(
            project_id=PROJECT_ID, name="core", items=items(3)
        )
        assert created.version == 1

        loaded = await repo.get(project_id=PROJECT_ID, name="core")
        assert loaded.content_hash == created.content_hash
        assert len(loaded) == 3

    async def test_version_auto_increments(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        await repo.create(project_id=PROJECT_ID, name="core", items=items(2))
        second = await repo.create(project_id=PROJECT_ID, name="core", items=items(3))
        assert second.version == 2

    async def test_duplicate_version_rejected(self, session: AsyncSession) -> None:
        """显式报错而非自动递增：否则调用方以为建了 v2 实际建了 v3。"""
        repo = DatasetRepository(session)
        await repo.create(project_id=PROJECT_ID, name="core", items=items(2), version=1)
        with pytest.raises(DatasetVersionConflictError, match="版本不可变"):
            await repo.create(
                project_id=PROJECT_ID, name="core", items=items(3), version=1
            )

    async def test_get_specific_version(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        v1 = await repo.create(project_id=PROJECT_ID, name="core", items=items(2))
        await repo.create(project_id=PROJECT_ID, name="core", items=items(5))

        loaded = await repo.get(project_id=PROJECT_ID, name="core", version=1)
        assert len(loaded) == 2
        assert loaded.content_hash == v1.content_hash

    async def test_get_defaults_to_latest(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        await repo.create(project_id=PROJECT_ID, name="core", items=items(2))
        await repo.create(project_id=PROJECT_ID, name="core", items=items(5))
        assert len(await repo.get(project_id=PROJECT_ID, name="core")) == 5

    async def test_missing_raises(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        with pytest.raises(DatasetNotFoundError):
            await repo.get(project_id=PROJECT_ID, name="nope")

    async def test_hash_roundtrip_preserved(self, session: AsyncSession) -> None:
        """存进去再取出来 hash 必须不变，否则复现校验失效。"""
        repo = DatasetRepository(session)
        created = await repo.create(
            project_id=PROJECT_ID, name="core", items=items(10)
        )
        loaded = await repo.get(project_id=PROJECT_ID, name="core")
        assert loaded.content_hash == created.content_hash
        assert loaded.verify(), "取出的数据集应能自校验"

    async def test_verify_hash_helper(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        created = await repo.create(project_id=PROJECT_ID, name="core", items=items(3))
        assert await repo.verify_hash(
            project_id=PROJECT_ID, name="core", version=1,
            expected_hash=created.content_hash,
        )
        assert not await repo.verify_hash(
            project_id=PROJECT_ID, name="core", version=1, expected_hash="wrong"
        )

    async def test_tenant_isolation_at_app_layer(self, session: AsyncSession) -> None:
        """用别的 project_id 查不到 —— 应用层防线（M6 会加 RLS 兜底）。"""
        repo = DatasetRepository(session)
        await repo.create(project_id=PROJECT_ID, name="core", items=items(2))
        other = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
        with pytest.raises(DatasetNotFoundError):
            await repo.get(project_id=other, name="core")

    async def test_list_names_and_versions(self, session: AsyncSession) -> None:
        repo = DatasetRepository(session)
        await repo.create(project_id=PROJECT_ID, name="core", items=items(2))
        await repo.create(project_id=PROJECT_ID, name="core", items=items(3))
        await repo.create(project_id=PROJECT_ID, name="edge", items=items(1))

        assert await repo.list_names(project_id=PROJECT_ID) == [("core", 2), ("edge", 1)]
        versions = await repo.list_versions(project_id=PROJECT_ID, name="core")
        assert [v[0] for v in versions] == [2, 1]

    async def test_duplicate_item_ids_rejected_by_domain(
        self, session: AsyncSession
    ) -> None:
        repo = DatasetRepository(session)
        dupes = [
            DatasetItem(item_id="same", input="a"),
            DatasetItem(item_id="same", input="b"),
        ]
        with pytest.raises(ValueError, match="重复 item_id"):
            await repo.create(project_id=PROJECT_ID, name="bad", items=dupes)


class ScoreByLength(BaseEvaluator):
    kind = EvaluatorKind.DETERMINISTIC

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, 100.0)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        score = min(len(ctx.output) * 10.0, 100.0)
        return EvalResult(
            name=self.name, value=score, passed=score >= 50.0,
            judge_model="claude-sonnet-5",
        )


class UniformGen:
    def generate(self, item: DatasetItem) -> GenerationOutput:
        return GenerationOutput(text="x" * 10, cost_usd=Decimal("0.01"))


class TestExperimentRepository:
    async def test_create_and_transition(self, session: AsyncSession) -> None:
        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID,
            dataset_ref="core@v1#abc123",
            config_label="v1",
            config={"model": "gpt-4o"},
        )
        await repo.transition(
            project_id=PROJECT_ID, experiment_id=exp_id, to="running"
        )
        row = await repo.get(project_id=PROJECT_ID, experiment_id=exp_id)
        assert row.status == "running"

    async def test_invalid_transition_rejected(self, session: AsyncSession) -> None:
        """散落的 status = "x" 会让状态流转无从追溯 —— 必须走显式转移。"""
        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID, dataset_ref="d", config_label="v1", config={}
        )
        with pytest.raises(InvalidTransitionError, match="不能从 pending 转到 completed"):
            await repo.transition(
                project_id=PROJECT_ID, experiment_id=exp_id, to="completed"
            )

    async def test_terminal_state_is_final(self, session: AsyncSession) -> None:
        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID, dataset_ref="d", config_label="v1", config={}
        )
        await repo.transition(project_id=PROJECT_ID, experiment_id=exp_id, to="cancelled")
        with pytest.raises(InvalidTransitionError, match="已是终态"):
            await repo.transition(
                project_id=PROJECT_ID, experiment_id=exp_id, to="running"
            )

    async def test_save_result_persists_metrics(self, session: AsyncSession) -> None:
        ds = Dataset.create(
            dataset_id="d", name="core", version=1, items=items(5)
        )
        scorer = CompositeScorer((ScoreSpec(ScoreByLength("length")),), threshold=50.0)
        result = ExperimentRunner(scorer=scorer).run(
            experiment_id="e", dataset=ds, generator=UniformGen(), config_label="v1"
        )

        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID, dataset_ref=ds.ref, config_label="v1", config={}
        )
        await repo.transition(project_id=PROJECT_ID, experiment_id=exp_id, to="running")
        await repo.save_result(
            project_id=PROJECT_ID, experiment_id=exp_id, result=result
        )

        row = await repo.get(project_id=PROJECT_ID, experiment_id=exp_id)
        assert row.status == "completed"
        assert row.item_count == 5
        assert row.metrics["composite_quality"] == 100.0
        assert "length" in row.metrics["evaluators"]
        assert judge_models_of(row) == ("claude-sonnet-5",)
        assert row.total_cost_usd == Decimal("0.05")
        assert row.finished_at is not None

    async def test_save_result_twice_rejected(self, session: AsyncSession) -> None:
        ds = Dataset.create(dataset_id="d", name="core", version=1, items=items(2))
        scorer = CompositeScorer((ScoreSpec(ScoreByLength("l")),), threshold=50.0)
        result = ExperimentRunner(scorer=scorer).run(
            experiment_id="e", dataset=ds, generator=UniformGen()
        )
        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID, dataset_ref=ds.ref, config_label="v1", config={}
        )
        await repo.save_result(
            project_id=PROJECT_ID, experiment_id=exp_id, result=result
        )
        with pytest.raises(InvalidTransitionError, match="不能再写入结果"):
            await repo.save_result(
                project_id=PROJECT_ID, experiment_id=exp_id, result=result
            )

    async def test_find_baseline_requires_same_dataset(
        self, session: AsyncSession
    ) -> None:
        """在不同数据集上比均值无意义 —— 基线查找必须限定 dataset_ref。"""
        repo = ExperimentRepository(session)
        matching = await repo.create(
            project_id=PROJECT_ID, dataset_ref="core@v1#abc", config_label="v1", config={}
        )
        await repo.transition(project_id=PROJECT_ID, experiment_id=matching, to="running")
        await repo.transition(
            project_id=PROJECT_ID, experiment_id=matching, to="completed"
        )
        other = await repo.create(
            project_id=PROJECT_ID, dataset_ref="other@v1#xyz", config_label="v1", config={}
        )
        await repo.transition(project_id=PROJECT_ID, experiment_id=other, to="running")
        await repo.transition(project_id=PROJECT_ID, experiment_id=other, to="completed")

        found = await repo.find_baseline(
            project_id=PROJECT_ID, dataset_ref="core@v1#abc", config_label="v1"
        )
        assert found is not None
        assert found.id == matching

    async def test_find_baseline_ignores_incomplete(
        self, session: AsyncSession
    ) -> None:
        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID, dataset_ref="core@v1#abc", config_label="v1", config={}
        )
        await repo.transition(project_id=PROJECT_ID, experiment_id=exp_id, to="running")
        assert (
            await repo.find_baseline(
                project_id=PROJECT_ID, dataset_ref="core@v1#abc", config_label="v1"
            )
            is None
        )

    async def test_list_recent_filters(self, session: AsyncSession) -> None:
        repo = ExperimentRepository(session)
        for label in ("a", "b", "c"):
            await repo.create(
                project_id=PROJECT_ID, dataset_ref="d", config_label=label, config={}
            )
        assert len(await repo.list_recent(project_id=PROJECT_ID)) == 3
        assert len(await repo.list_recent(project_id=PROJECT_ID, status="running")) == 0

    async def test_cost_summary(self, session: AsyncSession) -> None:
        ds = Dataset.create(dataset_id="d", name="core", version=1, items=items(4))
        scorer = CompositeScorer((ScoreSpec(ScoreByLength("l")),), threshold=50.0)
        result = ExperimentRunner(scorer=scorer).run(
            experiment_id="e", dataset=ds, generator=UniformGen()
        )
        repo = ExperimentRepository(session)
        exp_id = await repo.create(
            project_id=PROJECT_ID, dataset_ref=ds.ref, config_label="v1", config={}
        )
        await repo.save_result(
            project_id=PROJECT_ID, experiment_id=exp_id, result=result
        )
        summary = await repo.cost_summary(project_id=PROJECT_ID)
        assert summary["experiment_count"] == 1
        assert summary["total_cost_usd"] == Decimal("0.04")


class TestPromptRepository:
    async def test_create_and_get(self, session: AsyncSession) -> None:
        repo = PromptRepository(session)
        created = await repo.create(
            project_id=PROJECT_ID,
            name="blog",
            template="写一篇关于 {{topic}} 的文章",
            variables={"topic": "主题"},
        )
        assert created.version == 1
        assert created.ref.startswith("blog@v1#")

        loaded = await repo.get(project_id=PROJECT_ID, name="blog")
        assert loaded.content_hash == created.content_hash

    async def test_render_requires_all_variables(self, session: AsyncSession) -> None:
        """缺变量时报错而非留下字面花括号 —— 后者产生难定位的质量问题。"""
        repo = PromptRepository(session)
        prompt = await repo.create(
            project_id=PROJECT_ID,
            name="blog",
            template="{{topic}} 与 {{audience}}",
            variables={"topic": "", "audience": ""},
        )
        with pytest.raises(KeyError, match="audience"):
            prompt.render({"topic": "AI"})

        assert prompt.render({"topic": "AI", "audience": "开发者"}) == "AI 与 开发者"

    async def test_label_is_unique_per_project(self, session: AsyncSession) -> None:
        """production 指向两个版本是无意义状态。"""
        repo = PromptRepository(session)
        await repo.create(
            project_id=PROJECT_ID, name="blog", template="v1", labels=("production",)
        )
        await repo.create(project_id=PROJECT_ID, name="blog", template="v2")
        await repo.set_label(
            project_id=PROJECT_ID, name="blog", version=2, label="production"
        )

        versions = await repo.list_versions(project_id=PROJECT_ID, name="blog")
        holders = [v.version for v in versions if "production" in v.labels]
        assert holders == [2]

    async def test_get_by_label(self, session: AsyncSession) -> None:
        repo = PromptRepository(session)
        await repo.create(project_id=PROJECT_ID, name="blog", template="old")
        await repo.create(
            project_id=PROJECT_ID, name="blog", template="new", labels=("production",)
        )
        found = await repo.get_by_label(
            project_id=PROJECT_ID, name="blog", label="production"
        )
        assert found.template == "new"

    async def test_missing_label_raises(self, session: AsyncSession) -> None:
        repo = PromptRepository(session)
        await repo.create(project_id=PROJECT_ID, name="blog", template="x")
        with pytest.raises(PromptNotFoundError, match="标签"):
            await repo.get_by_label(
                project_id=PROJECT_ID, name="blog", label="production"
            )

    async def test_hash_changes_with_variables(self, session: AsyncSession) -> None:
        """同样的模板文本配不同变量声明是不同的 prompt。"""
        repo = PromptRepository(session)
        a = await repo.create(
            project_id=PROJECT_ID, name="p1", template="{{x}}", variables={"x": "a"}
        )
        b = await repo.create(
            project_id=PROJECT_ID, name="p2", template="{{x}}", variables={"x": "b"}
        )
        assert a.content_hash != b.content_hash
