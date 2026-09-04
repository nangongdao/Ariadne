/**
 * 画布上的自定义节点 —— 按 kind 着色，顶部/底部各挂一排连接桩。
 *
 * 端口在边上按数量均分横向位置，所以 left 必须内联算；
 * 没有 Handle 就无法拖拽连线，不能省。
 */

import { type Node as RFNode, Handle, Position } from "@xyflow/react";
import { memo } from "react";

import { NODE_KIND_COLORS, NODE_KIND_LABELS } from "@/api/types";
import type { AriadneNodeData } from "@/lib/graph-transform";

const FALLBACK_COLOR = "#6b7280";

/** 端口沿边均分：第 i 个（共 n 个）落在 (i+1)/(n+1) 处。 */
function portOffset(index: number, total: number): string {
  return `${((index + 1) * 100) / (total + 1)}%`;
}

export const AriadneNode = memo(function AriadneNode({
  data,
  selected,
}: {
  data: RFNode["data"];
  selected?: boolean;
}) {
  const nodeData = data as unknown as AriadneNodeData;
  const color = NODE_KIND_COLORS[nodeData.kind] ?? FALLBACK_COLOR;
  const label = NODE_KIND_LABELS[nodeData.kind] ?? nodeData.kind;
  const inputs = nodeData.inputs ?? [];
  const outputs = nodeData.outputs ?? [];
  const params = nodeData.params ?? {};

  return (
    <div
      className="graph-node"
      style={{
        borderColor: color,
        boxShadow: selected
          ? `0 0 0 2px rgb(45 212 191 / 55%), 0 0 18px ${color}55`
          : "0 4px 14px rgb(2 6 14 / 45%)",
      }}
    >
      {inputs.map((port, i) => (
        <Handle
          key={`in-${port.name}`}
          id={port.name}
          type="target"
          position={Position.Top}
          className="graph-port"
          style={{ background: color, left: portOffset(i, inputs.length) }}
          title={`${port.name} (${port.kind})`}
        />
      ))}

      <div className="graph-node-head">
        <span className="graph-node-kind" style={{ background: `${color}22`, color }}>
          {label}
        </span>
        <span className="graph-node-id" title={nodeData.label}>
          {nodeData.label}
        </span>
      </div>

      {Object.keys(params).length > 0 && (
        <div className="graph-node-params">
          {Object.entries(params)
            .slice(0, 2)
            .map(([k, v]) => (
              <span key={k} className="graph-node-param">
                {k}={String(v).slice(0, 18)}
              </span>
            ))}
        </div>
      )}

      {outputs.map((port, i) => (
        <Handle
          key={`out-${port.name}`}
          id={port.name}
          type="source"
          position={Position.Bottom}
          className="graph-port"
          style={{ background: color, left: portOffset(i, outputs.length) }}
          title={`${port.name} (${port.kind})`}
        />
      ))}
    </div>
  );
});

export const nodeTypes = { ariadneNode: AriadneNode };

// MiniMap 回调提到模块级：内联箭头函数每次渲染都换引用，会让 MiniMap 白重绘
export function miniMapNodeColor(node: RFNode): string {
  return NODE_KIND_COLORS[(node.data as unknown as AriadneNodeData).kind] ?? FALLBACK_COLOR;
}

export function miniMapNodeStrokeColor(): string {
  return "transparent";
}
