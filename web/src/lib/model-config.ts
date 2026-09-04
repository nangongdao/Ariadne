/**
 * 模型配置的纯逻辑：provider 元数据、草稿 ↔ 载荷转换、密钥掩码、复制。
 *
 * 与 React 无关，页面与组件共用。api_key 只在「创建」时明文经过这里，
 * 任何函数都不得把它写进日志、存储或 URL。
 */

import type { ModelConfig, ModelProvider } from "@/api/types";
import { ApiError } from "@/api/client";

export interface ProviderMeta {
  value: ModelProvider;
  label: string;
  hint: string;
}

export const PROVIDERS: readonly ProviderMeta[] = [
  { value: "openai", label: "OpenAI", hint: "官方 Chat Completions" },
  { value: "anthropic", label: "Anthropic", hint: "Claude Messages API" },
  {
    value: "openai_compatible",
    label: "OpenAI 兼容",
    hint: "Ollama / vLLM / OneAPI 等网关",
  },
] as const;

export const PROVIDER_LABEL: Record<ModelProvider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  openai_compatible: "OpenAI 兼容",
};

export const PROVIDER_DEFAULT_BASE: Record<ModelProvider, string> = {
  anthropic: "https://api.anthropic.com",
  openai: "https://api.openai.com",
  openai_compatible: "",
};

export interface DraftConfig {
  name: string;
  provider: ModelProvider;
  model: string;
  api_key: string;
  base_url: string;
  degraded_model: string;
  is_default: boolean;
}

export const EMPTY_DRAFT: DraftConfig = {
  name: "",
  provider: "openai",
  model: "",
  api_key: "",
  base_url: "",
  degraded_model: "",
  is_default: false,
};

const DEFAULT_BASES: readonly string[] = Object.values(PROVIDER_DEFAULT_BASE);

/**
 * 切换 provider 时的 base_url 取值。
 *
 * 用户手填过的地址必须留着 —— 否则从「OpenAI 兼容」误点一下官方
 * provider，本地网关地址就没了，且无从找回。
 */
export function nextBaseUrl(current: string, provider: ModelProvider): string {
  const untouched = current === "" || DEFAULT_BASES.includes(current);
  return untouched ? PROVIDER_DEFAULT_BASE[provider] : current;
}

/** 已有配置 → 编辑草稿。api_key 一律留空：后端只回前缀，没有明文可回填。 */
export function draftFromConfig(cfg: ModelConfig): DraftConfig {
  return {
    name: cfg.name,
    provider: cfg.provider,
    model: cfg.model,
    api_key: "",
    base_url: cfg.base_url,
    degraded_model: cfg.degraded_model,
    is_default: cfg.is_default,
  };
}

export interface CreatePayload {
  name: string;
  provider: ModelProvider;
  model: string;
  api_key?: string;
  base_url?: string;
  degraded_model?: string;
  is_default?: boolean;
}

function trimmed(draft: DraftConfig) {
  return {
    name: draft.name.trim(),
    provider: draft.provider,
    model: draft.model.trim(),
    base_url: draft.base_url.trim(),
    degraded_model: draft.degraded_model.trim(),
    is_default: draft.is_default,
  };
}

export function toCreatePayload(draft: DraftConfig): CreatePayload {
  const base = trimmed(draft);
  const key = draft.api_key.trim();
  // exactOptionalPropertyTypes：可选字段宁可整个不给，也不传 undefined
  return key ? { ...base, api_key: key } : base;
}

/** 编辑载荷。api_key 留空表示「不改」，必须整个字段不出现在请求里。 */
export function toUpdatePayload(draft: DraftConfig): CreatePayload {
  return toCreatePayload(draft);
}

/**
 * 密钥留空时到底会发生什么。
 *
 * 空 api_key 是合法配置而不是坏配置：resolver.py:80 在存的 key 为空时回退
 * 服务端环境变量 ARIADNE_LLM_API_KEY，也就是「模型和 base_url 用我的，
 * key 用环境里的」。所以这里不拦提交，只把语义说出来 ——
 * 卡片上光写「（未设置）」，用户没法区分这是坏配置还是有意继承。
 *
 * 前端拿不到服务端环境变量的值，所以只说清回退链，不断言它一定能用。
 */
export function emptyKeyMeaning(provider: ModelProvider): string {
  if (provider === "openai_compatible") {
    return "留空则用服务端环境变量 ARIADNE_LLM_API_KEY；本地网关（Ollama / vLLM）通常不需要密钥，留空即可。";
  }
  return "留空则用服务端环境变量 ARIADNE_LLM_API_KEY；该变量也没配时，调用会因缺密钥失败。";
}

/** 密钥展示值。永远只有前缀，reveal 也不会多给一个字符。 */
export function maskedKey(prefix: string, reveal: boolean): string {
  if (!prefix) return "（未设置）";
  return reveal ? prefix : `${prefix}••••`;
}

export function errorMsg(err: unknown): string {
  if (err instanceof ApiError) return err.problem?.detail ?? err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * 写剪贴板。
 *
 * 非安全源（http 裸 IP）下 navigator.clipboard 直接不存在，
 * 授权被拒时 writeText 会 reject —— 两种都得让调用方知道失败，
 * 静默失败会让用户以为复制成功。
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
