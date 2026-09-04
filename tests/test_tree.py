"""调用树构建测试。

重点覆盖三种脏数据：父缺失、成环、多根。这些在真实采集里必然出现
（采样丢弃、乱序到达、上报 bug），树构建必须不丢 span 也不爆栈。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from ariadne.api.tree import build_tree, flatten_tree

BASE = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)


def row(span_id: str, parent: str = "", *, offset_ms: int = 0, dur: int = 100,
        name: str = "n") -> dict[str, Any]:
    return {
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "kind": "internal",
        "status": "ok",
        "started_at": BASE + timedelta(milliseconds=offset_ms),
        "duration_ms": dur,
        "cost_usd": "0",
    }


def test_builds_nested_tree() -> None:
    roots = build_tree([
        row("a", dur=300),
        row("b", "a", offset_ms=10, dur=100),
        row("c", "b", offset_ms=20, dur=50),
    ])
    assert len(roots) == 1
    assert roots[0].span_id == "a"
    assert roots[0].children[0].span_id == "b"
    assert roots[0].children[0].children[0].span_id == "c"


def test_self_ms_excludes_children() -> None:
    """self_ms 是定位真实瓶颈的关键：父耗时长可能只是在等子节点。"""
    roots = build_tree([
        row("a", dur=300),
        row("b", "a", dur=100),
        row("c", "a", dur=150),
    ])
    root = roots[0]
    assert root.duration_ms == 300
    assert root.self_ms == 50  # 300 - (100 + 150)
    assert root.children[0].self_ms == 100


def test_self_ms_never_negative() -> None:
    """子节点耗时之和超过父节点（时钟偏移/并发）时不应出负数。"""
    roots = build_tree([row("a", dur=50), row("b", "a", dur=200)])
    assert roots[0].self_ms == 0


def test_missing_parent_becomes_root() -> None:
    """父 span 被采样丢弃时，子 span 必须仍然可见。"""
    roots = build_tree([row("orphan", "nonexistent")])
    assert len(roots) == 1
    assert roots[0].span_id == "orphan"


def test_multiple_roots_all_returned() -> None:
    roots = build_tree([row("a", offset_ms=0), row("b", offset_ms=10)])
    assert {r.span_id for r in roots} == {"a", "b"}


def test_cycle_is_broken_not_infinite_loop() -> None:
    """a→b→a 的环必须被断开，且两个节点都不丢。"""
    roots = build_tree([row("a", "b"), row("b", "a")])
    ids = {n.span_id for _, n in flatten_tree(roots)}
    assert ids == {"a", "b"}
    # 展平结果不应重复（说明环真的断了）
    assert len(flatten_tree(roots)) == 2


def test_self_referencing_span_becomes_root() -> None:
    roots = build_tree([row("a", "a")])
    assert len(roots) == 1
    assert roots[0].parent_span_id == "a"  # 保留原始值以便排查数据问题


def test_children_sorted_by_start_time() -> None:
    """乱序到达的 span 在树里必须按时间排好。"""
    roots = build_tree([
        row("a", dur=500),
        row("late", "a", offset_ms=200),
        row("early", "a", offset_ms=50),
    ])
    assert [c.span_id for c in roots[0].children] == ["early", "late"]


def test_deep_tree_does_not_stack_overflow() -> None:
    """1000 层深树：self_ms 计算与排序都必须是迭代实现。"""
    rows = [row("s0", dur=2000)]
    rows += [row(f"s{i}", f"s{i-1}", offset_ms=i, dur=1) for i in range(1, 1000)]
    roots = build_tree(rows)
    assert len(flatten_tree(roots)) == 1000


def test_token_aggregation_on_node() -> None:
    r = row("a")
    r.update({"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 200,
              "cache_write_tokens": 10})
    node = build_tree([r])[0]
    assert node.total_tokens == 360


def test_empty_input() -> None:
    assert build_tree([]) == []
