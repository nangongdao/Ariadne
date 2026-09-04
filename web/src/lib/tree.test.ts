/**
 * 锁定"只看失败"必须穿透折叠层级。
 *
 * 回归的是一个会让人得出相反结论的 bug：flattenVisible 遇到折叠节点就跳过整棵子树，
 * 而调用树默认只展开两层，于是深层错误被筛没了 —— 列表页显示"失败 1"，
 * 树里勾"只看失败"是 0 行。
 */

import { describe, expect, it } from "vitest";

import type { SpanNode, SpanStatus } from "@/api/types";

import { collapseBeyondDepth, flattenErrors, flattenVisible, totalTokens } from "./tree";

/** 不用 as 断言：字段缺一个就该编译报错，否则契约变了测试还在假装通过 */
function span(id: string, status: SpanStatus, children: SpanNode[] = []): SpanNode {
  return {
    span_id: id,
    parent_span_id: "",
    name: id,
    kind: "llm",
    operation: "",
    status,
    error_type: "",
    provider: "",
    model_request: "",
    model_response: "",
    started_at: "2026-01-01T00:00:00Z",
    duration_ms: 1000,
    self_ms: 500,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    reasoning_tokens: 0,
    cost_usd: "0",
    input_preview: "",
    output_preview: "",
    input_ref: "",
    output_ref: "",
    loop_id: "",
    iteration: 0,
    attributes: {},
    tags: [],
    children,
  };
}

/** 错误埋在 depth 3，默认折叠深度是 2 —— 正是线上会踩到的形状 */
const DEEP_TREE = [
  span("root", "ok", [
    span("d1", "ok", [span("d2", "ok", [span("d3-boom", "error")])]),
    span("shallow-boom", "error"),
  ]),
];

const NO_PATH: ReadonlySet<string> = new Set();

describe("flattenErrors", () => {
  it("折叠状态下仍能找到深层错误 —— 这是这个函数存在的唯一理由", () => {
    const ids = flattenErrors(DEEP_TREE, NO_PATH).map((r) => r.node.span_id);
    expect(ids).toContain("d3-boom");
  });

  it("对照：flattenVisible 加筛选会漏掉它（记录 bug 的成因）", () => {
    const collapsed = collapseBeyondDepth(DEEP_TREE, 2);
    const visibleErrors = flattenVisible(DEEP_TREE, collapsed, NO_PATH)
      .filter((r) => r.node.status !== "ok")
      .map((r) => r.node.span_id);
    expect(visibleErrors).not.toContain("d3-boom");
  });

  it("只返回失败节点，不夹带 ok 的祖先", () => {
    const ids = flattenErrors(DEEP_TREE, NO_PATH).map((r) => r.node.span_id);
    expect(ids).toEqual(["d3-boom", "shallow-boom"]);
  });

  it("深度归 0 且不给三角 —— 祖先没显示，缩进和层级线会指向不存在的父行", () => {
    for (const row of flattenErrors(DEEP_TREE, NO_PATH)) {
      expect(row.depth).toBe(0);
      expect(row.hasChildren).toBe(false);
    }
  });

  it("保留关键路径标记", () => {
    const rows = flattenErrors(DEEP_TREE, new Set(["d3-boom"]));
    expect(rows.find((r) => r.node.span_id === "d3-boom")?.onCriticalPath).toBe(true);
  });

  it("失败节点自身的失败子节点也要列出，不因父节点已命中就停止下钻", () => {
    const nested = [span("a", "error", [span("b", "error")])];
    expect(flattenErrors(nested, NO_PATH).map((r) => r.node.span_id)).toEqual(["a", "b"]);
  });

  it("全树无失败时返回空数组", () => {
    const clean = [span("r", "ok", [span("c", "ok")])];
    expect(flattenErrors(clean, NO_PATH)).toEqual([]);
  });

  it("按前序输出，与展开态的阅读顺序一致", () => {
    const tree = [
      span("r", "ok", [span("x", "error"), span("y", "ok", [span("z", "error")])]),
    ];
    expect(flattenErrors(tree, NO_PATH).map((r) => r.node.span_id)).toEqual(["x", "z"]);
  });
});

describe("totalTokens", () => {
  it("不含 reasoning_tokens —— 多数 provider 已把它计入 output，加了就是重复计数", () => {
    const node = span("n", "ok");
    const withUsage: SpanNode = {
      ...node,
      input_tokens: 100,
      output_tokens: 2050,
      reasoning_tokens: 2000,
    };
    expect(totalTokens(withUsage)).toBe(2150);
  });
});
