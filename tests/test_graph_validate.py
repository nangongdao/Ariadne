"""graph_module 校验测试 —— 环检测、类型兼容、必填参数、断言可验证性。"""

from __future__ import annotations

from ariadne.graph_module.models import (
    BRANCH_INPUTS,
    BRANCH_OUTPUTS,
    LLM_INPUTS,
    LLM_OUTPUTS,
    LOOP_INPUTS,
    LOOP_OUTPUTS,
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
)
from ariadne.graph_module.validate import validate_graph

# ---------- 辅助函数 ----------


def _llm(node_id: str, prompt: str = "hello", model: str = "gpt-4") -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.LLM,
        inputs=LLM_INPUTS,
        outputs=LLM_OUTPUTS,
        params={"prompt": prompt, "model": model},
    )


def _eval(node_id: str) -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.EVAL,
        inputs=(Port(name="input", kind=PortKind.TEXT),),
        outputs=(
            Port(name="passed", kind=PortKind.JSON),
            Port(name="verdict", kind=PortKind.TEXT),
        ),
        params={
            "assertions": [
                {
                    "id": "has-func",
                    "kind": "regex",
                    "spec": {"pattern": r"def\s+\w+\s*\("},
                    "blocking": True,
                }
            ]
        },
    )


def _loop(node_id: str, goal_data: dict | None = None) -> NodeBase:
    if goal_data is None:
        goal_data = {
            "task": "写函数",
            "mode": "quality",
            "assertions": [
                {
                    "id": "test-pass",
                    "kind": "regex",
                    "spec": {"pattern": r"def\s+\w+\s*\("},
                    "blocking": True,
                }
            ],
            "budget": {"max_iterations": 3},
        }
    return NodeBase(
        id=node_id,
        kind=NodeKind.LOOP,
        inputs=LOOP_INPUTS,
        outputs=LOOP_OUTPUTS,
        params={"goal": goal_data},
    )


def _branch(
    node_id: str,
    condition: str = "true",
    branches: dict[str, str] | None = None,
) -> NodeBase:
    if branches is None:
        branches = {"yes": "target1", "no": "target2"}
    return NodeBase(
        id=node_id,
        kind=NodeKind.BRANCH,
        inputs=BRANCH_INPUTS,
        outputs=BRANCH_OUTPUTS,
        params={"condition": condition, "branches": branches},
    )


# ---------- 空图 ----------


class TestValidateEmpty:
    def test_empty_graph_ok(self) -> None:
        report = validate_graph(WorkflowGraph())
        assert report.ok
        assert len(report.errors) == 0


# ---------- 节点 id 唯一性 ----------


class TestValidateNodeIds:
    def test_duplicate_ids_rejected(self) -> None:
        a = _llm("dup")
        b = _llm("dup")
        g = WorkflowGraph(nodes=(a, b))
        report = validate_graph(g)
        assert not report.ok
        assert any("重复" in e.message for e in report.errors)

    def test_unique_ids_ok(self) -> None:
        a = _llm("a")
        b = _llm("b")
        g = WorkflowGraph(nodes=(a, b))
        report = validate_graph(g)
        assert report.ok


# ---------- 悬空边 ----------


class TestValidateEdges:
    def test_dangling_source_rejected(self) -> None:
        a = _llm("a")
        e = Edge(source="nonexistent", source_port="text", target="a", target_port="prompt")
        g = WorkflowGraph(nodes=(a,), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert any("不存在的源节点" in err.message for err in report.errors)

    def test_dangling_target_rejected(self) -> None:
        a = _llm("a")
        e = Edge(source="a", source_port="text", target="nonexistent", target_port="prompt")
        g = WorkflowGraph(nodes=(a,), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert any("不存在的目标节点" in err.message for err in report.errors)

    def test_nonexistent_source_port_rejected(self) -> None:
        a = _llm("a")
        b = _eval("b")
        e = Edge(source="a", source_port="nonexistent", target="b", target_port="input")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert any("没有输出端口" in err.message for err in report.errors)

    def test_nonexistent_target_port_rejected(self) -> None:
        a = _llm("a")
        b = _eval("b")
        e = Edge(source="a", source_port="text", target="b", target_port="nonexistent")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert any("没有输入端口" in err.message for err in report.errors)

    def test_valid_edge_ok(self) -> None:
        a = _llm("a")
        b = _eval("b")
        e = Edge(source="a", source_port="text", target="b", target_port="input")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert report.ok


# ---------- 环检测 ----------


class TestValidateCycle:
    def test_linear_no_cycle_ok(self) -> None:
        a = _llm("a")
        b = _eval("b")
        c = _llm("c")
        e1 = Edge(source="a", source_port="text", target="b", target_port="input")
        e2 = Edge(source="b", source_port="verdict", target="c", target_port="prompt")
        g = WorkflowGraph(nodes=(a, b, c), edges=(e1, e2))
        report = validate_graph(g)
        assert report.ok

    def test_cycle_rejected(self) -> None:
        a = _llm("a")
        b = _eval("b")
        e1 = Edge(source="a", source_port="text", target="b", target_port="input")
        e2 = Edge(source="b", source_port="verdict", target="a", target_port="prompt")
        g = WorkflowGraph(nodes=(a, b), edges=(e1, e2))
        report = validate_graph(g)
        assert not report.ok
        assert any("环" in err.message for err in report.errors)

    def test_self_loop_rejected(self) -> None:
        a = _llm("a")
        e = Edge(source="a", source_port="text", target="a", target_port="prompt")
        g = WorkflowGraph(nodes=(a,), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert any("环" in err.message for err in report.errors)

    def test_diamond_no_cycle_ok(self) -> None:
        """菱形图 A→B→D, A→C→D 无环。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        d = _eval("d")
        edges = (
            Edge(source="a", source_port="text", target="b", target_port="prompt"),
            Edge(source="a", source_port="text", target="c", target_port="prompt"),
            Edge(source="b", source_port="text", target="d", target_port="input"),
            Edge(source="c", source_port="text", target="d", target_port="input"),
        )
        g = WorkflowGraph(nodes=(a, b, c, d), edges=edges)
        report = validate_graph(g)
        assert report.ok


# ---------- 类型兼容 ----------


class TestValidateTypeCompat:
    def test_text_to_text_ok(self) -> None:
        a = _llm("a")
        b = _eval("b")
        e = Edge(source="a", source_port="text", target="b", target_port="input")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert report.ok

    def test_text_to_documents_rejected(self) -> None:
        a = _llm("a")
        b = NodeBase(
            id="b",
            kind=NodeKind.RAG,
            inputs=(Port(name="query", kind=PortKind.DOCUMENTS),),
            outputs=(Port(name="documents", kind=PortKind.DOCUMENTS),),
            params={"query": "test"},
        )
        e = Edge(source="a", source_port="text", target="b", target_port="query")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert any("类型不兼容" in err.message for err in report.errors)

    def test_any_to_text_ok(self) -> None:
        a = NodeBase(
            id="a",
            kind=NodeKind.CODE,
            inputs=(Port(name="input", kind=PortKind.ANY, required=False),),
            outputs=(Port(name="result", kind=PortKind.ANY),),
            params={"code": "print(1)"},
        )
        b = _eval("b")
        e = Edge(source="a", source_port="result", target="b", target_port="input")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert report.ok


# ---------- 必填参数 ----------


class TestValidateRequiredParams:
    def test_llm_missing_prompt_rejected(self) -> None:
        a = NodeBase(
            id="a",
            kind=NodeKind.LLM,
            inputs=LLM_INPUTS,
            outputs=LLM_OUTPUTS,
            params={"model": "gpt-4"},  # 缺 prompt
        )
        g = WorkflowGraph(nodes=(a,))
        report = validate_graph(g)
        assert not report.ok
        assert any("prompt" in err.message for err in report.errors)

    def test_llm_missing_model_rejected(self) -> None:
        a = NodeBase(
            id="a",
            kind=NodeKind.LLM,
            inputs=LLM_INPUTS,
            outputs=LLM_OUTPUTS,
            params={"prompt": "hi"},  # 缺 model
        )
        g = WorkflowGraph(nodes=(a,))
        report = validate_graph(g)
        assert not report.ok
        assert any("model" in err.message for err in report.errors)

    def test_branch_missing_condition_rejected(self) -> None:
        b = NodeBase(
            id="b",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            params={"branches": {"yes": "t1"}},  # 缺 condition
        )
        g = WorkflowGraph(nodes=(b,))
        report = validate_graph(g)
        assert not report.ok
        assert any("condition" in err.message for err in report.errors)

    def test_loop_missing_goal_rejected(self) -> None:
        n = NodeBase(
            id="loop1",
            kind=NodeKind.LOOP,
            inputs=LOOP_INPUTS,
            outputs=LOOP_OUTPUTS,
            params={},  # 缺 goal
        )
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g)
        assert not report.ok
        assert any("goal" in err.message for err in report.errors)

    def test_all_params_present_ok(self) -> None:
        a = _llm("a")
        g = WorkflowGraph(nodes=(a,))
        report = validate_graph(g)
        assert report.ok


# ---------- Loop 断言可验证性 ----------


class TestValidateLoopAssertions:
    def test_valid_loop_ok(self) -> None:
        n = _loop("loop1")
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g)
        assert report.ok

    def test_empty_assertions_rejected(self) -> None:
        n = _loop(
            "loop1",
            goal_data={
                "task": "test",
                "mode": "quality",
                "assertions": [],  # 空断言
            },
        )
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g)
        assert not report.ok
        assert any("goal" in err.message for err in report.errors)

    def test_all_non_blocking_rejected(self) -> None:
        n = _loop(
            "loop1",
            goal_data={
                "task": "test",
                "mode": "quality",
                "assertions": [
                    {
                        "id": "soft",
                        "kind": "regex",
                        "spec": {"pattern": ".*"},
                        "blocking": False,
                    }
                ],
            },
        )
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g)
        assert not report.ok

    def test_unverifiable_goal_rejected(self) -> None:
        """COMMAND 断言 + sandbox_available=False → 不可验证。"""
        n = _loop(
            "loop1",
            goal_data={
                "task": "test",
                "mode": "verify_execute",
                "assertions": [
                    {
                        "id": "cmd",
                        "kind": "command",
                        "spec": {"cmd": "pytest -x"},
                        "blocking": True,
                    }
                ],
            },
        )
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g, sandbox_available=False)
        assert not report.ok

    def test_command_with_sandbox_ok(self) -> None:
        n = _loop(
            "loop1",
            goal_data={
                "task": "test",
                "mode": "verify_execute",
                "assertions": [
                    {
                        "id": "cmd",
                        "kind": "command",
                        "spec": {"cmd": "pytest -x"},
                        "blocking": True,
                    }
                ],
            },
        )
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g, sandbox_available=True)
        assert report.ok

    def test_invalid_goal_spec_rejected(self) -> None:
        n = _loop(
            "loop1",
            goal_data={
                "task": "test",
                "mode": "quality",
                # 缺 assertions
            },
        )
        g = WorkflowGraph(nodes=(n,))
        report = validate_graph(g)
        assert not report.ok
        assert any("goal" in err.message for err in report.errors)


# ---------- 综合场景 ----------


class TestValidateComposite:
    def test_multi_error_reported(self) -> None:
        """多个错误同时报告。"""
        a = _llm("dup", prompt="hello")  # 缺 model 也要重复 id
        b = _llm("dup")  # 重复 id
        e = Edge(source="nonexistent", source_port="x", target="dup", target_port="y")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        report = validate_graph(g)
        assert not report.ok
        assert len(report.errors) >= 2

    def test_complex_valid_graph(self) -> None:
        """LLM → eval → branch → two LLMs (条件分支图)。"""
        llm1 = _llm("llm1")
        eval1 = _eval("eval1")
        branch1 = _branch("branch1", branches={"pass": "llm2", "fail": "llm3"})
        llm2 = _llm("llm2", prompt="good")
        llm3 = _llm("llm3", prompt="bad")
        edges = (
            Edge(source="llm1", source_port="text", target="eval1", target_port="input"),
            Edge(source="eval1", source_port="verdict", target="branch1", target_port="input"),
            Edge(source="branch1", source_port="route", target="llm2", target_port="prompt"),
            Edge(source="branch1", source_port="route", target="llm3", target_port="prompt"),
        )
        g = WorkflowGraph(nodes=(llm1, eval1, branch1, llm2, llm3), edges=edges)
        report = validate_graph(g)
        assert report.ok
