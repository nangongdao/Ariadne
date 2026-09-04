/**
 * 侧栏的两个轻面板：添加节点工具栏、校验问题列表。
 */

import { Crosshair, Plus } from "lucide-react";

import { NODE_KIND_COLORS, NODE_KIND_LABELS } from "@/api/types";
import type { GraphValidationError } from "@/api/types";
import { AVAILABLE_KINDS } from "@/lib/graph-transform";

export function NodeToolbar({ onAddNode }: { onAddNode: (kind: string) => void }) {
  return (
    <div className="node-toolbar">
      <h3>添加节点</h3>
      {AVAILABLE_KINDS.map((kind) => (
        <button
          key={kind}
          type="button"
          className="btn btn-sm"
          style={{ borderLeft: `4px solid ${NODE_KIND_COLORS[kind] ?? "#6b7280"}` }}
          onClick={() => onAddNode(kind)}
        >
          <Plus size={12} aria-hidden />
          {NODE_KIND_LABELS[kind] ?? kind}
        </button>
      ))}
    </div>
  );
}

/**
 * 从后端校验错误的 field 里解析出节点 id。
 *
 * 后端格式（graph_module/validate.py）：
 *   node:{id} / node:{id}:params:{p} / node:{id}:goal
 *   edge:{src}->{tgt} / edge:{node}.{port}
 *   subgraph[{id}].{field} / graph
 */
function nodeIdFromField(field: string): string | null {
  if (field.startsWith("node:")) {
    return field.slice(5).split(":")[0] ?? null;
  }
  if (field.startsWith("edge:")) {
    const source = field.slice(5).split("->")[0] ?? "";
    return source.split(".")[0] ?? null;
  }
  const subgraph = /^subgraph\[([^\]]+)\]/.exec(field);
  if (subgraph) return subgraph[1] ?? null;
  return null;
}

/** 校验问题列表：点某条就选中并聚焦对应节点，替代原来的 window.alert */
export function ValidationPanel({
  issues,
  knownNodeIds,
  onFocusNode,
}: {
  issues: GraphValidationError[];
  knownNodeIds: Set<string>;
  onFocusNode: (nodeId: string) => void;
}) {
  if (issues.length === 0) return null;

  return (
    <div className="config-panel">
      <div className="config-panel-header">
        <h3>校验问题（{issues.length}）</h3>
      </div>
      <ul className="violation-list">
        {issues.map((issue, i) => {
          const nodeId = nodeIdFromField(issue.field);
          const focusable = nodeId !== null && knownNodeIds.has(nodeId);
          const text = `[${issue.severity}] ${issue.field}: ${issue.message}`;
          return (
            <li key={`${issue.field}-${i}`}>
              {focusable ? (
                // 整行可点：点击选中并把画布聚焦到出错节点
                <button
                  type="button"
                  className="btn validation-jump"
                  aria-label={`定位到节点 ${nodeId}：${issue.message}`}
                  onClick={() => onFocusNode(nodeId)}
                >
                  <Crosshair size={12} aria-hidden />
                  {text}
                </button>
              ) : (
                <span className={issue.severity === "error" ? "error-text" : "hint"}>
                  {text}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
