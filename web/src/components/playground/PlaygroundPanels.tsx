/**
 * Playground 的三块展示单元：配置编辑器、对比结果卡、原始输出对照。
 */

import { Trash2 } from "lucide-react";
import { memo, useId } from "react";

import type { ConfigResult, LLMConfig, ReproduceResponse } from "@/api/types";
import { formatCost, formatTokens } from "@/lib/format";

/** 后端 system_prompt 的 max_length（playground.py:40），与 prompt 的 50000 不同 */
const MAX_SYSTEM_PROMPT = 10_000;
/** Playground 的 model 上限（playground.py:37/98）。模型配置页是 200，别混用 */
const MAX_MODEL_NAME = 100;

interface ConfigEditorProps {
  config: LLMConfig;
  index: number;
  modelListId: string;
  onChange: (index: number, config: LLMConfig) => void;
  onRemove: (index: number) => void;
}

export const ConfigEditor = memo(function ConfigEditor({
  config,
  index,
  modelListId,
  onChange,
  onRemove,
}: ConfigEditorProps) {
  const baseId = useId();

  return (
    <div className="config-editor">
      <div className="config-editor-header">
        <span className="config-label">配置 {index + 1}</span>
        {index >= 1 && (
          <button
            type="button"
            className="btn btn-sm"
            aria-label={`移除配置 ${index + 1}`}
            onClick={() => onRemove(index)}
          >
            <Trash2 size={12} aria-hidden />
            移除
          </button>
        )}
      </div>

      <div className="config-field">
        <label htmlFor={`${baseId}-model`}>模型</label>
        {/* 候选来自用户配置的模型，但仍可自由输入：从 span 复现时
            模型未必在候选里，select 会把它悄悄改掉 */}
        <input
          id={`${baseId}-model`}
          type="text"
          list={modelListId}
          value={config.model}
          onChange={(e) => onChange(index, { ...config, model: e.target.value })}
          /* Playground 的 model 上限是 100，模型配置页那边是 200 —— 两个端点不同，
             照 200 放行会让「模型页能存、这里却 422」 */
          maxLength={MAX_MODEL_NAME}
          placeholder="模型名"
        />
      </div>

      <div className="config-field">
        <label htmlFor={`${baseId}-temp`}>
          Temperature <span className="field-hint">{config.temperature.toFixed(1)}</span>
        </label>
        <input
          id={`${baseId}-temp`}
          type="range"
          min={0}
          max={2}
          step={0.1}
          value={config.temperature}
          onChange={(e) =>
            onChange(index, { ...config, temperature: Number(e.target.value) })
          }
        />
      </div>

      <div className="config-field">
        <label htmlFor={`${baseId}-max`}>Max Tokens</label>
        <input
          id={`${baseId}-max`}
          type="number"
          min={1}
          max={200000}
          value={config.max_tokens}
          onChange={(e) => {
            // 清空输入框时 Number("") 是 0，parseInt 是 NaN，都会被送进请求体
            const next = Number.parseInt(e.target.value, 10);
            onChange(index, {
              ...config,
              max_tokens: Number.isFinite(next) ? next : 1,
            });
          }}
        />
      </div>

      <div className="config-field">
        <label htmlFor={`${baseId}-sys`}>System Prompt</label>
        {/* 上限是 10000 而非 prompt 的 50000（playground.py:40）。
            不设 maxLength 的话，粘一段长 system prompt 会在提交时 422，
            而错误信息落在页顶，输入框这边看不出是哪个字段超了 */}
        <textarea
          id={`${baseId}-sys`}
          value={config.system_prompt}
          onChange={(e) => onChange(index, { ...config, system_prompt: e.target.value })}
          rows={2}
          maxLength={MAX_SYSTEM_PROMPT}
          placeholder="（可选）"
        />
      </div>
    </div>
  );
});

export const ResultCard = memo(function ResultCard({ result }: { result: ConfigResult }) {
  return (
    <div className="result-card">
      <div className="result-card-header">
        <span className="result-model mono">{result.config.model}</span>
        <span className="result-temp">T={result.config.temperature.toFixed(1)}</span>
        {result.cost_usd > 0 && (
          <span className="result-cost">{formatCost(result.cost_usd)}</span>
        )}
      </div>
      {result.error ? (
        <p className="result-error" role="alert">
          {result.error}
        </p>
      ) : (
        <pre className="result-output">
          {result.output || "（此配置未返回输出）"}
        </pre>
      )}
      {(result.input_tokens > 0 || result.output_tokens > 0) && (
        <div className="result-tokens">
          输入 {formatTokens(result.input_tokens)} · 输出{" "}
          {formatTokens(result.output_tokens)}
        </div>
      )}
    </div>
  );
});

/** 复现来源的原始输出。复现的意义就是拿新结果和它比，不展示等于白取。 */
export function OriginalOutput({ source }: { source: ReproduceResponse }) {
  return (
    <div className="playground-section">
      <div className="section-header">
        <h3>原始输出（复现来源）</h3>
        <span className="result-cost">{formatCost(source.original_cost_usd)}</span>
      </div>
      <p className="hint mono">
        trace {source.trace_id.slice(0, 8)}… · span {source.span_id.slice(0, 8)}…
      </p>
      <pre className="result-output">{source.original_output || "（原始输出为空）"}</pre>
    </div>
  );
}
