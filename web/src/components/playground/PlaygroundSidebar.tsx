/**
 * Playground 侧栏 —— 「从 Span 复现」与「固化为 spec.yaml」两块。
 *
 * 与主区（prompt + 配置对比）拆开：这两块只依赖自己的输入，
 * 主区改 prompt 不该让它们重渲染。
 */

import { AlertTriangle, Copy, Rows3 } from "lucide-react";
import { useId } from "react";

import type { LLMConfig } from "@/api/types";
import type { SpecInspection } from "@/lib/spec-guard";

interface ReproducePanelProps {
  traceId: string;
  spanId: string;
  isPending: boolean;
  onTraceIdChange: (value: string) => void;
  onSpanIdChange: (value: string) => void;
  onReproduce: () => void;
}

export function ReproducePanel({
  traceId,
  spanId,
  isPending,
  onTraceIdChange,
  onSpanIdChange,
  onReproduce,
}: ReproducePanelProps) {
  const traceInputId = useId();
  const spanInputId = useId();

  return (
    <div className="playground-section">
      <h3>从 Span 复现</h3>
      <p className="hint">
        也可以在 Trace 详情里点某个 LLM span 的「复现」，两个 id 会自动带过来。
      </p>
      <div className="config-field">
        <label htmlFor={traceInputId}>Trace ID</label>
        <input
          id={traceInputId}
          type="text"
          value={traceId}
          onChange={(e) => onTraceIdChange(e.target.value.trim())}
          placeholder="trace-uuid"
        />
      </div>
      <div className="config-field">
        <label htmlFor={spanInputId}>Span ID</label>
        <input
          id={spanInputId}
          type="text"
          value={spanId}
          onChange={(e) => onSpanIdChange(e.target.value.trim())}
          placeholder="span-uuid"
        />
      </div>
      <button
        type="button"
        className="btn"
        onClick={onReproduce}
        disabled={!traceId || !spanId || isPending}
      >
        <Rows3 size={13} aria-hidden />
        {isPending ? "提取中…" : "提取并复现"}
      </button>
    </div>
  );
}

interface FreezePanelProps {
  task: string;
  drafts: ReadonlyArray<{ id: number; config: LLMConfig }>;
  targetId: number | undefined;
  targetIndex: number;
  targetModel: string;
  canFreeze: boolean;
  isPending: boolean;
  specYaml: string;
  specCheck: SpecInspection | null;
  onTaskChange: (value: string) => void;
  onTargetChange: (id: number) => void;
  onFreeze: () => void;
  onCopySpec: () => void;
}

export function FreezePanel({
  task,
  drafts,
  targetId,
  targetIndex,
  targetModel,
  canFreeze,
  isPending,
  specYaml,
  specCheck,
  onTaskChange,
  onTargetChange,
  onFreeze,
  onCopySpec,
}: FreezePanelProps) {
  const taskInputId = useId();
  const freezeSelectId = useId();

  return (
    <div className="playground-section">
      <h3>固化为 spec.yaml</h3>
      <div className="config-field">
        <label htmlFor={taskInputId}>Task 描述</label>
        <input
          id={taskInputId}
          type="text"
          value={task}
          onChange={(e) => onTaskChange(e.target.value)}
          maxLength={2000}
          placeholder="（留空则取 prompt 前 100 字）"
        />
      </div>

      {drafts.length > 1 && (
        <div className="config-field">
          <label htmlFor={freezeSelectId}>固化哪一组</label>
          <select
            id={freezeSelectId}
            value={targetId ?? ""}
            onChange={(e) => onTargetChange(Number(e.target.value))}
          >
            {drafts.map((draft, i) => (
              <option key={draft.id} value={draft.id}>
                配置 {i + 1}
                {draft.config.model ? ` · ${draft.config.model}` : "（未填模型）"}
              </option>
            ))}
          </select>
        </div>
      )}

      <button
        type="button"
        className="btn btn-primary"
        onClick={onFreeze}
        disabled={isPending || !canFreeze}
      >
        {isPending ? "生成中…" : "固化为 Spec"}
      </button>
      {!canFreeze && (
        <p className="hint">
          {!targetModel.trim()
            ? `配置 ${targetIndex + 1} 还没填模型名，spec 里的 model 不能为空`
            : "Task 描述与 Prompt 至少填一个"}
        </p>
      )}

      {specYaml && (
        <div className="spec-output">
          {/* 放在复制按钮之前：警告写在 YAML 下面等于没写，
              用户的手已经在复制按钮上了 */}
          {specCheck?.alwaysPasses && (
            <div className="spec-warning" role="alert">
              <AlertTriangle size={13} aria-hidden />
              <div>
                <strong>这份 spec 不会校验任何输出</strong>
                <p>
                  {specCheck.assertionCount === 0
                    ? "里面没有断言"
                    : "里面只有占位断言（匹配一切、且不阻塞）"}
                  ，直接放进 CI 会永远通过 —— 它不能证明模型没坏。用之前把{" "}
                  <code>assertions</code> 换成真实断言。
                </p>
                {specCheck.flaws.length > 0 && (
                  <ul className="spec-warning-list">
                    {specCheck.flaws.map((flaw) => (
                      <li key={flaw.id}>
                        <code>{flaw.id}</code>：{flaw.reason}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
          <div className="spec-output-header">
            <span>spec.yaml</span>
            <button
              type="button"
              className="btn btn-sm"
              onClick={onCopySpec}
              aria-label="复制 spec.yaml 到剪贴板"
            >
              <Copy size={12} aria-hidden />
              复制
            </button>
          </div>
          <pre className="spec-yaml">{specYaml}</pre>
        </div>
      )}
    </div>
  );
}
