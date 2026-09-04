/**
 * 右侧节点配置面板 —— 只读元信息 + 可编辑参数。
 *
 * 参数值类型不定（字符串/数字/嵌套对象），所以按 JSON 编辑，
 * 但字符串参数按原样编辑，避免给人凭空多出一对引号。
 */

import { type Node as RFNode } from "@xyflow/react";
import { Trash2 } from "lucide-react";
import { useId, useState } from "react";

import { NODE_KIND_LABELS } from "@/api/types";
import type { AriadneNodeData } from "@/lib/graph-transform";

/**
 * 单个参数编辑器。
 *
 * 输入走本地草稿 text state，只在 blur 时解析：
 * 边打字边 JSON.parse + JSON.stringify 回显会让光标乱跳，
 * 且中间的不合法状态会被当字符串存进图里。解析失败就地报错，不丢输入。
 */
function ParamField({
  paramKey,
  value,
  onApply,
}: {
  paramKey: string;
  value: unknown;
  onApply: (key: string, value: unknown) => void;
}) {
  const fieldId = useId();
  // 原值是字符串就按原样编辑，否则按 JSON 编辑 —— 保住参数类型
  const isRawString = typeof value === "string";
  const [draft, setDraft] = useState(() =>
    isRawString ? value : (JSON.stringify(value, null, 2) ?? ""),
  );
  const [parseError, setParseError] = useState<string | null>(null);

  const apply = () => {
    if (isRawString) {
      setParseError(null);
      onApply(paramKey, draft);
      return;
    }
    try {
      const parsed: unknown = JSON.parse(draft);
      setParseError(null);
      onApply(paramKey, parsed);
    } catch (err) {
      setParseError(err instanceof Error ? err.message : "JSON 格式不合法");
    }
  };

  return (
    <div className="config-field">
      <label htmlFor={fieldId}>
        {paramKey}
        {!isRawString && <span className="field-hint"> JSON</span>}
      </label>
      <textarea
        id={fieldId}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={apply}
        aria-invalid={parseError !== null}
        rows={2}
      />
      {parseError !== null && (
        <p className="form-error" role="alert">
          {parseError}（未应用，修好后离开输入框即生效）
        </p>
      )}
    </div>
  );
}

export function NodeConfigPanel({
  selectedNode,
  onApplyParam,
  onDeselect,
  onDelete,
}: {
  selectedNode: RFNode | null;
  onApplyParam: (nodeId: string, key: string, value: unknown) => void;
  onDeselect: () => void;
  onDelete: (nodeId: string) => void;
}) {
  if (!selectedNode) {
    return (
      <div className="config-panel">
        <h3>节点配置</h3>
        <p className="hint">选择一个节点查看/编辑参数</p>
      </div>
    );
  }

  // key 让切换节点时重建表单，草稿状态自动清空
  return (
    <NodeConfigForm
      key={selectedNode.id}
      node={selectedNode}
      onApplyParam={onApplyParam}
      onDeselect={onDeselect}
      onDelete={onDelete}
    />
  );
}

function NodeConfigForm({
  node,
  onApplyParam,
  onDeselect,
  onDelete,
}: {
  node: RFNode;
  onApplyParam: (nodeId: string, key: string, value: unknown) => void;
  onDeselect: () => void;
  onDelete: (nodeId: string) => void;
}) {
  const baseId = useId();
  const data = node.data as unknown as AriadneNodeData;
  const paramEntries = Object.entries(data.params ?? {});
  const applyParam = (key: string, value: unknown) => onApplyParam(node.id, key, value);

  return (
    <div className="config-panel">
      <div className="config-panel-header">
        <h3>{NODE_KIND_LABELS[data.kind] ?? data.kind}</h3>
        <div className="page-actions">
          <button
            type="button"
            className="btn btn-sm btn-danger"
            aria-label={`删除节点 ${data.label}`}
            onClick={() => {
              if (window.confirm(`删除节点 ${data.label}？相连的边也会一起删除。`)) {
                onDelete(node.id);
              }
            }}
          >
            <Trash2 size={13} aria-hidden />
            删除
          </button>
          <button type="button" className="btn btn-sm" onClick={onDeselect}>
            关闭
          </button>
        </div>
      </div>

      <div className="config-field">
        <label htmlFor={`${baseId}-id`}>节点 ID</label>
        <input id={`${baseId}-id`} type="text" value={data.label} readOnly />
      </div>
      <div className="config-field">
        <label htmlFor={`${baseId}-kind`}>类型</label>
        <input id={`${baseId}-kind`} type="text" value={data.kind} readOnly />
      </div>
      <div className="config-field">
        <label htmlFor={`${baseId}-inputs`}>输入端口</label>
        <input
          id={`${baseId}-inputs`}
          type="text"
          value={`${data.inputs?.length ?? 0} 个`}
          readOnly
        />
      </div>
      <div className="config-field">
        <label htmlFor={`${baseId}-outputs`}>输出端口</label>
        <input
          id={`${baseId}-outputs`}
          type="text"
          value={`${data.outputs?.length ?? 0} 个`}
          readOnly
        />
      </div>

      <hr />
      <h4>参数</h4>
      {paramEntries.length === 0 ? (
        <p className="hint">无参数</p>
      ) : (
        paramEntries.map(([key, value]) => (
          <ParamField key={key} paramKey={key} value={value} onApply={applyParam} />
        ))
      )}
    </div>
  );
}
