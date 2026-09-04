/**
 * 模型配置卡片网格 + 空状态。
 *
 * 忙态按行传入（deletingId / defaultingId），不是整表一个 isPending ——
 * 否则一次保存会让所有卡片同时变灰，看不出到底哪一行在动。
 */

import { Box, Eye, EyeOff, KeyRound, Plus, Server, Sparkles, Star, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { ModelConfig } from "@/api/types";
import { CopyButton, DeleteConfirm } from "@/components/model-config/ModelConfigControls";
import { emptyKeyMeaning, maskedKey, PROVIDER_LABEL } from "@/lib/model-config";

export function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="models-empty">
      <Sparkles size={36} aria-hidden />
      <h3>还没有模型配置</h3>
      <p>
        在这里添加你的 LLM 端点 —— 自定义名称、模型、API Key 和 Base URL。
        支持官方 OpenAI / Anthropic，也支持 Ollama、vLLM 等 OpenAI 兼容网关。
      </p>
      <button type="button" className="btn btn-primary btn-tall" onClick={onCreate}>
        <Plus size={14} aria-hidden />
        添加第一个模型
      </button>
    </div>
  );
}

export function ModelCardGrid({
  configs,
  deletingId,
  defaultingId,
  onEdit,
  onDelete,
  onSetDefault,
}: {
  configs: ModelConfig[];
  deletingId: string | null;
  defaultingId: string | null;
  onEdit: (cfg: ModelConfig) => void;
  onDelete: (id: string) => void;
  onSetDefault: (id: string) => void;
}) {
  return (
    <ul className="model-grid">
      {configs.map((cfg) => (
        <li key={cfg.id}>
          <ModelCard
            cfg={cfg}
            deleting={deletingId === cfg.id}
            settingDefault={defaultingId === cfg.id}
            onEdit={() => onEdit(cfg)}
            onDelete={() => onDelete(cfg.id)}
            onSetDefault={() => onSetDefault(cfg.id)}
          />
        </li>
      ))}
    </ul>
  );
}

function ModelCard({
  cfg,
  deleting,
  settingDefault,
  onEdit,
  onDelete,
  onSetDefault,
}: {
  cfg: ModelConfig;
  deleting: boolean;
  settingDefault: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onSetDefault: () => void;
}) {
  const [reveal, setReveal] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const deleteBtnRef = useRef<HTMLButtonElement>(null);
  // 取消确认后确认区整块卸载，焦点会掉到 body，得送回「删除」按钮
  const refocusRef = useRef(false);

  useEffect(() => {
    if (confirming || !refocusRef.current) return;
    refocusRef.current = false;
    deleteBtnRef.current?.focus();
  }, [confirming]);

  return (
    <div className={`model-card${cfg.is_default ? " is-default" : ""}`}>
      <header className="model-card-head">
        <div className="model-card-title">
          <span className="model-name">{cfg.name}</span>
          {cfg.is_default && (
            <span className="badge-default" title="项目默认">
              <Star size={11} aria-hidden fill="currentColor" />
              默认
            </span>
          )}
        </div>
        <span className={`model-provider model-provider-${cfg.provider}`}>
          {PROVIDER_LABEL[cfg.provider]}
        </span>
      </header>

      <dl className="model-card-fields">
        <div className="model-field">
          <dt>
            <Box size={12} aria-hidden />
            模型
          </dt>
          <dd className="mono">{cfg.model}</dd>
        </div>
        <div className="model-field">
          <dt>
            <KeyRound size={12} aria-hidden />
            API Key
          </dt>
          <dd>
            {/* 后端只回前缀，reveal 也不可能拿到明文 */}
            <span className="mono">{maskedKey(cfg.api_key_prefix, reveal)}</span>
            {/* 没存 key 不等于配置坏了：服务端会回退环境变量。
                不标出来的话「（未设置）」看着就是个待修的错误 */}
            {!cfg.api_key_prefix && (
              <span className="tag tag-sm" title={emptyKeyMeaning(cfg.provider)}>
                用环境变量
              </span>
            )}
            {cfg.api_key_prefix && (
              <button
                type="button"
                className="btn-icon"
                onClick={() => setReveal((v) => !v)}
                aria-label={reveal ? "隐藏密钥前缀" : "显示密钥前缀"}
                title={reveal ? "隐藏" : "显示"}
              >
                {reveal ? <EyeOff size={12} aria-hidden /> : <Eye size={12} aria-hidden />}
              </button>
            )}
          </dd>
        </div>
        <div className="model-field">
          <dt>
            <Server size={12} aria-hidden />
            Base URL
          </dt>
          <dd>
            {/* 文本单独一层做省略号，否则会把复制按钮一起裁掉 */}
            <span className="mono model-field-text">
              {cfg.base_url || "（官方端点）"}
            </span>
            {cfg.base_url && <CopyButton text={cfg.base_url} label="Base URL" />}
          </dd>
        </div>
        {cfg.degraded_model && (
          <div className="model-field">
            <dt>降级模型</dt>
            <dd className="mono">{cfg.degraded_model}</dd>
          </div>
        )}
      </dl>

      {confirming ? (
        <DeleteConfirm
          name={cfg.name}
          pending={deleting}
          onConfirm={onDelete}
          onCancel={() => {
            refocusRef.current = true;
            setConfirming(false);
          }}
        />
      ) : (
        <footer className="model-card-foot">
          {!cfg.is_default && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={onSetDefault}
              disabled={settingDefault}
              title="设为项目默认"
            >
              <Star size={13} aria-hidden />
              {settingDefault ? "设置中…" : "设为默认"}
            </button>
          )}
          <button type="button" className="btn btn-sm" onClick={onEdit}>
            编辑
          </button>
          <button
            ref={deleteBtnRef}
            type="button"
            className="btn btn-sm btn-danger"
            onClick={() => setConfirming(true)}
            disabled={deleting}
            aria-label={`删除配置 ${cfg.name}`}
          >
            <Trash2 size={13} aria-hidden />
            {deleting ? "删除中…" : "删除"}
          </button>
        </footer>
      )}
    </div>
  );
}
