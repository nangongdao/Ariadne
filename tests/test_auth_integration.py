"""PG 侧 RLS 兜底测试 —— 连真实 Postgres，不 mock。

存在理由：SQLite 没有 RLS，所以 test_tenant_isolation.py / test_tenant_context.py
测到的只是应用层过滤（repository 的 project_id WHERE）。RLS 是第二层防线，
它只有在真实 PG 上、以非 owner 角色连接时才存在，也只有在那里才能验证：

- 验收 #3：应用层漏了过滤，DB 侧仍然兜住（发裸查询，断言只看到本租户行）
- 验收 #4：连接归还池后 GUC 不残留（否则下一个请求会继承上一个租户的身份）

同时这里是 ariadne.auth.rls 那两条系统目录查询唯一能被真实执行的地方 ——
row_security_active() / pg_policy 的行为在 SQLite 上无从验证。

运行：
    docker compose up -d postgres
    ariadne-migrate                     # 建表 + 策略 + ariadne_app 角色
    uv run pytest -m integration tests/test_auth_integration.py

无 PG 时整文件 skip。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from ariadne.auth.rls import check_rls
from ariadne.auth.tenant import reset_tenant_context, set_tenant_context
from ariadne.config import PostgresSettings
from ariadne.storage.postgres.engine import PostgresStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

PROJECT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
PROJECT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
ORG = uuid.UUID("00000000-0000-0000-0000-0000000000cc")

_GUC = "ariadne.project_id"
# 用有 RLS 策略且列结构最简单的表做载体：datasets 的策略由 e5f6a7b8c9d0 建立
_PROBE_TABLE = "datasets"


@pytest.fixture(scope="module")
def pg_settings() -> PostgresSettings:
    settings = PostgresSettings()
    if not settings.app_user:
        pytest.skip(
            "ARIADNE_PG_APP_USER 未配置 —— app_dsn() 会回落 owner，"
            "此时 RLS 本就不生效，测它没有意义"
        )
    return settings


@pytest.fixture
async def app_store(pg_settings: PostgresSettings) -> AsyncIterator[PostgresStore]:
    """以非 owner 角色连接（build_engine 用的是 app_dsn）。"""
    store = PostgresStore(pg_settings)
    if not await store.ping():
        await store.close()
        pytest.skip("Postgres 不可用，先 docker compose up -d postgres && ariadne-migrate")
    yield store
    await store.close()


@pytest.fixture
async def seeded(app_store: PostgresStore) -> AsyncIterator[None]:
    """两个租户各插一行。用 owner 连接插入 —— app 角色受策略约束，
    插 B 的数据时得先切 GUC，反而把被测对象绕进了准备步骤。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    owner_engine = create_async_engine(PostgresSettings().dsn(), poolclass=None)
    try:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, plan) "
                    "VALUES (:id, 'rls-it', 'free') ON CONFLICT (id) DO NOTHING"
                ),
                {"id": ORG},
            )
            for pid, slug in ((PROJECT_A, "rls-a"), (PROJECT_B, "rls-b")):
                await conn.execute(
                    text(
                        "INSERT INTO projects (id, org_id, slug, name, settings) "
                        "VALUES (:id, :org, :slug, :slug, '{}'::jsonb) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": pid, "org": ORG, "slug": slug},
                )
                # 逐列写全：id / description 在模型里是 Python 端 default
                # （uuid4 / ""），原生 SQL 不触发；datasets 没有 items 列 ——
                # 模型里的 items 是指向 dataset_items 的 relationship。
                await conn.execute(
                    text(
                        f"INSERT INTO {_PROBE_TABLE} (id, project_id, name, version, "
                        "content_hash, item_count, description) "
                        "VALUES (:did, :pid, 'it-ds', 1, :chash, 0, '') "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"did": uuid.uuid4(), "pid": pid, "chash": "0" * 64},
                )
        yield
    finally:
        async with owner_engine.begin() as conn:
            for pid in (PROJECT_A, PROJECT_B):
                await conn.execute(
                    text(f"DELETE FROM {_PROBE_TABLE} WHERE project_id = :pid"), {"pid": pid}
                )
                await conn.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": pid})
            await conn.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": ORG})
        await owner_engine.dispose()


class TestSelfCheckAgainstRealPostgres:
    """ariadne.auth.rls 的系统目录查询在真实 PG 上跑通 —— SQLite 上测不到。"""

    async def test_self_check_reports_rls_active(self, app_store: PostgresStore) -> None:
        async with app_store.session() as session:
            report = await check_rls(session)

        assert report is not None, "PG 方言下不该短路"
        # 空清单是故障而非通过，problems() 会显式报出来
        assert report.tables, "没枚举到任何带 project_id 的表 —— 迁移没跑完"
        assert report.ok, f"RLS 未生效：{report.problems()}"

    async def test_app_role_is_not_owner_and_has_no_bypass(
        self, app_store: PostgresStore
    ) -> None:
        """三条绕过路径逐条否掉 —— 这是 RLS 能生效的前提。"""
        async with app_store.session() as session:
            report = await check_rls(session)

        assert report is not None
        assert not report.is_superuser
        assert not report.bypasses_rls
        assert not [t.table for t in report.tables if t.is_owner], (
            "app 角色是某些表的 owner，owner 默认绕过自己表上的策略"
        )
        assert all(t.rls_enabled and t.has_policy for t in report.tables)


class TestRlsBacksUpMissingAppFilter:
    """验收 #3：应用层漏写 WHERE project_id 时，DB 侧仍然兜住。"""

    async def test_unfiltered_query_sees_only_current_tenant(
        self, app_store: PostgresStore, seeded: None
    ) -> None:
        """刻意发不带 project_id 的裸查询 —— 应用层这一层被完全跳过。"""
        async with app_store.tenant_session(PROJECT_A) as session:
            rows = (
                await session.execute(text(f"SELECT project_id FROM {_PROBE_TABLE}"))
            ).scalars().all()

        assert rows, "本租户的数据应当可见，否则测的是「全都读不到」而非隔离"
        assert set(rows) == {PROJECT_A}, f"越过应用层过滤后看到了别的租户：{set(rows)}"

    async def test_unfiltered_count_excludes_other_tenant(
        self, app_store: PostgresStore, seeded: None
    ) -> None:
        """两租户各有数据，裸 COUNT 只能数到一行。"""
        async with app_store.tenant_session(PROJECT_A) as session:
            count = await session.scalar(text(f"SELECT count(*) FROM {_PROBE_TABLE}"))
        assert count == 1, f"期望只看到 A 的 1 行，实际 {count}"

    async def test_cross_tenant_update_touches_nothing(
        self, app_store: PostgresStore, seeded: None
    ) -> None:
        """写侧同样受策略约束：带 B 的 id 也改不动。"""
        async with app_store.tenant_session(PROJECT_A) as session:
            result = await session.execute(
                text(f"UPDATE {_PROBE_TABLE} SET name = 'hijacked' WHERE project_id = :pid"),
                {"pid": PROJECT_B},
            )
        assert result.rowcount == 0

    async def test_no_guc_means_no_rows(self, app_store: PostgresStore, seeded: None) -> None:
        """GUC 未设时策略表达式为 NULL —— 默认拒绝，而不是默认全开。"""
        async with app_store.session() as session:
            rows = (
                await session.execute(text(f"SELECT project_id FROM {_PROBE_TABLE}"))
            ).scalars().all()
        assert rows == [], f"没设租户上下文却读到了数据：{rows}"


class TestGucDoesNotLeakAcrossConnections:
    """验收 #4：连接复用不串号。"""

    async def test_guc_gone_after_session_returned(
        self, app_store: PostgresStore, seeded: None
    ) -> None:
        """池化连接被复用时不该继承上一个租户的身份。"""
        async with app_store.tenant_session(PROJECT_A) as session:
            current = await session.scalar(text(f"SELECT current_setting('{_GUC}', true)"))
        assert str(current) == str(PROJECT_A)

        # 新 session 可能拿到同一条物理连接，GUC 必须已经不在
        async with app_store.session() as session:
            leaked = await session.scalar(text(f"SELECT current_setting('{_GUC}', true)"))
        assert not leaked, f"GUC 残留：{leaked!r}"

    async def test_reset_clears_guc_within_same_session(
        self, app_store: PostgresStore
    ) -> None:
        async with app_store.session() as session:
            await set_tenant_context(session, PROJECT_A)
            assert await session.scalar(text(f"SELECT current_setting('{_GUC}', true)"))
            await reset_tenant_context(session)
            assert not await session.scalar(text(f"SELECT current_setting('{_GUC}', true)"))

    async def test_guc_is_transaction_local(self, app_store: PostgresStore) -> None:
        """set_config(..., true) 的第三个参数是 is_local —— 事务结束即失效。

        这条是 #4 的根因保障：即使 reset 那步漏了，回滚/提交也会带走它。
        """
        async with app_store.session() as session:
            await set_tenant_context(session, PROJECT_A)
            await session.rollback()
            assert not await session.scalar(text(f"SELECT current_setting('{_GUC}', true)"))


class TestApiKeyLookupFunction:
    """跨租户前缀解析走 SECURITY DEFINER 函数 —— 它是刻意绕过 RLS 的那一处。"""

    async def test_lookup_function_is_callable_by_app_role(
        self, app_store: PostgresStore
    ) -> None:
        from ariadne.auth.key_lookup import resolve_projects_by_prefix

        async with app_store.session() as session:
            # 不存在的前缀返回空列表而不是报权限错，说明函数可执行
            assert await resolve_projects_by_prefix(session, "ak_nonexistent") == []

    async def test_public_cannot_execute_lookup_function(
        self, app_store: PostgresStore
    ) -> None:
        """SECURITY DEFINER 函数对 PUBLIC 开放等于把跨租户查询暴露给任意角色。"""
        async with app_store.session() as session:
            granted = await session.scalar(
                text(
                    "SELECT has_function_privilege('public', "
                    "'public.ariadne_api_key_projects(text)', 'EXECUTE')"
                )
            )
        assert granted is False, "PUBLIC 不该能执行跨租户查找函数"


class TestSchemaMatchesSelfCheckAssumption:
    """自检从"有 project_id 列"反推"该有 RLS"。这个前提在真实 schema 上成立吗。"""

    async def test_every_project_scoped_table_has_rls(
        self, app_store: PostgresStore
    ) -> None:
        async with app_store.session() as session:
            report = await check_rls(session)
        assert report is not None
        missing = [t.table for t in report.tables if not (t.rls_enabled and t.has_policy)]
        assert not missing, f"这些表有 project_id 却没有生效的策略：{missing}"

    async def test_no_rls_table_lacks_project_id(self, app_store: PostgresStore) -> None:
        """反向：开了 RLS 但没有 project_id 列的表，说明策略挂错了对象。"""
        async with app_store.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relkind = 'r' AND n.nspname = 'public' "
                        "  AND c.relrowsecurity "
                        "  AND NOT EXISTS ("
                        "      SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
                        "        AND a.attname = 'project_id' AND a.attnum > 0 "
                        "        AND NOT a.attisdropped)"
                    )
                )
            ).scalars().all()
        assert rows == [], f"这些表开了 RLS 却没有 project_id 列：{rows}"


class TestOwnerStillBypasses:
    """记录 owner 的行为，说明为什么应用必须连非 owner。"""

    async def test_owner_connection_sees_all_tenants(
        self, pg_settings: PostgresSettings, seeded: None
    ) -> None:
        """同一条裸查询，owner 连接能看到两个租户 —— 这就是 #15 要消掉的东西。"""
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(pg_settings.dsn())
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(text(f"SELECT project_id FROM {_PROBE_TABLE}"))
                ).scalars().all()
        finally:
            await engine.dispose()

        assert {PROJECT_A, PROJECT_B} <= set(rows), (
            "owner 竟然被策略挡住了 —— 要么设了 FORCE ROW LEVEL SECURITY，"
            "要么这个连接不是 owner，两种情况本测试的前提都得改"
        )
