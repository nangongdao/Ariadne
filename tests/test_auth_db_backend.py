"""db 认证后端测试（auth_backend="db"）。

这条路径此前零端到端覆盖，三个缺陷因此长期存活：
1. 撞前缀时 scalar_one_or_none() 抛 MultipleResultsFound → 500（外部可诱发）
2. 注释声称前缀查找"绕过 RLS"，实际没有任何东西绕过 —— 换非 owner 角色后全 401
3. 过期判断内联比较 aware/naive datetime，SQLite 下抛 TypeError → 500 而非 401

所以本文件的重点不是"能认过"，而是这三条各自的回归锚点。

key 一律手工插入而非走 repo.create()：后者前缀随机，撞前缀构造不出来。
Argon2id 每次哈希/验证约 50–100ms，故用例数量刻意压到最小。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from ariadne.api.deps import _auth_db, _auth_db_tenant
from ariadne.api.errors import UnauthorizedError
from ariadne.auth.key_lookup import resolve_projects_by_prefix
from ariadne.auth.keys import extract_prefix, hash_api_key
from ariadne.auth.rbac import Role
from ariadne.config import ApiSettings, ClickHouseSettings, RedisSettings, Settings
from ariadne.storage.postgres.auth_models import ApiKey
from ariadne.storage.postgres.models import Project

TEST_PROJECT = uuid.UUID("00000000-0000-0000-0000-000000000001")
TEST_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
OTHER_PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

# 前 16 字符完全相同，后缀不同 —— uq_api_key_prefix 是
# (project_id, key_prefix) 复合唯一，所以这在两个项目下是合法数据。
COLLIDING_A = "ak_live_collide0_aaaaaaaaaaaa"
COLLIDING_B = "ak_live_collide0_bbbbbbbbbbbb"


async def _insert_key(
    pg: Any,
    *,
    plain: str,
    project_id: uuid.UUID = TEST_PROJECT,
    role: str = "viewer",
    is_active: bool = True,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """按给定明文插入 key，返回 key_id。

    不用 repo.create()：它自己生成明文，前缀随机，撞前缀场景构造不出来。
    """
    key_id = uuid.uuid4()
    async with pg.session() as session:
        session.add(
            ApiKey(
                id=key_id,
                project_id=project_id,
                key_hash=hash_api_key(plain),
                key_prefix=extract_prefix(plain),
                name=f"key-{role}",
                role=role,
                scopes={},
                is_active=is_active,
                expires_at=expires_at,
            )
        )
    return key_id


async def _add_project(pg: Any, project_id: uuid.UUID, slug: str) -> None:
    """补一个项目 —— api_keys.project_id 有外键，撞前缀需要第二个租户。"""
    async with pg.session() as session:
        session.add(
            Project(id=project_id, org_id=TEST_ORG, slug=slug, name=slug)
        )


def _fake_request(pg: Any) -> Any:
    """只带 _auth_db_tenant 真正会碰的两处状态：app.state.pg 和 request.state。"""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(pg=pg)),
        state=SimpleNamespace(),
    )


class TestAuthDbHappyPath:
    async def test_valid_key_returns_tenant_context(self, memory_pg: Any) -> None:
        plain = "ak_live_valid001_secret_tail"
        key_id = await _insert_key(memory_pg, plain=plain, role="developer")

        request = _fake_request(memory_pg)
        ctx = await _auth_db_tenant(request, plain)

        assert ctx.project_id == TEST_PROJECT
        assert ctx.role is Role.DEVELOPER
        # 依赖链下游（get_tenant_pg 之外的路由）从 request.state 读
        assert request.state.tenant_context is ctx

        # _touch_last_used 走独立会话，认证成功后该字段必须已落库
        async with memory_pg.session() as session:
            row = await session.get(ApiKey, key_id)
            assert row is not None
            assert row.last_used_at is not None

    async def test_auth_db_returns_project_id(self, memory_pg: Any) -> None:
        """_auth_db 是 _auth_db_tenant 的薄包装（require_project 走它）。"""
        plain = "ak_live_projid1_secret_tail"
        await _insert_key(memory_pg, plain=plain)

        project_id = await _auth_db(_fake_request(memory_pg), plain)
        assert project_id == TEST_PROJECT


class TestPrefixCollision:
    """回归：撞前缀曾让 scalar_one_or_none() 抛 MultipleResultsFound → 500。

    前缀取明文前 16 字符，其中仅 7 字符来自随机段（约 42 bit），
    按生日界 ~2^21 把 key 就可能撞上 —— 不是不会发生的事。
    """

    @pytest.fixture
    async def two_projects(self, memory_pg: Any) -> Any:
        await _add_project(memory_pg, OTHER_PROJECT, "other")
        await _insert_key(
            memory_pg, plain=COLLIDING_A, project_id=TEST_PROJECT, role="admin"
        )
        await _insert_key(
            memory_pg, plain=COLLIDING_B, project_id=OTHER_PROJECT, role="viewer"
        )
        return memory_pg

    async def test_collision_is_real_not_a_test_artifact(self, two_projects: Any) -> None:
        """先证明前缀查找确实返回两个候选，否则下面的断言不说明任何问题。"""
        async with two_projects.session() as session:
            candidates = await resolve_projects_by_prefix(
                session, extract_prefix(COLLIDING_A)
            )
        assert extract_prefix(COLLIDING_A) == extract_prefix(COLLIDING_B)
        assert set(candidates) == {TEST_PROJECT, OTHER_PROJECT}

    async def test_each_colliding_key_authenticates_to_own_project(
        self, two_projects: Any
    ) -> None:
        """候选顺序不定，所以至少一把 key 必然先撞上另一个项目的哈希。

        那次 verify 失败要走 continue 继续试下一个候选，而不是直接 401。
        """
        ctx_a = await _auth_db_tenant(_fake_request(two_projects), COLLIDING_A)
        assert ctx_a.project_id == TEST_PROJECT
        assert ctx_a.role is Role.ADMIN

        ctx_b = await _auth_db_tenant(_fake_request(two_projects), COLLIDING_B)
        assert ctx_b.project_id == OTHER_PROJECT
        assert ctx_b.role is Role.VIEWER

    async def test_matching_prefix_wrong_secret_is_401(self, two_projects: Any) -> None:
        """前缀命中两个候选、但两个哈希都对不上 —— 应当 401，不是 500。"""
        forged = "ak_live_collide0_zzzzzzzzzzzz"
        assert extract_prefix(forged) == extract_prefix(COLLIDING_A)

        with pytest.raises(UnauthorizedError, match="无效"):
            await _auth_db_tenant(_fake_request(two_projects), forged)


class TestAuthDbRejections:
    async def test_expired_key_is_401_not_type_error(self, memory_pg: Any) -> None:
        """回归：SQLite 的 DateTime 不存时区，读回来是 naive。

        原先内联 `datetime.now(UTC) > row.expires_at` 会抛 TypeError（→ 500）。
        现在走 repo.is_expired 归一化，应当是干净的 401。
        """
        plain = "ak_live_expired1_secret_tail"
        await _insert_key(
            memory_pg, plain=plain, expires_at=datetime.now(UTC) - timedelta(days=1)
        )

        with pytest.raises(UnauthorizedError, match="已过期"):
            await _auth_db_tenant(_fake_request(memory_pg), plain)

    async def test_future_expiry_still_authenticates(self, memory_pg: Any) -> None:
        """过期判断的另一侧：设了 expires_at 但还没到，不该被拒。"""
        plain = "ak_live_future01_secret_tail"
        await _insert_key(
            memory_pg, plain=plain, expires_at=datetime.now(UTC) + timedelta(days=7)
        )

        ctx = await _auth_db_tenant(_fake_request(memory_pg), plain)
        assert ctx.project_id == TEST_PROJECT

    async def test_revoked_key_is_401(self, memory_pg: Any) -> None:
        """is_active=False 在第一步前缀查找就被滤掉，候选为空。"""
        plain = "ak_live_revoked1_secret_tail"
        await _insert_key(memory_pg, plain=plain, is_active=False)

        with pytest.raises(UnauthorizedError, match="无效"):
            await _auth_db_tenant(_fake_request(memory_pg), plain)

    async def test_unknown_prefix_is_401(self, memory_pg: Any) -> None:
        with pytest.raises(UnauthorizedError, match="无效"):
            await _auth_db_tenant(
                _fake_request(memory_pg), "ak_live_nosuchk_secret_tail"
            )


class TestAuthDbOverHttp:
    """端到端：整条依赖链（require_tenant → get_tenant_pg → 路由）。

    用 ASGITransport 而非 TestClient：后者在独立线程的 portal 循环里驱动 app，
    而 memory_pg 的 async engine 绑在 pytest-asyncio 的循环上，跨循环用同一个
    连接池会炸。这里 app 与 engine 同循环。
    """

    @pytest.fixture
    def db_app(self, fake_queue: Any, fake_store: Any, memory_pg: Any) -> Any:
        from ariadne.api.app import create_app

        settings = Settings(
            env="test",
            log_level="WARNING",
            clickhouse=ClickHouseSettings(host="localhost", database="ariadne_test"),
            redis=RedisSettings(url="redis://localhost:6379/15"),
            api=ApiSettings(
                auth_backend="db",
                default_project_id=TEST_PROJECT,
                static_api_key=SecretStr("unused-in-db-mode"),
            ),
        )
        application = create_app(settings)
        application.state.queue = fake_queue
        application.state.store = fake_store
        application.state.pg = memory_pg
        return application

    async def test_static_key_rejected_in_db_mode(self, db_app: Any) -> None:
        """先确认 db 后端真的在生效，否则下面的 200 可能是静态路径在放行。"""
        async with AsyncClient(
            transport=ASGITransport(app=db_app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/keys", headers={"X-Ariadne-Key": "unused-in-db-mode"}
            )

        assert resp.status_code == 401
        assert resp.json()["type"].endswith("/unauthorized")

    async def test_admin_key_lists_own_keys(
        self, db_app: Any, memory_pg: Any
    ) -> None:
        plain = "ak_live_httpadm_secret_tail"
        key_id = await _insert_key(memory_pg, plain=plain, role="admin")

        async with AsyncClient(
            transport=ASGITransport(app=db_app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/keys", headers={"X-Ariadne-Key": plain})

        assert resp.status_code == 200
        assert [k["id"] for k in resp.json()["keys"]] == [str(key_id)]

    async def test_collision_over_http_is_200_not_500(
        self, db_app: Any, memory_pg: Any
    ) -> None:
        """撞前缀的 500 是外部可诱发的 —— 这条是它的 HTTP 层锚点。"""
        await _add_project(memory_pg, OTHER_PROJECT, "other")
        await _insert_key(
            memory_pg, plain=COLLIDING_A, project_id=TEST_PROJECT, role="admin"
        )
        key_b = await _insert_key(
            memory_pg, plain=COLLIDING_B, project_id=OTHER_PROJECT, role="admin"
        )

        async with AsyncClient(
            transport=ASGITransport(app=db_app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/keys", headers={"X-Ariadne-Key": COLLIDING_B})

        assert resp.status_code == 200
        # 只看到自己项目的 key —— 顺带证明 TenantPg 的 project_id 取自认证结果
        assert [k["id"] for k in resp.json()["keys"]] == [str(key_b)]
