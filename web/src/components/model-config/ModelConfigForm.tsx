/**
 * 新建/编辑模型配置表单。
 *
 * 受控组件：草稿状态由页面持有，这里只负责渲染与字段级校验提示。
 * 编辑态下 api_key 留空表示保留原值（后端语义），placeholder 里说清。
 */

import { X } from "lucide-react";
import type { FormEvent } from "react";
import { useId } from "react";

import type { DraftConfig } from "@/lib/model-config";
import { emptyKeyMeaning, nextBaseUrl } from "@/lib/model-config";
import { ProviderPicker } from "@/components/model-config/ModelConfigControls";

export function ModelConfigForm({
  draft,
  editing,
  submitting,
  error,
  onChange,
  onSubmit,
  onCancel,
}: {
  draft: DraftConfig;
  editing: boolean;
  submitting: boolean;
  error: string;
  onChange: (patch: Partial<DraftConfig>) => void;
  onSubmit: (e: FormEvent) => void;
  onCancel: () => void;
}) {
  const baseId = useId();
  const id = (field: string) => `${baseId}-${field}`;
  const showKeyHint = editing && !draft.api_key;
  const showEmptyKeyMeaning = !editing && !draft.api_key.trim();
  const needsBaseUrl = draft.provider === "openai_compatible";

  return (
    <form className="model-form" onSubmit={onSubmit}>
      <header className="model-form-header">
        <h2>{editing ? "编辑配置" : "新建模型配置"}</h2>
        <button type="button" className="btn-icon" onClick={onCancel} aria-label="关闭表单">
          <X size={16} aria-hidden />
        </button>
      </header>

      <div className="model-form-body">
        <div className="field">
          <label className="field-label" htmlFor={id("name")}>
            显示名称
          </label>
          <input
            id={id("name")}
            value={draft.name}
            onChange={(e) => onChange({ name: e.target.value })}
            placeholder="如：工作用 GPT-4o"
            required
            maxLength={200}
            autoFocus
          />
        </div>

        <div className="field">
          <span className="field-label" id={id("provider")}>
            Provider
          </span>
          <ProviderPicker
            value={draft.provider}
            labelId={id("provider")}
            onChange={(provider) =>
              onChange({ provider, base_url: nextBaseUrl(draft.base_url, provider) })
            }
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor={id("model")}>
            模型名
          </label>
          <input
            id={id("model")}
            value={draft.model}
            onChange={(e) => onChange({ model: e.target.value })}
            placeholder="gpt-4o / claude-sonnet-5 / llama3.1"
            required
            maxLength={200}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor={id("key")}>
            API Key
            {showKeyHint && <span className="field-hint">留空保留原值</span>}
          </label>
          <input
            id={id("key")}
            type="password"
            value={draft.api_key}
            aria-describedby={showEmptyKeyMeaning ? id("key-hint") : undefined}
            onChange={(e) => onChange({ api_key: e.target.value })}
            placeholder={editing ? "••••••（不改则留空）" : "sk-…"}
            autoComplete="off"
            spellCheck={false}
          />
          {/* 新建且没填 key 时才说：编辑态留空是「保留原值」，
              两种语义混在一句里会把人绕晕 */}
          {showEmptyKeyMeaning && (
            <p className="field-note" id={id("key-hint")}>
              {emptyKeyMeaning(draft.provider)}
            </p>
          )}
        </div>

        <div className="field">
          <label className="field-label" htmlFor={id("base")}>
            Base URL
            {needsBaseUrl && <span className="field-hint">兼容网关必填</span>}
          </label>
          <input
            id={id("base")}
            value={draft.base_url}
            onChange={(e) => onChange({ base_url: e.target.value })}
            placeholder={
              needsBaseUrl ? "http://localhost:11434/v1" : "留空用官方端点"
            }
            required={needsBaseUrl}
            spellCheck={false}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor={id("degraded")}>
            降级模型（可选）
          </label>
          <input
            id={id("degraded")}
            value={draft.degraded_model}
            onChange={(e) => onChange({ degraded_model: e.target.value })}
            placeholder="预算降级时用的便宜模型"
            maxLength={200}
          />
        </div>

        <div className="field-check">
          <input
            id={id("default")}
            type="checkbox"
            checked={draft.is_default}
            onChange={(e) => onChange({ is_default: e.target.checked })}
          />
          <label htmlFor={id("default")}>
            设为项目默认（Loop Worker 优先使用此配置）
          </label>
        </div>
      </div>

      {error && (
        <p className="model-form-error" role="alert">
          {error}
        </p>
      )}

      <footer className="model-form-footer">
        <button type="button" className="btn" onClick={onCancel}>
          取消
        </button>
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "保存中…" : editing ? "保存修改" : "创建配置"}
        </button>
      </footer>
    </form>
  );
}
