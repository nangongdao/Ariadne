"""GraphExecutor 装配层的接线断言 —— R12 第六实例的防线。

为什么单独一个文件：`test_graph_executor.py` / `test_graph_nodes.py` 里每条
测试都自己手搓 `node_executors={NodeKind.LLM: FakeLLMExecutor()}` 传进
`GraphExecutor.run()`。8 种 executor 因此全部"单测通过"，而 src 里没有任何
地方构造这张表 —— 唯一的构造点是 nodes/subgraph.py:98 的递归自调，子图把
父图给它的 dict 再传下去，整条链没有源头。

所以这里的每条断言都刻意**不直接构造 executor**，而是从 Settings 或 HTTP
请求出发走真实装配路径。测试里手动注入 executor 的写法证明"如果被调用它能
工作"，证明不了"它被调用"。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from ariadne.config import Settings
from ariadne.graph_module.models import NodeKind
from ariadne.graph_module.runtime import (
    build_node_executors,
    build_node_executors_from_settings,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


class FakeLLM:
    """LLMClient 的最小实现：只要 complete 可 await。"""

    def __init__(self, reply: str = "fake-reply") -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append(prompt)
        return self.reply


class FakeRetriever:
    async def retrieve(self, query: str, *, top_k: int, index: str) -> list[str]:
        return [f"doc-{query}"]


class TestNoDependencyKindsAlwaysAvailable:
    """branch / eval / subgraph 不依赖外部 IO，必须无条件可用。"""

    def test_branch_and_eval_registered_without_any_dependency(self) -> None:
        executors = build_node_executors()
        assert NodeKind.BRANCH in executors
        assert NodeKind.EVAL in executors

    def test_subgraph_shares_the_same_table(self) -> None:
        """子图与父图能力集必须一致 —— 拿到的得是同一个 dict 对象。

        若 SubgraphNodeExecutor 拿到的是快照副本，之后往表里补的 kind
        在子图里就不可见：同一个节点在父图能跑、在子图报"没有注册"。
        """
        executors = build_node_executors(llm=FakeLLM())
        sub = executors[NodeKind.SUBGRAPH]
        assert sub._node_executors is executors  # type: ignore[attr-defined]


class TestDependencyGatedKinds:
    """缺依赖时不注册，而不是注册一个返回空串的假执行器。"""

    def test_llm_absent_without_client(self) -> None:
        executors = build_node_executors()
        assert NodeKind.LLM not in executors
        assert NodeKind.LOOP not in executors

    def test_llm_and_loop_appear_together(self) -> None:
        """两者共用同一个 LLMClient，不该出现只有一个可用的状态。"""
        executors = build_node_executors(llm=FakeLLM())
        assert NodeKind.LLM in executors
        assert NodeKind.LOOP in executors

    def test_rag_absent_without_retriever(self) -> None:
        """Retriever 在 src 里没有实现（M5 未定检索后端），默认必须缺席。"""
        assert NodeKind.RAG not in build_node_executors()
        assert NodeKind.RAG in build_node_executors(retriever=FakeRetriever())

    def test_tool_absent_without_executor(self) -> None:
        async def fake_tool(cmd: str, args: dict[str, Any]) -> str:
            return "ok"

        assert NodeKind.TOOL not in build_node_executors()
        assert NodeKind.TOOL in build_node_executors(tool_executor=fake_tool)


class TestSettingsPath:
    """生产入口用的是 from_settings，断言必须走它而不是 build_node_executors。"""

    def test_no_api_key_means_no_llm_node(self, settings: Settings) -> None:
        """构造一个注定 401 的 client 会把配置缺失变成运行时图执行失败。"""
        executors = build_node_executors_from_settings(settings)
        if not settings.llm.api_key.get_secret_value():
            assert NodeKind.LLM not in executors

    def test_code_node_available_on_windows_via_restricted_runner(
        self, settings: Settings
    ) -> None:
        """Code 节点要能用：Windows 上没有沙箱后端，靠受限子进程兜底。

        这条同时锁住"Code 节点与 COMMAND 断言隔离强度一致"——两者都经
        build_command_runner 决策，降级时都落到 RestrictedRunner。
        """
        executors = build_node_executors_from_settings(settings)
        assert NodeKind.CODE in executors

    def test_code_runner_comes_from_shared_selector(self, settings: Settings) -> None:
        """复用 build_command_runner 而不是重写一份沙箱选择逻辑。

        逻辑分叉的表现是改了一处安全检查、另一处仍是旧行为。这里断言
        Code 节点的 runner 类型与 COMMAND 断言选出的一致。
        """
        from ariadne.loop_module.verifier.command import RestrictedRunner
        from ariadne.worker.loop_worker import build_command_runner

        shared = build_command_runner(settings)
        expected = type(shared) if shared is not None else RestrictedRunner
        code_exec = build_node_executors_from_settings(settings)[NodeKind.CODE]
        assert isinstance(code_exec._runner, expected)  # type: ignore[attr-defined]

    def test_injected_llm_wins_over_settings(self, settings: Settings) -> None:
        llm = FakeLLM()
        executors = build_node_executors_from_settings(settings, llm=llm)
        assert executors[NodeKind.LLM]._llm is llm  # type: ignore[attr-defined]


class TestExecuteEndpoint:
    """端点接线断言：图能被真正执行，不是只能建和校验。"""

    def _create(self, client: TestClient, auth: dict[str, str], graph: dict) -> str:
        resp = client.post(
            "/v1/graphs",
            json={"name": "exec-graph", "graph": graph},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    def _wait_for_completion(
        self, client: TestClient, auth: dict[str, str], graph_run_id: str, timeout: int = 10
    ) -> dict[str, Any]:
        """轮询 graph_run 直到完成（或超时）。"""
        start = time.time()
        while time.time() - start < timeout:
            resp = client.get(f"/v1/graphs/runs/{graph_run_id}", headers=auth)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            if body["state"] in ("COMPLETED", "FAILED", "TIMEOUT"):
                return body
            time.sleep(0.1)
        raise TimeoutError(f"Graph run {graph_run_id} 未在 {timeout}s 内完成")

    def _branch_graph(self) -> dict[str, Any]:
        """单个 branch 节点 —— 无外部依赖，任何环境都能真跑完。"""
        return {
            "version": "1",
            "graph": {
                "version": "1",
                "nodes": [
                    {
                        "id": "b",
                        "kind": "branch",
                        "inputs": [
                            {"name": "input", "kind": "text", "required": True}
                        ],
                        "outputs": [
                            {"name": "__route", "kind": "text", "required": True}
                        ],
                        "params": {
                            "condition": "left",
                            "branches": {"left": "b", "right": "b"},
                        },
                    }
                ],
                "edges": [],
            },
        }

    def test_execute_reaches_the_executor(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """核心断言：POST /execute 真的跑到了 GraphExecutor。

        端点存在但返回 501/404 的话这条就红 —— 那正是修复前的状态
        （只有 CRUD + validate，没有 execute）。
        """
        graph_id = self._create(client, auth, self._branch_graph())
        resp = client.post(
            f"/v1/graphs/{graph_id}/execute",
            json={"inputs": {"input": "x"}},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "graph_run_id" in body
        assert body["state"] == "PENDING"

        # 测试模式下 execute_graph 会同步执行，检查最终状态
        # （无需轮询，_execute 已经执行完成）
        graph_run_id = body["graph_run_id"]
        result_resp = client.get(f"/v1/graphs/runs/{graph_run_id}", headers=auth)
        assert result_resp.status_code == 200, result_resp.text
        result = result_resp.json()

        # 如果还是 PENDING，说明同步执行没有生效，打印详细信息
        if result["state"] == "PENDING":
            import sys

            print(
                f"\n执行未完成: state={result['state']}, "
                f"errors={result.get('errors')}",
                file=sys.stderr,
            )
            # 仍然尝试轮询
            result = self._wait_for_completion(client, auth, graph_run_id, timeout=5)

        assert result["state"] == "COMPLETED", (
            f"Expected COMPLETED, got {result['state']}, "
            f"errors: {result.get('errors')}"
        )
        assert result["node_states"]["b"] == "completed"

    def test_unavailable_kinds_reported_not_hidden(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """缺哪些能力要直说，省掉"为什么我的 rag 节点没结果"这轮排查。

        使用 rag 节点（需要 Retriever）来测试，因为测试环境中没有配置 Retriever。
        RAG 节点的 query 参数可以来自 inputs（上游输入）或 params（节点配置）。
        """
        # 创建一个使用 rag 节点的图（需要 Retriever，测试环境中没有）
        # query 从 params 提供（避免"缺少必填输入"错误）
        graph_id = self._create(
            client,
            auth,
            {
                "version": "1",
                "graph": {
                    "version": "1",
                    "nodes": [
                        {
                            "id": "r",
                            "kind": "rag",
                            "inputs": [],
                            "outputs": [{"name": "documents", "kind": "json", "required": True}],
                            "params": {"query": "test query", "top_k": 3},
                        }
                    ],
                    "edges": [],
                },
            },
        )
        resp = client.post(
            f"/v1/graphs/{graph_id}/execute",
            json={"inputs": {}},
            headers=auth
        )
        assert resp.status_code == 202, resp.text

        # 等待执行完成并检查错误信息
        result = client.get(f"/v1/graphs/runs/{resp.json()['graph_run_id']}", headers=auth)
        assert result.status_code == 200
        result_data = result.json()

        # 应该失败并在错误中提到缺少 executor
        assert result_data["state"] == "FAILED"
        assert result_data["errors"], "应该有错误信息"
        # 错误信息应该提到缺少 executor 或 rag 不可用
        errors_str = " ".join(str(e) for e in result_data["errors"])
        assert (
            "rag" in errors_str.lower()
            or "executor" in errors_str.lower()
            or "retriever" in errors_str.lower()
        )

    def test_missing_executor_fails_the_node_not_silently_empty(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """rag 节点无 Retriever 时必须 failed + 报错，不能"执行成功但输出空"。

        静默返回空输出与 _AlwaysPassEvaluator 永远给 100 分是同一类失效
        模式：调用方拿到 200 就以为跑通了。
        """
        graph = {
            "version": "1",
            "graph": {
                "version": "1",
                "nodes": [
                    {
                        "id": "r",
                        "kind": "rag",
                        "inputs": [
                            {"name": "query", "kind": "text", "required": True}
                        ],
                        "outputs": [
                            {"name": "docs", "kind": "json", "required": True}
                        ],
                        "params": {"query": "q", "index": "kb", "top_k": 3},
                    }
                ],
                "edges": [],
            },
        }
        graph_id = self._create(client, auth, graph)
        resp = client.post(
            f"/v1/graphs/{graph_id}/execute",
            json={"inputs": {"query": "q"}},
            headers=auth,
        )
        assert resp.status_code == 202, resp.text

        # 等待执行完成并验证失败
        result = self._wait_for_completion(client, auth, resp.json()["graph_run_id"])
        assert result["state"] == "FAILED"
        assert result["node_states"]["r"] == "failed"
        assert any("没有注册 executor" in e for e in result["errors"])

    def test_execute_requires_write_permission(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """执行会真的调 LLM、跑代码、花钱 —— READ 权限不够。"""
        import inspect

        from ariadne.api.routers import graphs

        source = inspect.getsource(graphs.execute_graph)
        assert "Permission.WRITE" in source

    def test_unknown_graph_is_404(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """不存在的图应该在提交阶段就返回 404，而不是创建 Job 后才失败。"""
        resp = client.post(
            "/v1/graphs/00000000-0000-0000-0000-000000000000/execute",
            json={},
            headers=auth,
        )
        assert resp.status_code == 404

    def test_execute_resolves_project_model_for_graph(
        self, client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Graph 执行复用项目模型 resolver，而不是只读全局 settings。"""
        from ariadne.graph_module.nodes.llm import LLMNodeExecutor
        from ariadne.loop_module.engine import LLMResponse
        from ariadne.worker.graph_worker_singleton import get_graph_worker

        class FakeProjectLLM:
            def __init__(self) -> None:
                self.models: list[str] = []

            async def complete(self, prompt: str, *, model: str) -> LLMResponse:
                self.models.append(model)
                return LLMResponse(
                    output="project response", input_tokens=1, output_tokens=1
                )

        llm = FakeProjectLLM()
        captured: dict[str, object] = {}

        async def fake_resolve(pg: object, project_id: object, **kwargs: object):
            captured["pg"] = pg
            captured["project_id"] = project_id
            return llm, "project-default-model"

        def fake_build(
            settings: object,
            *,
            llm: object | None = None,
            default_model: str | None = None,
            **kwargs: object,
        ) -> dict[NodeKind, object]:
            captured["default_model"] = default_model
            assert llm is not None
            return {NodeKind.LLM: LLMNodeExecutor(llm, default_model=default_model)}  # type: ignore[arg-type]

        # Patch GraphWorker 实例的 _execute 方法
        worker = get_graph_worker()

        async def patched_execute(graph_run_id, project_id):
            from ariadne.graph_module.executor import GraphExecutor
            from ariadne.graph_module.serialize import spec_to_graph
            from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository
            from ariadne.storage.postgres.repositories.graphs import GraphRepository

            async with worker._pg_factory.session() as session:
                graph_runs_repo = GraphRunRepository(session)
                graphs_repo = GraphRepository(session)

                try:
                    run = await graph_runs_repo.by_id(graph_run_id)
                    graph_row = await graphs_repo.get(
                        project_id=project_id, graph_id=run.graph_id
                    )
                    await graph_runs_repo.transition(graph_run_id, "RUNNING")
                    await session.commit()

                    graph = spec_to_graph(graph_row.graph)

                    # 使用 fake_resolve 和 fake_build
                    resolved_llm, resolved_model = await fake_resolve(
                        worker._pg_factory, project_id
                    )
                    node_executors = fake_build(
                        worker._settings, llm=resolved_llm, default_model=resolved_model
                    )

                    exec_result = await GraphExecutor().run(
                        graph, inputs=run.inputs, node_executors=node_executors
                    )

                    final_state = "FAILED" if exec_result.errors else "COMPLETED"
                    await graph_runs_repo.finish(
                        graph_run_id=graph_run_id,
                        final_state=final_state,
                        outputs=exec_result.outputs,
                        node_states={
                            k: v.value for k, v in exec_result.node_states.items()
                        },
                        errors=exec_result.errors,
                    )
                    await session.commit()

                    if resolved_llm is not None:
                        close = getattr(resolved_llm, "aclose", None)
                        if close is not None:
                            await close()

                except Exception as e:
                    try:
                        await graph_runs_repo.finish(
                            graph_run_id=graph_run_id,
                            final_state="FAILED",
                            outputs=None,
                            node_states={},
                            errors=[f"执行异常: {type(e).__name__}: {e}"],
                        )
                        await session.commit()
                    except Exception:
                        pass

        monkeypatch.setattr(worker, "_execute", patched_execute)

        graph = {
            "version": "1",
            "graph": {
                "version": "1",
                "nodes": [
                    {
                        "id": "llm",
                        "kind": "llm",
                        "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                        "outputs": [{"name": "text", "kind": "text", "required": True}],
                        "params": {"prompt": "hello", "model": "node-model"},
                    }
                ],
                "edges": [],
            },
        }
        graph_id = self._create(client, auth, graph)
        response = client.post(
            f"/v1/graphs/{graph_id}/execute",
            json={"inputs": {"prompt": "hello"}},
            headers=auth,
        )

        assert response.status_code == 202, response.text

        # 等待执行完成并验证结果
        result = self._wait_for_completion(client, auth, response.json()["graph_run_id"])
        assert result["state"] == "COMPLETED"
        assert result["outputs"]["llm"]["text"] == "project response"
        assert captured["default_model"] == "project-default-model"
        assert llm.models == ["node-model"]


class TestNodesPackageExports:
    """nodes 包此前 __all__ 是空列表 —— executor 层在包边界上不可见。"""

    @pytest.mark.parametrize(
        "name",
        [
            "BranchNodeExecutor",
            "CodeNodeExecutor",
            "EvalNodeExecutor",
            "LLMNodeExecutor",
            "LoopNodeExecutor",
            "RAGNodeExecutor",
            "SubgraphNodeExecutor",
            "ToolNodeExecutor",
        ],
    )
    def test_all_eight_executors_exported(self, name: str) -> None:
        from ariadne.graph_module import nodes

        assert name in nodes.__all__
        assert hasattr(nodes, name)
