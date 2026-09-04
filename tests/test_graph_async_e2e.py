"""Graph 异步执行端到端测试。

验证完整的异步 Job 流程：创建 → 提交 → 轮询状态 → 完成。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


class TestGraphAsyncE2E:
    """Graph 异步执行端到端测试。"""

    def test_graph_executes_to_completion(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """完整流程：创建图 → 提交执行 → 轮询状态 → COMPLETED + outputs。"""
        # 1. 创建一个简单的 branch 图（无外部依赖）
        graph_resp = client.post(
            "/v1/graphs",
            json={
                "name": "e2e-branch-graph",
                "graph": {
                    "version": "1",
                    "graph": {
                        "version": "1",
                        "nodes": [
                            {
                                "id": "branch1",
                                "kind": "branch",
                                "inputs": [
                                    {"name": "input", "kind": "text", "required": True}
                                ],
                                "outputs": [
                                    {"name": "__route", "kind": "text", "required": True}
                                ],
                                "params": {
                                    "condition": "left",
                                    "branches": {"left": "branch1", "right": "branch1"},
                                },
                            }
                        ],
                        "edges": [],
                    },
                },
            },
            headers=auth,
        )
        assert graph_resp.status_code == 201, graph_resp.text
        graph_id = graph_resp.json()["id"]

        # 2. 提交执行
        execute_resp = client.post(
            f"/v1/graphs/{graph_id}/execute",
            json={"inputs": {"input": "test input"}},
            headers=auth,
        )
        assert execute_resp.status_code == 202, execute_resp.text
        body = execute_resp.json()
        assert "graph_run_id" in body
        assert body["state"] == "PENDING"
        assert body["status_url"] == f"/v1/graphs/runs/{body['graph_run_id']}"

        graph_run_id = body["graph_run_id"]

        # 3. 轮询状态直到完成（测试模式下应该同步执行，立即完成）
        max_attempts = 50
        for i in range(max_attempts):
            status_resp = client.get(
                f"/v1/graphs/runs/{graph_run_id}", headers=auth
            )
            assert status_resp.status_code == 200, status_resp.text
            status = status_resp.json()

            if status["state"] in ("COMPLETED", "FAILED", "TIMEOUT"):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"Graph run 未在 {max_attempts * 0.1}s 内完成")

        # 4. 验证最终状态
        assert status["state"] == "COMPLETED", f"期望 COMPLETED，实际 {status['state']}, errors: {status.get('errors')}"
        assert status["outputs"]["branch1"]["__route"] == "left"
        assert status["node_states"]["branch1"] == "completed"
        assert status["errors"] == []
        assert status["graph_id"] == graph_id
        assert status["inputs"] == {"input": "test input"}
        assert status["created_at"] is not None
        assert status["finished_at"] is not None

    def test_graph_execution_failure_captured(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """执行失败时 errors 被正确记录（使用缺少 executor 的节点）。"""
        # 创建一个会失败的图（rag 节点需要 Retriever，测试环境没有）
        graph_resp = client.post(
            "/v1/graphs",
            json={
                "name": "e2e-fail-graph",
                "graph": {
                    "version": "1",
                    "graph": {
                        "version": "1",
                        "nodes": [
                            {
                                "id": "rag1",
                                "kind": "rag",
                                "inputs": [],
                                "outputs": [
                                    {"name": "documents", "kind": "json", "required": True}
                                ],
                                "params": {"query": "test", "top_k": 3},
                            }
                        ],
                        "edges": [],
                    },
                },
            },
            headers=auth,
        )
        assert graph_resp.status_code == 201
        graph_id = graph_resp.json()["id"]

        # 提交执行
        execute_resp = client.post(
            f"/v1/graphs/{graph_id}/execute",
            json={"inputs": {}},
            headers=auth,
        )
        assert execute_resp.status_code == 202
        graph_run_id = execute_resp.json()["graph_run_id"]

        # 轮询直到完成
        max_attempts = 50
        for i in range(max_attempts):
            status_resp = client.get(
                f"/v1/graphs/runs/{graph_run_id}", headers=auth
            )
            status = status_resp.json()
            if status["state"] in ("COMPLETED", "FAILED", "TIMEOUT"):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Graph run 未在 5s 内完成")

        # 验证失败状态
        assert status["state"] == "FAILED"
        assert len(status["errors"]) > 0
        assert status["node_states"]["rag1"] == "failed"
        assert status["outputs"] is None or status["outputs"] == {}

    def test_list_graph_runs(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """列出项目的 graph runs。"""
        # 创建并执行一个图
        graph_resp = client.post(
            "/v1/graphs",
            json={
                "name": "e2e-list-graph",
                "graph": {
                    "version": "1",
                    "graph": {
                        "version": "1",
                        "nodes": [
                            {
                                "id": "branch1",
                                "kind": "branch",
                                "inputs": [
                                    {"name": "input", "kind": "text", "required": True}
                                ],
                                "outputs": [
                                    {"name": "__route", "kind": "text", "required": True}
                                ],
                                "params": {
                                    "condition": "left",
                                    "branches": {"left": "branch1", "right": "branch1"},
                                },
                            }
                        ],
                        "edges": [],
                    },
                },
            },
            headers=auth,
        )
        graph_id = graph_resp.json()["id"]

        # 执行两次
        run_ids = []
        for i in range(2):
            execute_resp = client.post(
                f"/v1/graphs/{graph_id}/execute",
                json={"inputs": {"input": f"test-{i}"}},
                headers=auth,
            )
            run_ids.append(execute_resp.json()["graph_run_id"])

        # 等待执行完成
        time.sleep(0.5)

        # 列出 runs
        list_resp = client.get("/v1/graphs/runs", headers=auth)
        assert list_resp.status_code == 200, list_resp.text
        body = list_resp.json()

        assert "runs" in body
        assert "total" in body
        assert len(body["runs"]) >= 2  # 至少包含刚才创建的两个

        # 验证返回的 runs 包含必要字段
        for run in body["runs"]:
            assert "id" in run
            assert "graph_id" in run
            assert "state" in run
            assert "inputs" in run
            assert "node_states" in run
            assert "errors" in run
            assert "created_at" in run

    def test_concurrent_executions(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """同一个图可以并发执行多次。"""
        # 创建图
        graph_resp = client.post(
            "/v1/graphs",
            json={
                "name": "e2e-concurrent-graph",
                "graph": {
                    "version": "1",
                    "graph": {
                        "version": "1",
                        "nodes": [
                            {
                                "id": "branch1",
                                "kind": "branch",
                                "inputs": [
                                    {"name": "input", "kind": "text", "required": True}
                                ],
                                "outputs": [
                                    {"name": "__route", "kind": "text", "required": True}
                                ],
                                "params": {
                                    "condition": "left",
                                    "branches": {"left": "branch1", "right": "branch1"},
                                },
                            }
                        ],
                        "edges": [],
                    },
                },
            },
            headers=auth,
        )
        graph_id = graph_resp.json()["id"]

        # 并发提交 3 次执行
        run_ids = []
        for i in range(3):
            execute_resp = client.post(
                f"/v1/graphs/{graph_id}/execute",
                json={"inputs": {"input": f"input-{i}"}},
                headers=auth,
            )
            assert execute_resp.status_code == 202
            run_ids.append(execute_resp.json()["graph_run_id"])

        # 等待所有执行完成
        time.sleep(1.0)

        # 验证所有执行都完成
        for i, run_id in enumerate(run_ids):
            status_resp = client.get(f"/v1/graphs/runs/{run_id}", headers=auth)
            status = status_resp.json()
            assert status["state"] == "COMPLETED", f"Run {i} failed: {status['errors']}"
            assert status["outputs"]["branch1"]["__route"] == "left"


__all__ = ["TestGraphAsyncE2E"]
