/**
 * API 客户端。
 *
 * API Key 从 localStorage 读取而非硬编码：M1 是单 project 静态 key 模型，
 * 用户在设置页填一次即可。M6 换成 JWT 时只需改这一层。
 */

import type {
  CompareResponse,
  CostSummary,
  DatasetDetail,
  DatasetNameEntry,
  DatasetVersionEntry,
  ExperimentSummary,
  FreezeSpecResponse,
  GraphResponse,
  GraphValidateResponse,
  HealthStatus,
  LLMConfig,
  LoopEvent,
  LoopIteration,
  LoopSummary,
  ModelConfig,
  ModelConfigCreateResponse,
  ModelConfigList,
  ModelProvider,
  PlaygroundCompareResponse,
  PlaygroundRunResponse,
  PipelineStats,
  ProblemDetail,
  ReproduceResponse,
  SpanFilters,
  SpanListItem,
  TraceDetail,
  TraceSummary,
  WorkflowGraphData,
} from "./types";

const KEY_STORAGE = "ariadne.apiKey";
const BASE_STORAGE = "ariadne.apiBase";
const DEFAULT_KEY = "ak_local_dev_key";
// 桌面端（Tauri）下 origin 是 tauri://localhost，相对路径解析不到后端——
// 默认直连本机后端，可在设置页覆盖。浏览器模式保持 origin 相对（走 vite 代理）。
const DESKTOP_DEFAULT_BASE = "http://127.0.0.1:8000";

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function getApiBase(): string {
  if (!isTauri()) return window.location.origin;
  return localStorage.getItem(BASE_STORAGE) ?? DESKTOP_DEFAULT_BASE;
}

export function setApiBase(base: string): void {
  localStorage.setItem(BASE_STORAGE, base.replace(/\/$/, ""));
}

export function getApiKey(): string {
  // ?? 只挡 null；空串是"存过但存了个空"，照发出去就是每页 401
  return localStorage.getItem(KEY_STORAGE)?.trim() || DEFAULT_KEY;
}

export function setApiKey(key: string): void {
  localStorage.setItem(KEY_STORAGE, key);
}

/** 携带 Problem Details 的错误，UI 可据 type 做差异化提示。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: ProblemDetail | null,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 认证失败需要引导用户去改 key，与其他错误的处置不同。 */
  get isAuthError(): boolean {
    return this.status === 401;
  }
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(new URL(path, getApiBase()), {
    method: "POST",
    headers: { "X-Ariadne-Key": getApiKey(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let problem: ProblemDetail | null = null;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      // 非 JSON 错误响应
    }
    throw new ApiError(
      response.status,
      problem,
      problem?.detail ?? `请求失败 (${response.status})`,
    );
  }
  return (await response.json()) as T;
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(new URL(path, getApiBase()), {
    method: "PUT",
    headers: { "X-Ariadne-Key": getApiKey(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let problem: ProblemDetail | null = null;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      // 非 JSON 错误响应
    }
    throw new ApiError(
      response.status,
      problem,
      problem?.detail ?? `请求失败 (${response.status})`,
    );
  }
  return (await response.json()) as T;
}

async function del<T>(path: string): Promise<T> {
  const response = await fetch(new URL(path, getApiBase()), {
    method: "DELETE",
    headers: { "X-Ariadne-Key": getApiKey() },
  });

  if (!response.ok) {
    let problem: ProblemDetail | null = null;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      // 非 JSON 错误响应
    }
    throw new ApiError(
      response.status,
      problem,
      problem?.detail ?? `请求失败 (${response.status})`,
    );
  }

  // DELETE 成功的常规形态是 204 No Content（空响应体）。
  // 对空体调 response.json() 必抛 SyntaxError，会把「服务端已删成功」
  // 误报成「删除失败」—— 故先排空体再解析。
  if (response.status === 204 || response.headers.get("Content-Length") === "0") {
    return undefined as unknown as T;
  }
  const text = await response.text();
  if (!text.trim()) return undefined as unknown as T;
  return JSON.parse(text) as T;
}

async function request<T>(path: string, params?: object): Promise<T> {
  const url = new URL(path, getApiBase());
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  const response = await fetch(url, {
    headers: { "X-Ariadne-Key": getApiKey() },
  });

  if (!response.ok) {
    let problem: ProblemDetail | null = null;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      // 非 JSON 错误响应（网关 502 等），保持 problem 为 null
    }
    throw new ApiError(
      response.status,
      problem,
      problem?.detail ?? `请求失败 (${response.status})`,
    );
  }

  return (await response.json()) as T;
}

export const api = {
  health: () => request<HealthStatus>("/health"),

  stats: () => request<PipelineStats>("/v1/stats"),

  listTraces: (opts: { limit?: number; before?: string; only_errors?: boolean } = {}) =>
    request<TraceSummary[]>("/v1/traces", opts),

  getTrace: (traceId: string) =>
    request<TraceDetail>(`/v1/traces/${encodeURIComponent(traceId)}`),

  listSpans: (filters: SpanFilters = {}) =>
    request<SpanListItem[]>("/v1/spans", filters),

  costs: (opts: { group_by?: string; hours?: number } = {}) =>
    request<CostSummary>("/v1/costs", opts),

  listDatasets: () => request<DatasetNameEntry[]>("/v1/datasets"),

  datasetVersions: (name: string) =>
    request<DatasetVersionEntry[]>(
      `/v1/datasets/${encodeURIComponent(name)}/versions`,
    ),

  getDataset: (name: string, version?: number) =>
    request<DatasetDetail>(
      `/v1/datasets/${encodeURIComponent(name)}`,
      version ? { version } : {},
    ),

  listExperiments: (opts: { limit?: number; dataset_ref?: string } = {}) =>
    request<ExperimentSummary[]>("/v1/experiments", opts),

  getExperiment: (id: string) =>
    request<ExperimentSummary>(`/v1/experiments/${encodeURIComponent(id)}`),

  compareExperiments: (body: {
    baseline_id: string;
    current_id: string;
    fail_if?: Array<Record<string, unknown>>;
    allow_dataset_mismatch?: boolean;
  }) => post<CompareResponse>("/v1/experiments/compare", body),

  // ---------- Loop（M3） ----------

  listLoops: (opts: { limit?: number; state?: string; mode?: string } = {}) =>
    request<LoopSummary[]>("/v1/loops", opts),

  getLoop: (id: string) =>
    request<LoopSummary>(`/v1/loops/${encodeURIComponent(id)}`),

  loopIterations: (id: string) =>
    request<{ iterations: LoopIteration[] }>(
      `/v1/loops/${encodeURIComponent(id)}/iterations`,
    ),

  createLoop: (body: {
    task: string;
    mode: string;
    assertions: Array<{
      id: string;
      kind: string;
      spec: Record<string, unknown>;
      blocking?: boolean;
      hint?: string;
    }>;
    budget?: {
      max_iterations: number;
      max_total_tokens: number;
      max_cost_usd: number;
    };
    /** 工作目录种子文件：相对路径 → 内容。command 断言验证的对象。 */
    workspace?: Record<string, string>;
  }) =>
    post<{ loop_id: string; state: string; stream_url: string; queued: boolean }>(
      "/v1/loops",
      body,
    ),

  cancelLoop: (id: string) =>
    post<LoopSummary>(`/v1/loops/${encodeURIComponent(id)}/cancel`, {}),

  approveLoop: (id: string, approved: boolean) =>
    post<LoopSummary>(
      `/v1/loops/${encodeURIComponent(id)}/approve`,
      { approved },
    ),

  resumeLoop: (id: string) =>
    post<LoopSummary>(`/v1/loops/${encodeURIComponent(id)}/resume`, {}),

  /** SSE 事件流：Worker 的状态变化实时推送。返回 AbortController 用于断开。 */
  streamLoopEvents: (
    id: string,
    onEvent: (event: LoopEvent) => void,
  ): AbortController => {
    const controller = new AbortController();
    const source = new EventSource(
      new URL(`/v1/loops/${encodeURIComponent(id)}/stream`, getApiBase()),
    );
    source.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as LoopEvent);
      } catch {
        // 非 JSON（如心跳）忽略
      }
    };
    // 页面停留时若连接断开（如代理超时），自动重连由 EventSource 内置处理
    source.onerror = () => {
      if (controller.signal.aborted) source.close();
    };
    controller.signal.addEventListener("abort", () => source.close());
    return controller;
  },

  // ---------- M5: Graphs ----------

  listGraphs: () => request<GraphResponse[]>("/v1/graphs"),

  getGraph: (id: string) =>
    request<GraphResponse>(`/v1/graphs/${encodeURIComponent(id)}`),

  createGraph: (data: { name: string; graph: Record<string, unknown>; description?: string }) =>
    post<GraphResponse>("/v1/graphs", data),

  updateGraph: (id: string, data: { name: string; graph: Record<string, unknown>; description?: string }) =>
    put<GraphResponse>(`/v1/graphs/${encodeURIComponent(id)}`, data),

  deleteGraph: (id: string) => del<void>(`/v1/graphs/${encodeURIComponent(id)}`),

  validateGraph: (graph: Record<string, unknown>) =>
    post<GraphValidateResponse>("/v1/graphs/validate", { graph }),

  graphToWorkflowData: (graph: Record<string, unknown>): WorkflowGraphData => {
    return graph as unknown as WorkflowGraphData;
  },

  // ---------- M5 Week 4: Playground ----------

  playgroundRun: (prompt: string, config?: Partial<LLMConfig>) =>
    post<PlaygroundRunResponse>("/v1/playground/run", {
      prompt,
      config: {
        model: config?.model ?? "gpt-4o",
        temperature: config?.temperature ?? 0.7,
        max_tokens: config?.max_tokens ?? 4096,
        system_prompt: config?.system_prompt ?? "",
      },
    }),

  playgroundCompare: (prompt: string, configs: LLMConfig[]) =>
    post<PlaygroundCompareResponse>("/v1/playground/compare", {
      prompt,
      configs,
    }),

  playgroundFreeze: (data: {
    task: string;
    model: string;
    temperature?: number;
    assertions?: Record<string, unknown>[];
    budget?: Record<string, unknown>;
  }) => post<FreezeSpecResponse>("/v1/playground/freeze", data),

  playgroundReproduce: (traceId: string, spanId: string) =>
    post<ReproduceResponse>("/v1/playground/reproduce", {
      trace_id: traceId,
      span_id: spanId,
    }),

  // ---------- 模型配置（自定义 provider / model / key / base_url） ----------

  listModelConfigs: () => request<ModelConfigList>("/v1/models"),

  createModelConfig: (data: {
    name: string;
    provider: ModelProvider;
    model: string;
    api_key?: string;
    base_url?: string;
    degraded_model?: string;
    is_default?: boolean;
    sort_order?: number;
  }) => post<ModelConfigCreateResponse>("/v1/models", data),

  updateModelConfig: (
    id: string,
    data: {
      name?: string;
      provider?: ModelProvider;
      model?: string;
      api_key?: string;
      base_url?: string;
      degraded_model?: string;
      is_default?: boolean;
      is_active?: boolean;
      sort_order?: number;
    },
  ) => put<ModelConfig>(`/v1/models/${encodeURIComponent(id)}`, data),

  deleteModelConfig: (id: string) =>
    del<void>(`/v1/models/${encodeURIComponent(id)}`),
};
