"""RBAC API 权限测试 —— 各路由端点的权限拒绝/通过。

用 FastAPI dependency_overrides 注入不同角色的 TenantContext，
不依赖 DB 后端。static 模式下默认是 admin（全权限），
这里覆盖 require_tenant 返回特定角色来测试拒绝路径。
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from ariadne.auth.rbac import Role
from ariadne.auth.tenant import TenantContext

TEST_PROJECT = UUID("00000000-0000-0000-0000-000000000001")


def _override_tenant(role: Role) -> Any:
    """构造一个 require_tenant override，返回指定角色的 TenantContext。"""
    from ariadne.api.deps import require_tenant

    async def _tenant() -> TenantContext:
        return TenantContext(project_id=TEST_PROJECT, role=role)

    return require_tenant, _tenant


class TestApprovalsRBAC:
    """审批路由权限测试 —— §4.1 职责分离。"""

    def test_admin_can_create_approval(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.ADMIN)[1]
        resp = client.post(
            f"/v1/loops/{TEST_PROJECT}/approvals",
            json={"hook": "pre_model", "reason": "test"},
            headers={"X-Ariadne-Key": "test"},
        )
        # 201 或 422（loop 不存在）—— 不是 403 就行
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()

    def test_viewer_cannot_create_approval(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.post(
            f"/v1/loops/{TEST_PROJECT}/approvals",
            json={"hook": "pre_model", "reason": "test"},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        assert resp.json()["type"].endswith("/forbidden")
        client.app.dependency_overrides.clear()

    def test_approver_cannot_create_approval(self, client: Any) -> None:
        """approver 有 APPROVE 但无 WRITE —— 不能提交审批请求。"""
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.APPROVER)[1]
        resp = client.post(
            f"/v1/loops/{TEST_PROJECT}/approvals",
            json={"hook": "pre_model", "reason": "test"},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()

    def test_viewer_can_list_approvals(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.get(
            f"/v1/loops/{TEST_PROJECT}/approvals",
            headers={"X-Ariadne-Key": "test"},
        )
        # 200 或 404（loop 不存在）—— 不是 403 就行
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()


class TestApprovalDecideRBAC:
    """审批决策的职责分离 —— 端到端跑通一次真实审批。

    §4.1 的约束有两个方向，只测拒绝路径证不完：
    - approver 没有 WRITE，但必须能批准（否则这个角色毫无用处）
    - developer 有 WRITE，但不能批准自己触发的操作

    所以这里真的建 loop、建审批、再决策，而不是拿不存在的 ID 看是否 403。
    """

    GOAL: ClassVar[dict[str, Any]] = {
        "task": "写一个返回两数之和的 Python 函数",
        "mode": "quality",
        "assertions": [
            {
                "id": "has_def",
                "kind": "regex",
                "spec": {"pattern": r"def\s+\w+\s*\("},
                "hint": "输出必须包含一个函数定义",
            }
        ],
        "budget": {"max_iterations": 5, "max_total_tokens": 50000},
    }

    def _pending_approval(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> tuple[str, str]:
        """建 loop 并挂一个 pending 审批，返回 (审批 ID, loop ID)。"""
        from ariadne.api.routers import loops as loops_router

        async def _noop(settings: Any, loop_id: Any, project_id: Any) -> None:
            return None

        # 入队会连 Redis，与审批权限无关
        monkeypatch.setattr(loops_router, "_enqueue", _noop)

        created = client.post("/v1/loops", headers=auth, json=self.GOAL)
        assert created.status_code == 202, created.text
        loop_id = created.json()["loop_id"]

        approval = client.post(
            f"/v1/loops/{loop_id}/approvals",
            headers=auth,
            json={"context": {"diff": "+1 line"}, "expires_in_seconds": 3600},
        )
        assert approval.status_code == 201, approval.text
        body = approval.json()
        assert body["status"] == "pending"
        return str(body["id"]), str(body["loop_id"])

    def _decide(self, client: Any, approval_id: str, role: Role) -> Any:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(role)[1]
        try:
            return client.post(
                f"/v1/approvals/{approval_id}/decide",
                json={"decision": "approved", "reviewer": "alice", "comment": "ok"},
                headers={"X-Ariadne-Key": "test"},
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_approver_can_approve_without_write(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """approver 只有 READ+APPROVE，照样要能把审批推到 approved。"""
        approval_id, _ = self._pending_approval(client, auth, monkeypatch)
        resp = self._decide(client, approval_id, Role.APPROVER)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"
        assert body["reviewer"] == "alice"
        assert body["decided_at"]

    def test_approval_decision_resumes_human_pending_loop(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """正式审批 API 决定后，HUMAN_PENDING Loop 必须重新入队。"""
        import asyncio

        from sqlalchemy import select

        from ariadne.api.deps import require_tenant
        from ariadne.loop_module.state_machine import LoopState
        from ariadne.storage.postgres.loop_models import LoopRun

        approval_id, loop_id = self._pending_approval(client, auth, monkeypatch)

        async def _mark_pending() -> None:
            async with client.app.state.pg.session() as session:
                row = (
                    await session.execute(
                        select(LoopRun).where(LoopRun.id == UUID(loop_id))
                    )
                ).scalar_one()
                row.state = LoopState.HUMAN_PENDING.value

        asyncio.run(_mark_pending())
        enqueued: list[tuple[str, str]] = []

        async def _enqueue(settings: Any, loop_id: Any, project_id: Any) -> None:
            enqueued.append((str(loop_id), str(project_id)))

        from ariadne.api.routers import loops as loops_router

        monkeypatch.setattr(loops_router, "_enqueue", _enqueue)
        client.app.dependency_overrides[require_tenant] = _override_tenant(
            Role.APPROVER
        )[1]
        try:
            response = client.post(
                f"/v1/approvals/{approval_id}/decide",
                json={"decision": "approved", "reviewer": "alice"},
                headers=auth,
            )
        finally:
            client.app.dependency_overrides.clear()

        assert response.status_code == 200, response.text
        assert enqueued == [(loop_id, str(TEST_PROJECT))]
        async def _read_state() -> str:
            async with client.app.state.pg.session() as session:
                row = (
                    await session.execute(
                        select(LoopRun.state).where(LoopRun.id == UUID(loop_id))
                    )
                ).scalar_one()
                return str(row)

        assert asyncio.run(_read_state()) == LoopState.EXECUTING.value

    def test_developer_cannot_approve(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """developer 有 WRITE 但无 APPROVE —— 写代码的人不能批自己的操作。"""
        approval_id, loop_id = self._pending_approval(client, auth, monkeypatch)
        resp = self._decide(client, approval_id, Role.DEVELOPER)
        assert resp.status_code == 403
        assert resp.json()["type"].endswith("/forbidden")

        # 被拒后审批必须还是 pending，不能被半途改掉
        listed = client.get(f"/v1/loops/{loop_id}/approvals", headers=auth).json()
        assert [a["status"] for a in listed] == ["pending"]

    def test_viewer_cannot_approve(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        approval_id, _ = self._pending_approval(client, auth, monkeypatch)
        assert self._decide(client, approval_id, Role.VIEWER).status_code == 403

    def test_second_decision_conflicts(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """审批不可重复决策 —— 否则批准后还能被改成拒绝。"""
        approval_id, _ = self._pending_approval(client, auth, monkeypatch)
        assert self._decide(client, approval_id, Role.APPROVER).status_code == 200
        again = self._decide(client, approval_id, Role.APPROVER)
        assert again.status_code == 400
        assert "已处理" in again.json()["detail"]


class TestLegacyLoopApproveRBAC:
    """旧版 Loop 审批入口也必须遵守 APPROVE 职责分离。"""

    GOAL: ClassVar[dict[str, Any]] = {
        "task": "写一个返回两数之和的 Python 函数",
        "mode": "quality",
        "assertions": [
            {
                "id": "has_def",
                "kind": "regex",
                "spec": {"pattern": r"def\s+\w+\s*\("},
            }
        ],
    }

    def test_approver_can_use_legacy_approve_endpoint(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        from ariadne.api.deps import require_tenant
        from ariadne.api.routers import loops as loops_router

        async def _noop(settings: Any, loop_id: Any, project_id: Any) -> None:
            return None

        monkeypatch.setattr(loops_router, "_enqueue", _noop)
        created = client.post("/v1/loops", headers=auth, json=self.GOAL)
        assert created.status_code == 202, created.text
        loop_id = created.json()["loop_id"]

        client.app.dependency_overrides[require_tenant] = _override_tenant(
            Role.APPROVER
        )[1]
        try:
            response = client.post(
                f"/v1/loops/{loop_id}/approve",
                headers=auth,
                json={"approved": True},
            )
        finally:
            client.app.dependency_overrides.clear()

        assert response.status_code == 200, response.text

    def test_developer_cannot_use_legacy_approve_endpoint(
        self, client: Any, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        from ariadne.api.deps import require_tenant
        from ariadne.api.routers import loops as loops_router

        async def _noop(settings: Any, loop_id: Any, project_id: Any) -> None:
            return None

        monkeypatch.setattr(loops_router, "_enqueue", _noop)
        created = client.post("/v1/loops", headers=auth, json=self.GOAL)
        assert created.status_code == 202, created.text
        loop_id = created.json()["loop_id"]

        client.app.dependency_overrides[require_tenant] = _override_tenant(
            Role.DEVELOPER
        )[1]
        try:
            response = client.post(
                f"/v1/loops/{loop_id}/approve",
                headers=auth,
                json={"approved": True},
            )
        finally:
            client.app.dependency_overrides.clear()

        assert response.status_code == 403


class TestCostsRBAC:
    """成本路由权限测试 —— VIEW_BILLING。"""

    def test_billing_can_read_costs(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.BILLING)[1]
        resp = client.get(
            "/v1/costs",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()

    def test_viewer_cannot_read_costs(self, client: Any) -> None:
        """viewer 有 READ 但无 VIEW_BILLING。"""
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.get(
            "/v1/costs",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()

    def test_developer_cannot_read_costs(self, client: Any) -> None:
        """developer 有 WRITE/READ 但无 VIEW_BILLING。"""
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.DEVELOPER)[1]
        resp = client.get(
            "/v1/costs",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()


class TestRulesRBAC:
    """规则路由权限测试 —— MANAGE_RULES。"""

    def test_developer_can_update_rules(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.DEVELOPER)[1]
        resp = client.put(
            "/v1/rules",
            json={"name": "default", "rules": []},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()

    def test_viewer_cannot_update_rules(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.put(
            "/v1/rules",
            json={"name": "default", "rules": []},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()

    def test_approver_cannot_update_rules(self, client: Any) -> None:
        """approver 有 APPROVE 但无 MANAGE_RULES。"""
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.APPROVER)[1]
        resp = client.put(
            "/v1/rules",
            json={"name": "default", "rules": []},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()

    def test_viewer_can_list_rules(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.get(
            "/v1/rules",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()


class TestKeysRBAC:
    """API Key 管理权限测试 —— MANAGE_KEYS。"""

    def test_admin_can_list_keys(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.ADMIN)[1]
        resp = client.get(
            "/v1/keys",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()

    def test_developer_cannot_list_keys(self, client: Any) -> None:
        """developer 无 MANAGE_KEYS 权限。"""
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.DEVELOPER)[1]
        resp = client.get(
            "/v1/keys",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()

    def test_viewer_cannot_create_keys(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.post(
            "/v1/keys",
            json={"name": "test", "role": "viewer"},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()


class TestExperimentsRBAC:
    """实验路由权限测试 —— 对比是只读操作。"""

    FAKE = "00000000-0000-0000-0000-0000000000ff"

    def _compare(self, client: Any, role: Role) -> Any:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(role)[1]
        try:
            return client.post(
                "/v1/experiments/compare",
                json={"baseline_id": self.FAKE, "current_id": self.FAKE},
                headers={"X-Ariadne-Key": "test"},
            )
        finally:
            client.app.dependency_overrides.clear()

    def test_read_only_roles_can_compare(self, client: Any) -> None:
        """看质量报告正是 viewer/approver/billing 的日常，不该要 WRITE。

        实验不存在时应是 404 —— 说明权限已放行、查询真的执行了。
        """
        for role in (Role.VIEWER, Role.APPROVER, Role.BILLING):
            resp = self._compare(client, role)
            assert resp.status_code == 404, f"{role} 被挡在对比之外: {resp.status_code}"

    def test_viewer_cannot_submit_result(self, client: Any) -> None:
        """只读放行的是对比，写结果仍要 WRITE。"""
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.post(
            f"/v1/experiments/{self.FAKE}/result",
            json={"result": {}},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()

    def test_viewer_cannot_put_snapshot(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.put(
            f"/v1/experiments/{self.FAKE}/snapshot",
            json={"result": {}},
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code == 403
        client.app.dependency_overrides.clear()


class TestAuditRBAC:
    """审计路由权限测试 —— READ。"""

    def test_viewer_can_read_audit(self, client: Any) -> None:
        from ariadne.api.deps import require_tenant

        client.app.dependency_overrides[require_tenant] = _override_tenant(Role.VIEWER)[1]
        resp = client.get(
            "/v1/audit",
            headers={"X-Ariadne-Key": "test"},
        )
        assert resp.status_code != 403
        client.app.dependency_overrides.clear()
