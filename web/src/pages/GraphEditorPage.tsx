/**
 * 图编排编辑页 —— React Flow 画布 + 节点配置面板 + 校验 + 保存。
 *
 * 本文件只管数据流与编排：取图、灌画布、脏状态守卫、校验、保存。
 * 节点渲染见 components/graph/AriadneNode，侧栏见同目录的两个面板，
 * 序列化与布局见 lib/graph-transform。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
} from "@xyflow/react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type {
  GraphEdgeData,
  GraphNodeData,
  GraphResponse,
  GraphValidationError,
} from "@/api/types";
import {
  miniMapNodeColor,
  miniMapNodeStrokeColor,
  nodeTypes,
} from "@/components/graph/AriadneNode";
import { NodeToolbar, ValidationPanel } from "@/components/graph/GraphPanels";
import { GraphToolbar } from "@/components/graph/GraphToolbar";
import { NodeConfigPanel } from "@/components/graph/NodeConfigPanel";
import { useGraphCanvas } from "@/components/graph/useGraphCanvas";
import { TableSkeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import { UnsavedChangesDialog } from "@/components/UnsavedChangesDialog";
import { layoutGraph, toFlowEdges, toFlowNodes } from "@/lib/graph-transform";
import { navigationGuard } from "@/lib/navigation-guard";
import { ErrorBox } from "@/pages/TraceListPage";

import "@xyflow/react/dist/style.css";

function GraphEditorInner() {
  const { graphId } = useParams<{ graphId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isNew = graphId === "new" || graphId === undefined;

  const canvas = useGraphCanvas();
  const { setRfNodes, setRfEdges, setDirty, serializeGraph } = canvas;
  const [validationErrors, setValidationErrors] = useState<GraphValidationError[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  /** 已灌入画布的图 id —— 首次加载守卫，避免重取覆盖未保存的编辑 */
  const hydratedIdRef = useRef<string | null>(null);
  /** 挂起的离开目的地（侧栏/面板/顶栏跳转都会经过这里），或 null 不离开 */
  const [pendingLeaveTo, setPendingLeaveTo] = useState<string | null>(null);
  /** 「保存并离开」的目标 —— 保存成功后由定义处 onSuccess 统一跳转，
      避免与版本更新后的 /graphs/{newId} 跳转打架 */
  const leaveTargetRef = useRef<string | null>(null);
  /** 用户已主动离开/放弃离开：保存请求发出后仍可能在途，此时 onSuccess
      不应再按任何目标跳转，否则会把已经走掉的人拽回来 */
  const userLeftRef = useRef(false);

  const { data: graphData, isPending } = useQuery({
    queryKey: ["graph", graphId],
    queryFn: () => api.getGraph(graphId as string),
    enabled: !isNew,
  });

  /**
   * 服务端数据 → 画布，**只在首次加载时**执行。
   *
   * 原来写在 useMemo 里（渲染期副作用，React 19 严格模式会重复执行），
   * 且每次 ["graph", graphId] 重取都会覆盖画布上未保存的编辑。
   * 现在用 useEffect + hydratedIdRef 守卫：同一个图只灌一次。
   */
  useEffect(() => {
    if (!graphData) return;
    if (hydratedIdRef.current === graphData.id) return;
    hydratedIdRef.current = graphData.id;

    const wfData = graphData.graph as {
      graph?: { nodes: GraphNodeData[]; edges: GraphEdgeData[] };
    };
    const inner = wfData.graph;
    if (inner) {
      const { nodes, edges } = layoutGraph(
        toFlowNodes(inner.nodes),
        toFlowEdges(inner.edges),
      );
      setRfNodes(nodes);
      setRfEdges(edges);
    }
    setName(graphData.name);
    setDescription(graphData.description ?? "");
    setValidationErrors(graphData.validation_errors ?? []);
    setDirty(false);
  }, [graphData, setRfNodes, setRfEdges, setDirty]);

  // 关标签/刷新前拦一道（路由内的「返回」由 handleBack 处理）
  useEffect(() => {
    if (!canvas.dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Safari 至今只认 returnValue，不设的话那边根本不弹确认
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [canvas.dirty]);

  // 画布/名称/描述是否已改动 —— 供守卫闭包读取，避免 effect 重注册前的
  // 短暂窗口读到旧值（保存成功瞬间点导航会被误拦一次）
  const dirtyRef = useRef(false);
  dirtyRef.current = canvas.dirty;

  // 侧栏 NavLink / Ctrl+K 面板 / 顶栏状态按钮这些客户端跳转不进 handleBack，
  // 也不触发 beforeunload —— 没有守卫就会静默丢画布。这里注册全局守卫，
  // 脏时把跳转挂起，由三选项对话框决定去留。
  useEffect(() => {
    return navigationGuard.register((to) => {
      if (!dirtyRef.current) return true;
      setPendingLeaveTo(to);
      return false;
    });
  }, []);

  const validateMutation = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.validateGraph(payload),
    onSuccess: (result) => {
      // 结果渲染进侧栏可点击列表，不再用 window.alert
      setValidationErrors([...result.errors, ...result.warnings]);
      if (result.ok) {
        toast.success("校验通过");
      } else {
        toast.error(`校验失败：${result.errors.length} 个错误，见右侧「校验问题」`);
      }
    },
  });

  const saveMutation = useMutation({
    mutationFn: (payload: {
      name: string;
      graph: Record<string, unknown>;
      description: string;
    }) => (isNew ? api.createGraph(payload) : api.updateGraph(graphId as string, payload)),
    onSuccess: (g: GraphResponse) => {
      void queryClient.invalidateQueries({ queryKey: ["graphs"] });
      setValidationErrors(g.validation_errors ?? []);
      setDirty(false);
      toast.success(isNew ? "已创建" : "已保存");

      // 保存请求在途时用户可能已点了「不保存离开」或关掉对话框离开，
      // 此后的 onSuccess 不应再跳任何地方，否则会把已经走掉的人拽回编辑器
      if (userLeftRef.current) return;

      // 「保存并离开」的目标在成功后统一在这里消费，用完即清，
      // 避免下次普通保存又被残留的旧目标带偏
      const leaveTarget = leaveTargetRef.current;
      leaveTargetRef.current = null;

      // 后端 PUT 是版本化写入：插入一行新 uuid，旧行 is_active=False。
      // 地址栏还停在旧 id 的话，第二次保存会被 is_active 过滤掉 → 404
      // 「图不存在」，用户被困在编辑器里只能回列表重开。
      if (g.id !== graphId) {
        // 先认领新 id，跳转后的首次取数才不会把刚保存的画布重新灌一遍
        hydratedIdRef.current = g.id;
        queryClient.setQueryData(["graph", g.id], g);
        // 旧 id 已失活，留着缓存只会在返回时喂出一个查不到的图
        queryClient.removeQueries({ queryKey: ["graph", graphId] });
        navigate(leaveTarget ?? `/graphs/${g.id}`, { replace: true });
      } else {
        queryClient.setQueryData(["graph", graphId], g);
        if (leaveTarget !== null) navigate(leaveTarget, { replace: true });
      }
    },
    onError: (err) => {
      // 保存失败：清掉「保存并离开」残留目标，否则用户稍后手动保存
      // 会沿旧目标被带走
      leaveTargetRef.current = null;
      // 422 的 problem 带 reasons（逐条校验失败）。只显示 message 的话用户
      // 只看到"图校验失败"，还得自己再点一次「校验」才知道是哪个节点。
      const problem = err instanceof ApiError ? err.problem : null;
      const reasons = problem?.["reasons"];
      if (Array.isArray(reasons) && reasons.length > 0) {
        setValidationErrors(
          reasons.map((r) => ({ field: "graph", message: String(r), severity: "error" })),
        );
      }
    },
  });

  // 浏览器前进/后退（popstate）在 SPA 内不触发 beforeunload —— 脏态下
  // 按后退会静默丢画布。这里在后退发生时把地址栏恢复回编辑器、挂起离开，
  // 让三选项对话框来裁决。replaceState 不触发 popstate，不会递归；
  // 且不能用 pushState —— 那会打断 react-router 内部的 history idx 链，
  // 之后的进退会跳错条目
  useEffect(() => {
    if (!canvas.dirty) return;
    const onPopState = () => {
      const current = window.location.pathname + window.location.search;
      // 对话框已打开时再按后退：把它当「留在本页」，关掉对话框并复原地址
      if (pendingLeaveTo !== null) {
        window.history.replaceState(null, "", current);
        setPendingLeaveTo(null);
        return;
      }
      // 保存在途时按后退：人已经走了，别再拦回来 —— 但保存结果可能
      // 仍带版本化跳转，得标记"已离开"让 onSuccess 别再拽人
      if (saveMutation.isPending) {
        userLeftRef.current = true;
        return;
      }
      window.history.replaceState(null, "", current);
      // 后退的合理去处与「返回」按钮一致：图列表
      setPendingLeaveTo("/graphs");
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [canvas.dirty, pendingLeaveTo, saveMutation.isPending]);

  /**
   * 保存前的统一校验：名称非空 + 多 Loop 拦截（后端会因 graph_to_spec
   * 抛 ValueError 而裸 500）。校验失败返回 null 并就地显示错误，
   * 调用方决定是否要关掉对话框。
   */
  const validateBeforeSave = (): string | null => {
    const graphName = name.trim();
    if (!graphName) {
      setNameError("请先填写图名称");
      nameInputRef.current?.focus();
      return null;
    }
    const loopCount = canvas.countByKind("loop");
    if (loopCount > 1) {
      setValidationErrors([
        {
          field: "graph",
          message: `图里有 ${loopCount} 个 Loop 节点，当前版本只支持 1 个（多 Loop 需并行池）。请删到只剩一个再保存。`,
          severity: "error",
        },
      ]);
      toast.error("多 Loop 图暂时存不下来，见右侧「校验问题」");
      return null;
    }
    return graphName;
  };

  // 名称改用页面内校验 + 聚焦输入框，不再 window.prompt
  const handleSave = () => {
    const graphName = validateBeforeSave();
    if (graphName === null) return;
    setNameError(null);
    saveMutation.mutate({ name: graphName, graph: serializeGraph(), description });
  };

  // 对话框动作：先清空挂起，避免下一次既可跳又显示"还在考虑"
  const confirmDiscardAndLeave = () => {
    if (pendingLeaveTo === null) return;
    userLeftRef.current = true;
    leaveTargetRef.current = null;
    // 这里已是"彻底离开"：用 replace 覆盖当前条目，避免后退又落回
    // 一个已失活图的编辑器页（旧 id 404）
    navigate(pendingLeaveTo, { replace: true });
    setPendingLeaveTo(null);
  };

  const cancelLeave = () => {
    // 只关对话框、清掉残留目标。不能置 userLeftRef —— 那是"已确认离开"，
    // "留在本页"只是取消：用户继续编辑再保存时，onSuccess 还要正常走
    // 版本化跳转，否则会滞留旧 id 页面、二次保存 404
    leaveTargetRef.current = null;
    setPendingLeaveTo(null);
  };

  const confirmSaveAndLeave = () => {
    if (pendingLeaveTo === null) return;
    // 校验失败时关掉对话框、把错误留在页面上 —— 用户先解决再走
    const graphName = validateBeforeSave();
    if (graphName === null) {
      setPendingLeaveTo(null);
      setNameError("请先填写图名称");
      return;
    }
    // 只记目标不立即跳：保存成功后的版本跳转（/graphs/{newId}）由定义处
    // onSuccess 统一裁决，避免两边互跳
    leaveTargetRef.current = pendingLeaveTo;
    setPendingLeaveTo(null);
    saveMutation.mutate({ name: graphName, graph: serializeGraph(), description });
  };

  const handleBack = () => {
    if (canvas.dirty) {
      setPendingLeaveTo("/graphs");
      return;
    }
    navigate("/graphs");
  };

  if (!isNew && isPending) {
    return (
      <div className="page">
        <TableSkeleton cols={4} rows={6} />
      </div>
    );
  }

  const error = validateMutation.error ?? saveMutation.error;

  return (
    <div className="page graph-editor-page">
      <GraphToolbar
        name={name}
        description={description}
        nameError={nameError}
        dirty={canvas.dirty}
        isNew={isNew}
        isValidating={validateMutation.isPending}
        isSaving={saveMutation.isPending}
        nameInputRef={nameInputRef}
        onNameChange={(value) => {
          setName(value);
          // 改名也是改动。不置脏的话「未保存」不出现、返回也不拦，改完直接走就丢了
          setDirty(true);
          if (nameError) setNameError(null);
        }}
        onDescriptionChange={(value) => {
          setDescription(value);
          setDirty(true);
        }}
        onValidate={() => validateMutation.mutate(serializeGraph())}
        onSave={handleSave}
        onBack={handleBack}
      />

      {nameError !== null && (
        <p className="form-error" role="alert">
          {nameError}
        </p>
      )}

      {error && <ErrorBox error={error as Error} />}

      <div className="graph-editor-body">
        <div className="graph-canvas-wrapper">
          <ReactFlow
            nodes={canvas.rfNodes}
            edges={canvas.rfEdges}
            onNodesChange={canvas.handleNodesChange}
            onEdgesChange={canvas.handleEdgesChange}
            onConnect={canvas.onConnect}
            onNodeClick={(_, node) => canvas.setSelectedNodeId(node.id)}
            nodeTypes={nodeTypes}
            fitView
            deleteKeyCode={["Backspace", "Delete"]}
            onlyRenderVisibleElements
            minZoom={0.2}
            maxZoom={2.5}
          >
            <Background variant={BackgroundVariant.Dots} />
            <Controls />
            <MiniMap
              pannable
              nodeColor={miniMapNodeColor}
              nodeStrokeColor={miniMapNodeStrokeColor}
            />
          </ReactFlow>
        </div>

        <div className="graph-sidebar">
          <NodeToolbar onAddNode={canvas.addNode} />
          <ValidationPanel
            issues={validationErrors}
            knownNodeIds={canvas.knownNodeIds}
            onFocusNode={canvas.focusNode}
          />
          <NodeConfigPanel
            selectedNode={canvas.selectedNode}
            onApplyParam={canvas.applyNodeParam}
            onDeselect={() => canvas.setSelectedNodeId(null)}
            onDelete={canvas.deleteNode}
          />
        </div>
      </div>

      {pendingLeaveTo !== null && (
        <UnsavedChangesDialog
          summary={`「${name.trim() || "未命名"}」有未保存的修改：${canvas.rfNodes.length} 个节点、连线、名称或描述。`}
          saving={saveMutation.isPending}
          onSave={confirmSaveAndLeave}
          onDiscard={confirmDiscardAndLeave}
          onCancel={cancelLeave}
        />
      )}
    </div>
  );
}

export function GraphEditorPage() {
  return (
    <ReactFlowProvider>
      <GraphEditorInner />
    </ReactFlowProvider>
  );
}
