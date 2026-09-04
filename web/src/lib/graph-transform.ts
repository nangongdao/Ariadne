/**
 * React Flow ↔ 后端 WorkflowGraph 的双向转换、dagre 自动布局，
 * 以及新建节点时用的标准端口/参数模板。
 *
 * 端口与参数模板必须与后端 graph_module/models.py 保持一致，
 * 否则新建的节点一提交就会被校验驳回。
 */

import {
  type Edge as RFEdge,
  type EdgeChange,
  type Node as RFNode,
  type NodeChange,
  MarkerType,
} from "@xyflow/react";
import dagre from "dagre";

import type { GraphEdgeData, GraphNodeData, GraphPort } from "@/api/types";

/** 自定义节点在画布上的实际尺寸，dagre 需要它来排布。 */
const NODE_WIDTH = 180;
const NODE_HEIGHT = 60;

/** AriadneNode 读取的 data 形状。RFNode["data"] 是宽松索引类型，需要断言。 */
export interface AriadneNodeData {
  kind: string;
  label: string;
  params: Record<string, unknown>;
  inputs: GraphPort[];
  outputs: GraphPort[];
}

export function layoutGraph(
  nodes: RFNode[],
  edges: RFEdge[],
): { nodes: RFNode[]; edges: RFEdge[] } {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 50, ranksep: 80 });
  g.setDefaultEdgeLabel(() => ({}));

  nodes.forEach((node) => {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });
  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
    };
  });

  return { nodes: layoutedNodes, edges };
}

export function toFlowNodes(graphNodes: GraphNodeData[]): RFNode[] {
  return graphNodes.map((n) => ({
    id: n.id,
    type: "ariadneNode",
    position: { x: 0, y: 0 },
    data: {
      kind: n.kind,
      label: n.id,
      params: n.params,
      inputs: n.inputs,
      outputs: n.outputs,
    },
  }));
}

export function toFlowEdges(graphEdges: GraphEdgeData[]): RFEdge[] {
  return graphEdges.map((e, i) => ({
    id: `e-${e.source}-${e.target}-${i}`,
    source: e.source,
    target: e.target,
    sourceHandle: e.source_port,
    targetHandle: e.target_port,
    markerEnd: { type: MarkerType.ArrowClosed },
  }));
}

export function toGraphNodes(rfNodes: RFNode[]): GraphNodeData[] {
  return rfNodes.map((n) => {
    const data = n.data as unknown as AriadneNodeData;
    return {
      id: n.id,
      kind: data.kind,
      inputs: data.inputs,
      outputs: data.outputs,
      params: data.params,
    };
  });
}

export function toGraphEdges(rfEdges: RFEdge[]): GraphEdgeData[] {
  return rfEdges.map((e) => ({
    source: e.source,
    source_port: (e.sourceHandle as string) ?? "",
    target: e.target,
    target_port: (e.targetHandle as string) ?? "",
  }));
}

/**
 * 只有真正改变图结构的变更才算脏。
 * 位置/尺寸/选中不算：位置不进序列化（toGraphNodes 不含 position，
 * 加载时由 dagre 重算），若把拖拽也算脏会导致大量无意义的离开确认。
 */
export function isStructuralNodeChange(change: NodeChange<RFNode>): boolean {
  return change.type === "add" || change.type === "remove" || change.type === "replace";
}

export function isStructuralEdgeChange(change: EdgeChange<RFEdge>): boolean {
  return change.type === "add" || change.type === "remove" || change.type === "replace";
}

/** 可添加的节点类型，顺序即工具栏顺序。 */
export const AVAILABLE_KINDS: ReadonlyArray<string> = [
  "llm",
  "tool",
  "rag",
  "code",
  "branch",
  "loop",
  "eval",
  "subgraph",
];

/** 各类型的标准端口（与后端 models.py 一致）。 */
export const STD_PORTS: Record<string, { inputs: GraphPort[]; outputs: GraphPort[] }> = {
  llm: {
    inputs: [{ name: "prompt", kind: "text", required: true }],
    outputs: [{ name: "text", kind: "text", required: true }],
  },
  tool: {
    inputs: [{ name: "args", kind: "json", required: false }],
    outputs: [{ name: "result", kind: "text", required: true }],
  },
  rag: {
    inputs: [{ name: "query", kind: "text", required: true }],
    outputs: [{ name: "documents", kind: "documents", required: true }],
  },
  code: {
    inputs: [{ name: "input", kind: "any", required: false }],
    outputs: [{ name: "result", kind: "text", required: true }],
  },
  branch: {
    inputs: [{ name: "input", kind: "any", required: true }],
    outputs: [{ name: "route", kind: "any", required: false }],
  },
  loop: {
    inputs: [{ name: "input", kind: "any", required: false }],
    outputs: [
      { name: "output", kind: "text", required: true },
      { name: "iterations", kind: "json", required: true },
      { name: "converged", kind: "json", required: true },
    ],
  },
  eval: {
    inputs: [{ name: "input", kind: "text", required: true }],
    outputs: [
      { name: "passed", kind: "json", required: true },
      { name: "verdict", kind: "text", required: true },
    ],
  },
  subgraph: {
    inputs: [{ name: "input", kind: "any", required: false }],
    outputs: [{ name: "output", kind: "any", required: true }],
  },
};

/** 新建节点的参数模板，给出键名让人知道该填什么，值留空。 */
export const DEFAULT_PARAMS: Record<string, Record<string, unknown>> = {
  llm: { prompt: "", model: "gpt-4", temperature: 0.7, max_tokens: 4096 },
  tool: { cmd: "", args: {} },
  rag: { query: "", top_k: 5, index: "default" },
  code: { code: "", language: "python" },
  branch: { condition: "", branches: {} },
  loop: { goal: { task: "", assertions: [] } },
  eval: { assertions: [] },
  subgraph: { graph: { version: "1", nodes: [], edges: [] } },
};
