"""graph_module 节点类型测试 —— 7 种节点的参数模型、执行器、注册表。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ariadne.graph_module import NodeFactory, available_node_kinds
from ariadne.graph_module.executor import (
    ROUTE_KEY,
    GraphExecutor,
    NodeExecutionContext,
)
from ariadne.graph_module.models import (
    BRANCH_INPUTS,
    CODE_INPUTS,
    CODE_OUTPUTS,
    EVAL_INPUTS,
    EVAL_OUTPUTS,
    LLM_INPUTS,
    LLM_OUTPUTS,
    LOOP_INPUTS,
    LOOP_OUTPUTS,
    RAG_INPUTS,
    RAG_OUTPUTS,
    TOOL_INPUTS,
    TOOL_OUTPUTS,
    Edge,
    NodeBase,
    NodeKind,
    WorkflowGraph,
)
from ariadne.graph_module.nodes.branch import BranchNodeExecutor
from ariadne.graph_module.nodes.code import CodeNodeExecutor, CodeRunner
from ariadne.graph_module.nodes.eval import EvalNodeExecutor
from ariadne.graph_module.nodes.llm import LLMNodeExecutor
from ariadne.graph_module.nodes.loop import LoopNodeExecutor
from ariadne.graph_module.nodes.rag import RAGNodeExecutor, Retriever
from ariadne.graph_module.nodes.tool import ToolNodeExecutor
from ariadne.loop_module.engine import LLMResponse
from ariadne.loop_module.verifier.command import ExecResult

# ---------- 测试用桩 ----------


class ScriptedLLM:
    """按脚本输出 LLM 响应。"""

    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.calls = 0
        self.models: list[str] = []

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        idx = self.calls
        self.calls += 1
        self.models.append(model)
        output = self.outputs[idx] if idx < len(self.outputs) else self.outputs[-1]
        return LLMResponse(
            output=output,
            input_tokens=10,
            output_tokens=20,
        )


class StubRetriever(Retriever):
    """返回固定文档列表的桩。"""

    def __init__(self, docs: list[str]):
        self._docs = docs
        self.queries: list[str] = []

    async def retrieve(self, query: str, *, top_k: int, index: str) -> list[str]:
        self.queries.append(query)
        return self._docs[:top_k]


class StubRunner(CodeRunner):
    """返回固定 ExecResult 的桩。"""

    def __init__(self, stdout: str = "ok", exit_code: int = 0):
        self._stdout = stdout
        self._exit_code = exit_code
        self.cmds: list[str] = []

    def run(self, cmd: str, *, workdir: Path) -> ExecResult:
        self.cmds.append(cmd)
        return ExecResult(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr="",
            duration_ms=1,
        )


# ---------- 辅助构造函数 ----------


def _llm_node(node_id: str, **params: object) -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.LLM,
        inputs=LLM_INPUTS,
        outputs=LLM_OUTPUTS,
        params={"prompt": "test", "model": "gpt-4", **params},
    )


def _edge(src: str, src_port: str, tgt: str, tgt_port: str) -> Edge:
    return Edge(source=src, source_port=src_port, target=tgt, target_port=tgt_port)


# ======================================================================
# 注册表
# ======================================================================


class TestNodeRegistry:
    """节点类型注册表。"""

    def test_all_eight_kinds_registered(self):
        """八种节点类型全部注册。"""
        kinds = set(available_node_kinds())
        assert kinds == {"llm", "tool", "rag", "code", "branch", "loop", "eval", "subgraph"}

    @pytest.mark.parametrize(
        "kind,expected_name",
        [
            ("llm", "LLMNodeParams"),
            ("tool", "ToolNodeParams"),
            ("rag", "RAGNodeParams"),
            ("code", "CodeNodeParams"),
            ("branch", "BranchNodeParams"),
            ("loop", "LoopNodeParams"),
            ("eval", "EvalNodeParams"),
            ("subgraph", "SubgraphNodeParams"),
        ],
    )
    def test_factory_returns_correct_class(self, kind: str, expected_name: str):
        """NodeFactory 返回正确的参数类。"""
        cls = NodeFactory(kind)
        assert cls.__name__ == expected_name

    def test_factory_raises_on_unknown(self):
        """未注册的 kind 抛 ValueError。"""
        with pytest.raises(ValueError, match="未注册"):
            NodeFactory("nonexistent")


# ======================================================================
# LLM 节点
# ======================================================================


class TestLLMNode:
    """LLM 节点执行器。"""

    @pytest.mark.asyncio
    async def test_llm_executes_and_returns_text(self):
        """LLM 节点调用 LLMClient 返回 text 端口。"""
        llm = ScriptedLLM(["hello world"])
        executor = LLMNodeExecutor(llm)
        node = _llm_node("a")
        ctx = NodeExecutionContext(node=node, inputs={"prompt": "hi"})
        result = await executor.execute(ctx)
        assert result["text"] == "hello world"
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_llm_uses_upstream_prompt(self):
        """上游 prompt 输入覆盖 params 中的 prompt。"""
        llm = ScriptedLLM(["upstream response"])
        executor = LLMNodeExecutor(llm)
        node = _llm_node("a", prompt="default")
        ctx = NodeExecutionContext(node=node, inputs={"prompt": "from upstream"})
        result = await executor.execute(ctx)
        assert result["text"] == "upstream response"

    @pytest.mark.asyncio
    async def test_llm_uses_param_prompt_when_no_upstream(self):
        """无上游输入时用 params 中的 prompt。"""
        llm = ScriptedLLM(["param response"])
        executor = LLMNodeExecutor(llm)
        node = _llm_node("a", prompt="from param")
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result["text"] == "param response"

    @pytest.mark.asyncio
    async def test_llm_uses_project_default_model_when_param_is_empty(self):
        """项目模型配置作为默认值，节点显式 model 仍可覆盖。"""
        llm = ScriptedLLM(["default model response"])
        executor = LLMNodeExecutor(llm, default_model="project-model")
        node = _llm_node("a", prompt="from param", model="")
        ctx = NodeExecutionContext(node=node, inputs={})
        await executor.execute(ctx)
        assert llm.models == ["project-model"]

    @pytest.mark.asyncio
    async def test_llm_node_in_graph(self):
        """LLM 节点在完整图中执行。"""
        a = _llm_node("a")
        b = _llm_node("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "prompt"),),
        )
        llm = ScriptedLLM(["first", "second"])
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: LLMNodeExecutor(llm)},
        )
        assert result.errors == []
        assert result.outputs["b"]["text"] == "second"
        assert llm.calls == 2


# ======================================================================
# Tool 节点
# ======================================================================


class TestToolNode:
    """Tool 节点执行器。"""

    @pytest.mark.asyncio
    async def test_tool_executes_and_returns_result(self):
        """Tool 节点调用 tool_executor 返回 result 端口。"""
        async def tool_exec(cmd: str, args: dict[str, Any]) -> str:
            return f"{cmd}:{args}"

        executor = ToolNodeExecutor(tool_exec)
        node = NodeBase(
            id="t",
            kind=NodeKind.TOOL,
            inputs=TOOL_INPUTS,
            outputs=TOOL_OUTPUTS,
            params={"cmd": "echo", "args": {"msg": "hi"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "echo" in result["result"]
        assert "hi" in result["result"]

    @pytest.mark.asyncio
    async def test_tool_merges_upstream_args(self):
        """上游 args 输入与 params args 合并。"""
        async def tool_exec(cmd: str, args: dict[str, Any]) -> str:
            return f"{cmd}:{sorted(args.keys())}"

        executor = ToolNodeExecutor(tool_exec)
        node = NodeBase(
            id="t",
            kind=NodeKind.TOOL,
            inputs=TOOL_INPUTS,
            outputs=TOOL_OUTPUTS,
            params={"cmd": "run", "args": {"a": 1}},
        )
        ctx = NodeExecutionContext(node=node, inputs={"args": {"b": 2}})
        result = await executor.execute(ctx)
        assert "a" in result["result"]
        assert "b" in result["result"]

    @pytest.mark.asyncio
    async def test_tool_no_args_defaults_to_empty(self):
        """无 args 参数时用空 dict。"""
        async def tool_exec(cmd: str, args: dict[str, Any]) -> str:
            return f"{cmd}:{args}"

        executor = ToolNodeExecutor(tool_exec)
        node = NodeBase(
            id="t",
            kind=NodeKind.TOOL,
            inputs=TOOL_INPUTS,
            outputs=TOOL_OUTPUTS,
            params={"cmd": "list"},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "list" in result["result"]


# ======================================================================
# RAG 节点
# ======================================================================


class TestRAGNode:
    """RAG 节点执行器。"""

    @pytest.mark.asyncio
    async def test_rag_returns_documents(self):
        """RAG 节点返回 documents 端口。"""
        retriever = StubRetriever(["doc1", "doc2", "doc3"])
        executor = RAGNodeExecutor(retriever)
        node = NodeBase(
            id="r",
            kind=NodeKind.RAG,
            inputs=RAG_INPUTS,
            outputs=RAG_OUTPUTS,
            params={"query": "test", "top_k": 2, "index": "main"},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result["documents"] == ["doc1", "doc2"]
        assert retriever.queries == ["test"]

    @pytest.mark.asyncio
    async def test_rag_uses_upstream_query(self):
        """上游 query 输入覆盖 params query。"""
        retriever = StubRetriever(["doc1"])
        executor = RAGNodeExecutor(retriever)
        node = NodeBase(
            id="r",
            kind=NodeKind.RAG,
            inputs=RAG_INPUTS,
            outputs=RAG_OUTPUTS,
            params={"query": "default", "top_k": 5},
        )
        ctx = NodeExecutionContext(node=node, inputs={"query": "from upstream"})
        await executor.execute(ctx)
        assert retriever.queries == ["from upstream"]

    @pytest.mark.asyncio
    async def test_rag_top_k_limits_results(self):
        """top_k 限制返回文档数。"""
        retriever = StubRetriever(["d1", "d2", "d3", "d4", "d5"])
        executor = RAGNodeExecutor(retriever)
        node = NodeBase(
            id="r",
            kind=NodeKind.RAG,
            inputs=RAG_INPUTS,
            outputs=RAG_OUTPUTS,
            params={"query": "q", "top_k": 3},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert len(result["documents"]) == 3


# ======================================================================
# Code 节点
# ======================================================================


class TestCodeNode:
    """Code 节点执行器。"""

    @pytest.mark.asyncio
    async def test_code_returns_result(self):
        """Code 节点返回执行结果。"""
        runner = StubRunner(stdout="hello from code")
        executor = CodeNodeExecutor(runner)
        node = NodeBase(
            id="c",
            kind=NodeKind.CODE,
            inputs=CODE_INPUTS,
            outputs=CODE_OUTPUTS,
            params={"code": "print('hello')", "language": "python"},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "hello from code" in result["result"]

    @pytest.mark.asyncio
    async def test_code_injects_input_variable(self):
        """上游 input 被注入为 __input 变量。"""
        runner = StubRunner(stdout="got input")
        executor = CodeNodeExecutor(runner)
        node = NodeBase(
            id="c",
            kind=NodeKind.CODE,
            inputs=CODE_INPUTS,
            outputs=CODE_OUTPUTS,
            params={"code": "print(__input)"},
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": "test_value"})
        result = await executor.execute(ctx)
        assert "got input" in result["result"]

    @pytest.mark.asyncio
    async def test_code_node_in_graph(self):
        """Code 节点在图中执行。"""
        runner = StubRunner(stdout="executed")
        a = NodeBase(
            id="a",
            kind=NodeKind.CODE,
            inputs=CODE_INPUTS,
            outputs=CODE_OUTPUTS,
            params={"code": "print('a')"},
        )
        graph = WorkflowGraph(nodes=(a,))
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors={NodeKind.CODE: CodeNodeExecutor(runner)},
        )
        assert result.errors == []
        assert "executed" in result.outputs["a"]["result"]

    @pytest.mark.asyncio
    async def test_code_node_js_uses_node_command(self):
        """javascript 语言映射到 node 命令（而非白名单外的 run）。"""
        runner = StubRunner(stdout="js ok")
        executor = CodeNodeExecutor(runner)
        node = NodeBase(
            id="c",
            kind=NodeKind.CODE,
            inputs=CODE_INPUTS,
            outputs=CODE_OUTPUTS,
            params={"code": "console.log('hi')", "language": "javascript"},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "js ok" in result["result"]
        # 断言传给 runner 的命令以 node 开头，而不是 run
        assert runner.cmds and runner.cmds[-1].startswith("node ")

    @pytest.mark.asyncio
    async def test_code_node_unsupported_language_rejected(self):
        """不支持的语言显式报错（而非生成必失败的 run 命令）。"""
        runner = StubRunner(stdout="")
        executor = CodeNodeExecutor(runner)
        node = NodeBase(
            id="c",
            kind=NodeKind.CODE,
            inputs=CODE_INPUTS,
            outputs=CODE_OUTPUTS,
            params={"code": "print(1)", "language": "julia"},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        with pytest.raises(ValueError, match="不受支持"):
            await executor.execute(ctx)


# ======================================================================
# Branch 节点
# ======================================================================


class TestBranchNode:
    """Branch 节点执行器。"""

    @pytest.mark.asyncio
    async def test_branch_literal_condition(self):
        """condition 是字面分支名时直接路由。"""
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "path_a", "branches": {"path_a": "node_a", "path_b": "node_b"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result[ROUTE_KEY] == "path_a"

    @pytest.mark.asyncio
    async def test_branch_expression_condition(self):
        """condition 是表达式时求值后路由。"""
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "'high' if __input > 10 else 'low'",
                    "branches": {"high": "h", "low": "l"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": 15})
        result = await executor.execute(ctx)
        assert result[ROUTE_KEY] == "high"

    @pytest.mark.asyncio
    async def test_branch_expression_low(self):
        """表达式求值为 low 分支。"""
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "'high' if __input > 10 else 'low'",
                    "branches": {"high": "h", "low": "l"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": 5})
        result = await executor.execute(ctx)
        assert result[ROUTE_KEY] == "low"

    @pytest.mark.asyncio
    async def test_branch_invalid_condition_defaults(self):
        """无效表达式默认路由到 default。"""
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "invalid !!! syntax",
                    "branches": {"default": "d"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result[ROUTE_KEY] == "default"

    @pytest.mark.asyncio
    async def test_branch_unknown_result_defaults(self):
        """求值结果不在 branches 中时路由到 default。"""
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "'unknown_path'",
                    "branches": {"known": "k", "default": "d"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result[ROUTE_KEY] == "default"

    @pytest.mark.asyncio
    async def test_branch_condition_rejects_object_introspection(self):
        """恶意 condition 必须被 AST 白名单拦下，路由到 default 而非执行任意代码。

        安全审查确认：曾经的 eval(condition, {"__builtins__": {}}) 可经
        `().__class__.__mro__[1].__subclasses__()...` 内省链逃逸执行任意命令。
        AST 白名单从语法层拒绝 Attribute/Call/Subscript 非 Name 打头的链。
        """
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "().__class__.__mro__[1].__subclasses__()"
                                "[147].__init__.__globals__['system']('echo PWNED')",
                    "branches": {"default": "d", "pwned": "p"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": 1})
        result = await executor.execute(ctx)
        # 逃逸被拦：默认走 default 分支，且不会被路由到 pwned
        assert result[ROUTE_KEY] == "default"

    @pytest.mark.asyncio
    async def test_branch_condition_rejects_direct_dangerous_names(self):
        """直接引用 __import__/open/__class__ 等危险名的 condition 被拒。"""
        executor = BranchNodeExecutor()
        node = NodeBase(
            id="br",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=(),
            params={"condition": "__import__('os').system('id')",
                    "branches": {"default": "d"}},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result[ROUTE_KEY] == "default"


# ======================================================================
# Eval 节点
# ======================================================================


class TestEvalNode:
    """Eval 节点执行器。"""

    @pytest.mark.asyncio
    async def test_eval_regex_pass(self):
        """正则断言通过时 passed=True。"""
        executor = EvalNodeExecutor()
        node = NodeBase(
            id="e",
            kind=NodeKind.EVAL,
            inputs=EVAL_INPUTS,
            outputs=EVAL_OUTPUTS,
            params={
                "assertions": [
                    {
                        "id": "a1",
                        "kind": "regex",
                        "spec": {"pattern": "hello"},
                        "blocking": True,
                    }
                ],
            },
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": "hello world"})
        result = await executor.execute(ctx)
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_eval_regex_fail(self):
        """正则断言不通过时 passed=False。"""
        executor = EvalNodeExecutor()
        node = NodeBase(
            id="e",
            kind=NodeKind.EVAL,
            inputs=EVAL_INPUTS,
            outputs=EVAL_OUTPUTS,
            params={
                "assertions": [
                    {
                        "id": "a1",
                        "kind": "regex",
                        "spec": {"pattern": "goodbye"},
                        "blocking": True,
                    }
                ],
            },
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": "hello world"})
        result = await executor.execute(ctx)
        assert result["passed"] is False
        assert "a1" in result["verdict"]

    @pytest.mark.asyncio
    async def test_eval_no_assertions(self):
        """无断言时 passed=False。"""
        executor = EvalNodeExecutor()
        node = NodeBase(
            id="e",
            kind=NodeKind.EVAL,
            inputs=EVAL_INPUTS,
            outputs=EVAL_OUTPUTS,
            params={"assertions": []},
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": "test"})
        result = await executor.execute(ctx)
        assert result["passed"] is False
        assert "无断言" in result["verdict"]

    @pytest.mark.asyncio
    async def test_eval_multiple_assertions_all_pass(self):
        """多条断言全通过时 passed=True。"""
        executor = EvalNodeExecutor()
        node = NodeBase(
            id="e",
            kind=NodeKind.EVAL,
            inputs=EVAL_INPUTS,
            outputs=EVAL_OUTPUTS,
            params={
                "assertions": [
                    {"id": "a1", "kind": "regex", "spec": {"pattern": "hello"}, "blocking": True},
                    {"id": "a2", "kind": "regex", "spec": {"pattern": "world"}, "blocking": True},
                ],
            },
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": "hello world"})
        result = await executor.execute(ctx)
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_eval_one_blocking_fails(self):
        """一条 blocking 断言失败时 passed=False。"""
        executor = EvalNodeExecutor()
        node = NodeBase(
            id="e",
            kind=NodeKind.EVAL,
            inputs=EVAL_INPUTS,
            outputs=EVAL_OUTPUTS,
            params={
                "assertions": [
                    {"id": "a1", "kind": "regex", "spec": {"pattern": "hello"}, "blocking": True},
                    {"id": "a2", "kind": "regex", "spec": {"pattern": "missing"}, "blocking": True},
                ],
            },
        )
        ctx = NodeExecutionContext(node=node, inputs={"input": "hello world"})
        result = await executor.execute(ctx)
        assert result["passed"] is False
        assert "a2" in result["verdict"]


# ======================================================================
# Loop 节点
# ======================================================================


class TestLoopNode:
    """Loop 节点执行器。"""

    @pytest.mark.asyncio
    async def test_loop_executes_and_returns_output(self):
        """Loop 节点执行返回 output/iterations/converged。"""
        # 最小 Goal：regex 断言，LLM 输出匹配
        llm = ScriptedLLM(["hello"])
        executor = LoopNodeExecutor(llm)
        node = NodeBase(
            id="l",
            kind=NodeKind.LOOP,
            inputs=LOOP_INPUTS,
            outputs=LOOP_OUTPUTS,
            params={
                "goal": {
                    "task": "say hello",
                    "assertions": [
                        {
                            "id": "a1",
                            "kind": "regex",
                            "spec": {"pattern": "hello"},
                            "blocking": True,
                        }
                    ],
                    "budget": {"max_iterations": 3},
                }
            },
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "output" in result
        assert "iterations" in result
        assert "converged" in result
        assert isinstance(result["iterations"], int)
        assert isinstance(result["converged"], bool)

    @pytest.mark.asyncio
    async def test_loop_missing_goal_raises(self):
        """缺少 goal 参数抛 ValueError。"""
        llm = ScriptedLLM(["hello"])
        executor = LoopNodeExecutor(llm)
        node = NodeBase(
            id="l",
            kind=NodeKind.LOOP,
            inputs=LOOP_INPUTS,
            outputs=LOOP_OUTPUTS,
            params={},
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        with pytest.raises(ValueError, match="goal"):
            await executor.execute(ctx)

    @pytest.mark.asyncio
    async def test_loop_converges_when_assertion_passes(self):
        """LLM 输出满足断言时 converged=True。"""
        llm = ScriptedLLM(["hello world"])
        executor = LoopNodeExecutor(llm)
        node = NodeBase(
            id="l",
            kind=NodeKind.LOOP,
            inputs=LOOP_INPUTS,
            outputs=LOOP_OUTPUTS,
            params={
                "goal": {
                    "task": "say hello",
                    "assertions": [
                        {
                            "id": "a1",
                            "kind": "regex",
                            "spec": {"pattern": "hello"},
                            "blocking": True,
                        }
                    ],
                    "budget": {"max_iterations": 3},
                }
            },
        )
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result["converged"] is True


# ======================================================================
# 参数模型
# ======================================================================


class TestParamsModels:
    """参数模型基本行为。"""

    def test_llm_params_frozen(self):
        from dataclasses import FrozenInstanceError

        from ariadne.graph_module.nodes.llm import LLMNodeParams
        params = LLMNodeParams(prompt="hi", model="gpt-4")
        with pytest.raises(FrozenInstanceError):
            params.prompt = "x"  # type: ignore[misc]

    def test_tool_params_defaults(self):
        from ariadne.graph_module.nodes.tool import ToolNodeParams
        params = ToolNodeParams(cmd="echo")
        assert params.args is None

    def test_rag_params_defaults(self):
        from ariadne.graph_module.nodes.rag import RAGNodeParams
        params = RAGNodeParams(query="test")
        assert params.top_k == 5
        assert params.index == "default"

    def test_code_params_defaults(self):
        from ariadne.graph_module.nodes.code import CodeNodeParams
        params = CodeNodeParams(code="print(1)")
        assert params.language == "python"

    def test_branch_params(self):
        from ariadne.graph_module.nodes.branch import BranchNodeParams
        params = BranchNodeParams(
            condition="x > 0",
            branches={"yes": "y_node", "no": "n_node"},
        )
        assert params.condition == "x > 0"
        assert "yes" in params.branches

    def test_loop_params(self):
        from ariadne.graph_module.nodes.loop import LoopNodeParams
        params = LoopNodeParams(goal={"task": "x"})
        assert params.goal["task"] == "x"

    def test_eval_params(self):
        from ariadne.graph_module.nodes.eval import EvalNodeParams
        params = EvalNodeParams(assertions=[{"id": "a", "kind": "regex", "spec": {}}])
        assert len(params.assertions) == 1

    def test_subgraph_params(self):
        from ariadne.graph_module.nodes.subgraph import SubgraphNodeParams
        params = SubgraphNodeParams(graph={"version": "1", "nodes": [], "edges": []})
        assert params.graph["version"] == "1"
