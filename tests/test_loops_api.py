"""Loop API 测试。

覆盖 9 个端点中的核心行为（用内存 pg 桩 + 注入，离线可跑）：
- 创建（202 + 目标可验证性 422 拒绝）
- 列表 / 详情
- 取消（含已终态冲突 409）
- 审批（批准重入队 / 拒绝转 REJECTED）
- 轮次记录

注意：_enqueue 会连 Redis，测试里 monkeypatch 成 no-op —— 入队是
旁路，创建成功与否不依赖队列（逻辑已在 worker 测试覆盖真队列）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

from ariadne.api.routers import loops as loops_router
from ariadne.storage.postgres.repositories.loop_runs import LoopRunRepository

VALID_GOAL = {
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


def _disable_enqueue(monkeypatch: Any) -> None:
    """入队打桩：避免测试连 Redis。"""
    async def _noop(
        settings: Any, loop_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        return None

    monkeypatch.setattr(loops_router, "_enqueue", _noop)


class TestCreateLoop:
    def test_create_returns_202(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        response = client.post("/v1/loops", headers=auth, json=VALID_GOAL)
        assert response.status_code == 202
        body = response.json()
        assert uuid.UUID(body["loop_id"])
        assert body["state"] == "VALIDATE"
        assert body["stream_url"] == f"/v1/loops/{body['loop_id']}/stream"

    def test_create_unverifiable_goal_rejected(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """目标不可验证 → 422（RFC 9457 problem+json）。"""
        _disable_enqueue(monkeypatch)
        # 空断言 / 全 non-blocking
        payload = {
            "task": "把代码优化一下",
            "assertions": [
                {
                    "id": "lint",
                    "kind": "regex",
                    "spec": {"pattern": "x"},
                    "blocking": False,
                }
            ],
        }
        response = client.post("/v1/loops", headers=auth, json=payload)
        assert response.status_code == 422
        body = response.json()
        assert body["type"].endswith("/unverifiable-goal")

    def test_create_unknown_mode_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        payload = {**VALID_GOAL, "mode": "nonexistent"}
        response = client.post("/v1/loops", headers=auth, json=payload)
        assert response.status_code == 422

    def test_create_command_assertion_accepted(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """COMMAND 断言目标必须被接受 —— 代码生成是 M3 的标杆场景。

        这条曾经断言 422（"无沙箱时 COMMAND 断言不可验证"），把一个缺陷
        固化成了契约：`validate_goal` 的 `sandbox_available` 在 API 层硬编码
        False，于是**任何带 COMMAND 断言的 Loop 在创建时就被拒**，而 COMMAND
        是"信号最硬的那类断言"。Worker 现在会为这类目标建工作目录
        （`LoopWorker._prepare_workspace`），所以正确契约是接受。
        """
        _disable_enqueue(monkeypatch)
        payload = {
            "task": "跑测试",
            "assertions": [
                {"id": "tests", "kind": "command", "spec": {"cmd": "pytest -q"}}
            ],
            "workspace": {
                "solution.py": "def add(a, b):\n    return a - b\n",
                "test_solution.py": "from solution import add\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n",
            },
        }
        response = client.post("/v1/loops", headers=auth, json=payload)
        assert response.status_code == 202, response.text

    @pytest.mark.parametrize(
        "bad_path",
        ["../escaped.py", "/etc/passwd", "C:\\Windows\\evil.py", "a/../../b.py"],
    )
    def test_workspace_path_traversal_rejected(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any, bad_path: str
    ) -> None:
        """种子文件路径来自请求体，越界要在创建时就 422。"""
        _disable_enqueue(monkeypatch)
        payload = {
            "task": "跑测试",
            "assertions": [
                {"id": "tests", "kind": "command", "spec": {"cmd": "pytest -q"}}
            ],
            "workspace": {bad_path: "EVIL = 1\n"},
        }
        response = client.post("/v1/loops", headers=auth, json=payload)
        assert response.status_code == 422

    def test_workspace_file_size_limit_rejected(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        """种子文件大小必须在 API 边界拒绝，避免 Worker 写入超大请求体。"""
        from ariadne.loop_module.artifact import MAX_BYTES_PER_FILE

        _disable_enqueue(monkeypatch)
        payload = {
            "task": "跑测试",
            "assertions": [
                {"id": "tests", "kind": "command", "spec": {"cmd": "pytest -q"}}
            ],
            "workspace": {"solution.py": "x" * (MAX_BYTES_PER_FILE + 1)},
        }
        response = client.post("/v1/loops", headers=auth, json=payload)
        assert response.status_code == 422

    def test_create_requires_auth(self, client: TestClient) -> None:
        response = client.post("/v1/loops", json=VALID_GOAL)
        assert response.status_code == 401


class TestLoopListing:
    def test_list_empty(self, client: TestClient, auth: dict[str, str]) -> None:
        response = client.get("/v1/loops", headers=auth)
        assert response.status_code == 200
        assert response.json() == []

    def test_list_after_create(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        created = client.post("/v1/loops", headers=auth, json=VALID_GOAL)
        loop_id = created.json()["loop_id"]

        response = client.get("/v1/loops", headers=auth)
        assert response.status_code == 200
        items = response.json()
        assert any(i["id"] == loop_id for i in items)

    def test_filter_by_state(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        client.post("/v1/loops", headers=auth, json=VALID_GOAL)
        response = client.get("/v1/loops", headers=auth, params={"state": "VALIDATE"})
        assert all(i["state"] == "VALIDATE" for i in response.json())


class TestLoopDetail:
    def test_get_detail(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        loop_id = client.post("/v1/loops", headers=auth, json=VALID_GOAL).json()["loop_id"]

        response = client.get(f"/v1/loops/{loop_id}", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == loop_id
        assert body["state"] == "VALIDATE"
        assert body["goal"]["task"] == "写一个返回两数之和的 Python 函数"

    def test_get_missing_404(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.get(
            f"/v1/loops/{uuid.uuid4()}", headers=auth
        )
        assert response.status_code == 404


class TestCancel:
    def test_cancel_active_loop(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        loop_id = client.post("/v1/loops", headers=auth, json=VALID_GOAL).json()["loop_id"]
        response = client.post(f"/v1/loops/{loop_id}/cancel", headers=auth)
        assert response.status_code == 200
        assert response.json()["state"] == "CANCELLED"
        assert response.json()["final_state"] == "CANCELLED"

    def test_cancel_terminal_conflicts(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any, app: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        loop_id = client.post("/v1/loops", headers=auth, json=VALID_GOAL).json()["loop_id"]
        # 直接落终态（绕过 API，模拟 Worker 已完成）
        import asyncio


        pg = app.state.pg
        asyncio.run(_finish_loop(pg, loop_id))

        response = client.post(f"/v1/loops/{loop_id}/cancel", headers=auth)
        assert response.status_code == 409


async def _finish_loop(pg: Any, loop_id: str) -> None:
    from ariadne.loop_module.state_machine import LoopState

    async with pg.session() as session:
        await LoopRunRepository(session).finish(
            loop_id=uuid.UUID(loop_id),
            final_state=LoopState.CONVERGED,
            worker_id="test",
        )


class TestApprove:
    def test_approve_re_enqueues(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        enqueued: list[tuple[str, str]] = []

        async def _fake_enqueue(
            settings: Any, loop_id: uuid.UUID, project_id: uuid.UUID
        ) -> None:
            enqueued.append((str(loop_id), str(project_id)))

        monkeypatch.setattr(loops_router, "_enqueue", _fake_enqueue)
        loop_id = client.post("/v1/loops", headers=auth, json=VALID_GOAL).json()["loop_id"]

        response = client.post(
            f"/v1/loops/{loop_id}/approve", headers=auth, json={"approved": True}
        )
        assert response.status_code == 200
        assert response.json()["state"] == "EXECUTING"
        # 创建时入队一次 + 审批时入队一次，两次都要带 project_id
        # （Worker 靠它设 RLS 变量，漏了就读不到 loop_runs）。
        # 期望值取自 loop 行本身，而非测试常量：入队声明必须与落库的
        # project 一致，否则 Worker 设错变量照样读不到行。
        project_id = client.get(f"/v1/loops/{loop_id}", headers=auth).json()["project_id"]
        assert enqueued == [(loop_id, project_id), (loop_id, project_id)]

    def test_reject_goes_to_rejected(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        loop_id = client.post("/v1/loops", headers=auth, json=VALID_GOAL).json()["loop_id"]
        response = client.post(
            f"/v1/loops/{loop_id}/approve", headers=auth, json={"approved": False}
        )
        assert response.status_code == 200
        assert response.json()["state"] == "REJECTED"
        assert response.json()["final_state"] == "REJECTED"


class TestIterations:
    def test_empty_iterations(
        self, client: TestClient, auth: dict[str, str], monkeypatch: Any
    ) -> None:
        _disable_enqueue(monkeypatch)
        loop_id = client.post("/v1/loops", headers=auth, json=VALID_GOAL).json()["loop_id"]
        response = client.get(f"/v1/loops/{loop_id}/iterations", headers=auth)
        assert response.status_code == 200
        assert response.json()["iterations"] == []
