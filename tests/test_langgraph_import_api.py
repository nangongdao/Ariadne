"""LangGraph 导入 API 单元测试。

使用 conftest 中的 client fixture（TestClient + 内存桩），验证端点逻辑。
"""

from fastapi.testclient import TestClient


def test_import_langgraph_save(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    """导入 LangGraph 图并保存。"""
    payload = {
        "nodes": {
            "start": {},
            "process": {},
            "end": {},
        },
        "edges": [
            ["start", "process"],
            ["process", "end"],
        ],
        "branches": {},
        "name": "imported_graph",
        "description": "从 LangGraph 导入的测试图",
    }

    resp = client.post(
        "/v1/graphs/import-langgraph",
        json=payload,
        headers=auth,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["name"] == "imported_graph"
    assert data["version"] == 1
    assert data["is_active"] is True
    assert data["description"] == "从 LangGraph 导入的测试图"

    # 验证转换结果：graph 字段是嵌套的 spec，内层才有 nodes
    graph_data = data["graph"]
    assert "graph" in graph_data
    inner_graph = graph_data["graph"]
    assert "nodes" in inner_graph
    nodes = inner_graph["nodes"]

    # 应该有 3 个节点（start/process/end）
    assert len(nodes) >= 3

    # 检查节点是否标记了来源（在 params 字段中）
    for node in nodes:
        if node.get("id") in ["start", "process", "end"]:
            params = node.get("params", {})
            assert params.get("_imported_from") == "langgraph"


def test_import_langgraph_with_branches(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    """条件边经 JSON 契约传入后要变成 branch 节点。

    这条用例盯的是端点自己的转换层：JSON 里 branches 是纯 dict，而导入器
    用 getattr(spec, "ends") 读映射。端点必须把内层 dict 包成带 .ends 的
    对象，否则一切条件边都会被误判成"运行时路由"而拒掉（400）。
    """
    payload = {
        "nodes": {
            "route": {},
            "path_a": {},
            "path_b": {},
        },
        "edges": [
            ["__start__", "route"],
        ],
        "branches": {
            "route": {
                "pick": {"ends": {"a": "path_a", "b": "path_b"}},
            },
        },
        "name": "branching_graph",
        "description": "带条件分支的导入图",
    }

    resp = client.post(
        "/v1/graphs/import-langgraph",
        json=payload,
        headers=auth,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "branching_graph"

    nodes = data["graph"]["graph"]["nodes"]
    branch_nodes = [n for n in nodes if n.get("kind") == "branch"]
    assert len(branch_nodes) == 1, f"条件边应转换为 1 个 branch 节点，实得 {branch_nodes}"

    # 路由表要带上两个分支目标，不能是空 dict —— 空 dict 也能过 kind 断言
    routes = branch_nodes[0]["params"]["branches"]
    assert routes == {"a": "path_a", "b": "path_b"}, routes


def test_import_langgraph_no_save(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    """导入 LangGraph 图但不保存（name 为空）。"""
    payload = {
        "nodes": {
            "start": {},
            "end": {},
        },
        "edges": [
            ["start", "end"],
        ],
        "branches": {},
        "name": "",  # 空名称 -> 不保存
    }

    resp = client.post(
        "/v1/graphs/import-langgraph",
        json=payload,
        headers=auth,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["name"] == "<未保存>"
    assert data["version"] == 0
    assert data["is_active"] is False

    # 验证图确实没有保存到数据库
    list_resp = client.get(
        "/v1/graphs",
        headers=auth,
    )
    assert list_resp.status_code == 200
    graphs = list_resp.json()

    # 不应该有 "<未保存>" 的图
    assert not any(g["name"] == "<未保存>" for g in graphs)


def test_import_langgraph_duplicate_name(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    """导入时名称冲突应返回 409。"""
    payload = {
        "nodes": {"start": {}, "end": {}},
        "edges": [["start", "end"]],
        "branches": {},
        "name": "duplicate_test",
    }

    # 第一次导入
    resp1 = client.post(
        "/v1/graphs/import-langgraph",
        json=payload,
        headers=auth,
    )
    assert resp1.status_code == 201

    # 第二次导入相同名称
    resp2 = client.post(
        "/v1/graphs/import-langgraph",
        json=payload,
        headers=auth,
    )
    assert resp2.status_code == 409, "重复名称应返回 409 Conflict"
    assert "已存在" in resp2.json()["detail"]


def test_import_langgraph_invalid_structure(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    """导入边引用不存在节点的图应报错。"""
    payload = {
        "nodes": {"start": {}},
        "edges": [["start", "nonexistent"]],  # 边指向不存在的节点
        "branches": {},
        "name": "broken_graph",
    }

    resp = client.post(
        "/v1/graphs/import-langgraph",
        json=payload,
        headers=auth,
    )

    # 校验应该在保存前拦住，返回 422
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "校验失败" in detail or "不存在" in detail

