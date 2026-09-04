"""Graph API 路由测试 —— CRUD + validate。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

TEST_KEY = "ak_test_key"


# ---------- 辅助函数 ----------


def _make_graph_dict(node_id: str = "a") -> dict[str, object]:
    """构造一个合法的 LLM 单节点图 dict。"""
    return {
        "version": "1",
        "graph": {
            "version": "1",
            "nodes": [
                {
                    "id": node_id,
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "hello", "model": "gpt-4"},
                }
            ],
            "edges": [],
        },
    }


def _make_two_node_graph() -> dict[str, object]:
    """构造一个 A→B 线性图。"""
    return {
        "version": "1",
        "graph": {
            "version": "1",
            "nodes": [
                {
                    "id": "a",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "hello", "model": "gpt-4"},
                },
                {
                    "id": "b",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "world", "model": "gpt-4"},
                },
            ],
            "edges": [
                {
                    "source": "a",
                    "source_port": "text",
                    "target": "b",
                    "target_port": "prompt",
                }
            ],
        },
    }


def _make_cycle_graph() -> dict[str, object]:
    """构造一个有环的图（A→B→A）。"""
    return {
        "version": "1",
        "graph": {
            "version": "1",
            "nodes": [
                {
                    "id": "a",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "x", "model": "m"},
                },
                {
                    "id": "b",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "x", "model": "m"},
                },
            ],
            "edges": [
                {"source": "a", "source_port": "text", "target": "b", "target_port": "prompt"},
                {"source": "b", "source_port": "text", "target": "a", "target_port": "prompt"},
            ],
        },
    }


def _make_missing_params_graph() -> dict[str, object]:
    """构造一个缺必填参数的图（LLM 无 model）。"""
    return {
        "version": "1",
        "graph": {
            "version": "1",
            "nodes": [
                {
                    "id": "a",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "hello"},
                }
            ],
            "edges": [],
        },
    }


# ======================================================================
# CRUD
# ======================================================================


class TestGraphsCRUD:
    """图的增删改查。"""

    def test_create_graph(self, client: TestClient, auth: dict[str, str]) -> None:
        """创建图返回 201。"""
        response = client.post(
            "/v1/graphs",
            json={"name": "test-graph", "graph": _make_graph_dict()},
            headers=auth,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "test-graph"
        assert body["version"] == 1
        assert body["is_active"] is True
        assert "graph" in body

    def test_list_graphs_empty(self, client: TestClient, auth: dict[str, str]) -> None:
        """无图时返回空列表。"""
        response = client.get("/v1/graphs", headers=auth)
        assert response.status_code == 200
        assert response.json() == []

    def test_create_and_list(self, client: TestClient, auth: dict[str, str]) -> None:
        """创建后能列出。"""
        client.post(
            "/v1/graphs",
            json={"name": "g1", "graph": _make_graph_dict()},
            headers=auth,
        )
        client.post(
            "/v1/graphs",
            json={"name": "g2", "graph": _make_graph_dict("b")},
            headers=auth,
        )
        response = client.get("/v1/graphs", headers=auth)
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_get_graph_by_id(self, client: TestClient, auth: dict[str, str]) -> None:
        """按 id 读取单个图。"""
        create_resp = client.post(
            "/v1/graphs",
            json={"name": "g1", "graph": _make_graph_dict()},
            headers=auth,
        )
        graph_id = create_resp.json()["id"]

        response = client.get(f"/v1/graphs/{graph_id}", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == graph_id
        assert body["name"] == "g1"

    def test_get_graph_not_found(self, client: TestClient, auth: dict[str, str]) -> None:
        """不存在的 id 返回 404。"""
        response = client.get(
            "/v1/graphs/00000000-0000-0000-0000-000000000000", headers=auth
        )
        assert response.status_code == 404

    def test_update_graph_creates_new_version(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """更新图创建新版本，旧版本失活。"""
        create_resp = client.post(
            "/v1/graphs",
            json={"name": "g1", "graph": _make_graph_dict()},
            headers=auth,
        )
        graph_id = create_resp.json()["id"]

        update_resp = client.put(
            f"/v1/graphs/{graph_id}",
            json={"name": "g1", "graph": _make_two_node_graph()},
            headers=auth,
        )
        assert update_resp.status_code == 200
        body = update_resp.json()
        assert body["version"] == 2
        assert body["is_active"] is True
        # 新 id（不同版本不同行）
        assert body["id"] != graph_id

    def test_update_not_found(self, client: TestClient, auth: dict[str, str]) -> None:
        """更新不存在的图返回 404。"""
        response = client.put(
            "/v1/graphs/00000000-0000-0000-0000-000000000000",
            json={"name": "g1", "graph": _make_graph_dict()},
            headers=auth,
        )
        assert response.status_code == 404

    def test_delete_graph(self, client: TestClient, auth: dict[str, str]) -> None:
        """删除图（软删除 is_active=False）。"""
        create_resp = client.post(
            "/v1/graphs",
            json={"name": "g1", "graph": _make_graph_dict()},
            headers=auth,
        )
        graph_id = create_resp.json()["id"]

        del_resp = client.delete(f"/v1/graphs/{graph_id}", headers=auth)
        assert del_resp.status_code == 204

        # 列表中不再出现
        list_resp = client.get("/v1/graphs", headers=auth)
        assert list_resp.json() == []

    def test_delete_not_found(self, client: TestClient, auth: dict[str, str]) -> None:
        """删除不存在的图返回 404。"""
        response = client.delete(
            "/v1/graphs/00000000-0000-0000-0000-000000000000", headers=auth
        )
        assert response.status_code == 404

    def test_create_duplicate_name_conflict(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """同项目同名图返回 409。"""
        client.post(
            "/v1/graphs",
            json={"name": "dup", "graph": _make_graph_dict()},
            headers=auth,
        )
        response = client.post(
            "/v1/graphs",
            json={"name": "dup", "graph": _make_graph_dict()},
            headers=auth,
        )
        assert response.status_code == 409

    def test_update_rename_onto_existing_name_conflict(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """改名撞另一个活跃图返回 409，且不留下两行同名。"""
        client.post(
            "/v1/graphs",
            json={"name": "taken", "graph": _make_graph_dict()},
            headers=auth,
        )
        other_id = client.post(
            "/v1/graphs",
            json={"name": "mine", "graph": _make_graph_dict()},
            headers=auth,
        ).json()["id"]

        response = client.put(
            f"/v1/graphs/{other_id}",
            json={"name": "taken", "graph": _make_two_node_graph()},
            headers=auth,
        )
        assert response.status_code == 409

        # 冲突必须整个回滚：旧行不能已经失活，否则原图从列表里消失了
        names = sorted(g["name"] for g in client.get("/v1/graphs", headers=auth).json())
        assert names == ["mine", "taken"]

    def test_update_keeping_own_name_is_not_conflict(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """名字不变的更新是正常版本递增，不能被唯一性检查拦成 409。"""
        graph_id = client.post(
            "/v1/graphs",
            json={"name": "same", "graph": _make_graph_dict()},
            headers=auth,
        ).json()["id"]

        response = client.put(
            f"/v1/graphs/{graph_id}",
            json={"name": "same", "graph": _make_two_node_graph()},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2


# ======================================================================
# 校验
# ======================================================================


class TestGraphsValidate:
    """图校验端点。"""

    def test_validate_valid_graph(self, client: TestClient, auth: dict[str, str]) -> None:
        """合法图校验通过。"""
        response = client.post(
            "/v1/graphs/validate",
            json={"graph": _make_graph_dict()},
            headers=auth,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["errors"] == []

    def test_validate_cycle_graph(self, client: TestClient, auth: dict[str, str]) -> None:
        """有环的图校验失败。"""
        response = client.post(
            "/v1/graphs/validate",
            json={"graph": _make_cycle_graph()},
            headers=auth,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert len(body["errors"]) > 0
        assert "环" in body["errors"][0]["message"]

    def test_validate_missing_params(self, client: TestClient, auth: dict[str, str]) -> None:
        """缺必填参数的图校验失败。"""
        response = client.post(
            "/v1/graphs/validate",
            json={"graph": _make_missing_params_graph()},
            headers=auth,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert any("model" in e["message"] for e in body["errors"])

    def test_create_invalid_graph_returns_422(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """创建有环的图返回 422。"""
        response = client.post(
            "/v1/graphs",
            json={"name": "bad", "graph": _make_cycle_graph()},
            headers=auth,
        )
        assert response.status_code == 422

    def test_create_missing_params_returns_422(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """创建缺参数的图返回 422。"""
        response = client.post(
            "/v1/graphs",
            json={"name": "bad", "graph": _make_missing_params_graph()},
            headers=auth,
        )
        assert response.status_code == 422


# ======================================================================
# 鉴权
# ======================================================================


class TestGraphsAuth:
    """鉴权测试。"""

    def test_auth_required(self, client: TestClient) -> None:
        """无 API key 返回 401。"""
        response = client.get("/v1/graphs")
        assert response.status_code == 401

    def test_invalid_key_rejected(self, client: TestClient) -> None:
        """错误 API key 返回 401。"""
        response = client.get(
            "/v1/graphs", headers={"X-Ariadne-Key": "wrong"}
        )
        assert response.status_code == 401
