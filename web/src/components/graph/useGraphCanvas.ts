/**
 * 图画布状态 —— 节点/连线/选中/脏标记，以及所有改动画布的操作。
 *
 * 从 GraphEditorPage 拆出来的原因：那个文件同时管着取数、保存、版本跳转和
 * 画布操作，两类关注点的依赖完全不重叠。这里只关心"画布上有什么"。
 */

import {
  type Connection,
  type Edge as RFEdge,
  type EdgeChange,
  type Node as RFNode,
  type NodeChange,
  addEdge,
  MarkerType,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import { useCallback, useMemo, useState } from "react";

import {
  DEFAULT_PARAMS,
  isStructuralEdgeChange,
  isStructuralNodeChange,
  STD_PORTS,
  toGraphEdges,
  toGraphNodes,
} from "@/lib/graph-transform";

export function useGraphCanvas() {
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<RFNode>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const { fitView } = useReactFlow();

  // 画布变更 → 置脏。位置/选中不算（见 isStructuralNodeChange 注释）
  const handleNodesChange = useCallback(
    (changes: NodeChange<RFNode>[]) => {
      if (changes.some(isStructuralNodeChange)) setDirty(true);
      onNodesChange(changes);
    },
    [onNodesChange],
  );

  const handleEdgesChange = useCallback(
    (changes: EdgeChange<RFEdge>[]) => {
      if (changes.some(isStructuralEdgeChange)) setDirty(true);
      onEdgesChange(changes);
    },
    [onEdgesChange],
  );

  const selectedNode = useMemo(
    () => rfNodes.find((n) => n.id === selectedNodeId) ?? null,
    [rfNodes, selectedNodeId],
  );

  const addNode = useCallback(
    (kind: string) => {
      const id = `${kind}_${Date.now()}`;
      const ports = STD_PORTS[kind] ?? { inputs: [], outputs: [] };
      setRfNodes((nds) => [
        ...nds,
        {
          id,
          type: "ariadneNode",
          position: { x: Math.random() * 300, y: Math.random() * 200 },
          data: {
            kind,
            label: id,
            params: DEFAULT_PARAMS[kind] ?? {},
            inputs: ports.inputs,
            outputs: ports.outputs,
          },
        },
      ]);
      setDirty(true);
    },
    [setRfNodes],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      setRfEdges((eds) =>
        addEdge({ ...connection, markerEnd: { type: MarkerType.ArrowClosed } }, eds),
      );
      setDirty(true);
    },
    [setRfEdges],
  );

  /**
   * 更新单个参数。
   *
   * 只替换真正变化的那个节点对象，其余保持原引用，
   * 让 memo 化的 AriadneNode 不被整体击穿。合并在 setState 回调里做，
   * 拿到的一定是最新 params，不会被并发的兄弟字段覆盖。
   */
  const applyNodeParam = useCallback(
    (id: string, key: string, value: unknown) => {
      setRfNodes((nds) => {
        const idx = nds.findIndex((n) => n.id === id);
        if (idx === -1) return nds;
        const target = nds[idx]!;
        const data = target.data as { params?: Record<string, unknown> };
        const currentParams = data.params ?? {};
        if (Object.is(currentParams[key], value)) return nds;
        const next = [...nds];
        next[idx] = {
          ...target,
          data: { ...target.data, params: { ...currentParams, [key]: value } },
        };
        return next;
      });
      setDirty(true);
    },
    [setRfNodes],
  );

  const deleteNode = useCallback(
    (id: string) => {
      setRfNodes((nds) => nds.filter((n) => n.id !== id));
      setRfEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
      setSelectedNodeId((cur) => (cur === id ? null : cur));
      setDirty(true);
    },
    [setRfNodes, setRfEdges],
  );

  // 选中并把视图移到某个节点（校验问题列表点击时用）
  const focusNode = useCallback(
    (id: string) => {
      setSelectedNodeId(id);
      setRfNodes((nds) =>
        nds.map((n) => (n.selected === (n.id === id) ? n : { ...n, selected: n.id === id })),
      );
      void fitView({ nodes: [{ id }], duration: 350, maxZoom: 1.4 });
    },
    [setRfNodes, fitView],
  );

  const knownNodeIds = useMemo(() => new Set(rfNodes.map((n) => n.id)), [rfNodes]);

  const serializeGraph = useCallback(
    (): Record<string, unknown> => ({
      version: "1",
      graph: { version: "1", nodes: toGraphNodes(rfNodes), edges: toGraphEdges(rfEdges) },
    }),
    [rfNodes, rfEdges],
  );

  /** 某种 kind 的节点数 —— 保存前拦多 Loop 图用 */
  const countByKind = useCallback(
    (kind: string) => rfNodes.filter((n) => (n.data as { kind?: string }).kind === kind).length,
    [rfNodes],
  );

  return {
    rfNodes,
    rfEdges,
    setRfNodes,
    setRfEdges,
    selectedNode,
    setSelectedNodeId,
    dirty,
    setDirty,
    handleNodesChange,
    handleEdgesChange,
    addNode,
    onConnect,
    applyNodeParam,
    deleteNode,
    focusNode,
    knownNodeIds,
    serializeGraph,
    countByKind,
  };
}
