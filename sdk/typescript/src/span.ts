/**
 * Span 对象与上下文传播。
 *
 * 用 Node 的 AsyncLocalStorage 而非全局变量：async 场景下同一事件循环会
 * 交错运行多个异步调用，全局变量会导致父子关系错乱。AsyncLocalStorage
 * 保证每个异步链路有自己的上下文副本，与 Python 的 contextvars 对等。
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";
import { performance } from "node:perf_hooks";

const MAX_PREVIEW = 2048;

const _currentSpan = new AsyncLocalStorage<Span | undefined>();

/** 生成十六进制随机 ID。 */
function hexId(bytes: number): string {
  return randomBytes(bytes).toString("hex");
}

/** 获取当前异步上下文中的活跃 span。 */
export function currentSpan(): Span | undefined {
  return _currentSpan.getStore();
}

/** 获取当前 trace ID（无活跃 span 时返回空串）。 */
export function currentTraceId(): string {
  return _currentSpan.getStore()?.traceId ?? "";
}

export interface UsageData {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
}

/** Span 的 native 格式 payload，与 Python SDK 的 to_payload() 对等。 */
export interface SpanPayload {
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  name: string;
  kind: string;
  operation: string;
  provider: string;
  model: string;
  model_response: string;
  started_at: string;
  duration_ms: number;
  status: string;
  error_type: string;
  usage: UsageData;
  input_preview: string;
  output_preview: string;
  attributes: Record<string, string>;
  tags: string[];
}

export class Span {
  name: string;
  kind: string;
  traceId: string;
  spanId: string;
  parentSpanId: string;
  operation: string;
  provider: string;
  model: string;
  modelResponse: string;

  inputTokens = 0;
  outputTokens = 0;
  cacheReadTokens = 0;
  cacheWriteTokens = 0;
  reasoningTokens = 0;

  inputPreview = "";
  outputPreview = "";
  attributes: Record<string, string> = {};
  tags: string[] = [];

  status = "ok";
  errorType = "";

  private _exporter?: SpanExporter;
  private _startedAt: Date;
  private _startPerf: number;
  private _ended = false;

  constructor(
    name: string,
    opts: {
      kind?: string;
      operation?: string;
      provider?: string;
      model?: string;
      tags?: string[];
      exporter?: SpanExporter;
      traceId?: string;
      spanId?: string;
      parentSpanId?: string;
    } = {},
  ) {
    this.name = name;
    this.kind = opts.kind ?? "internal";
    this.operation = opts.operation ?? "";
    this.provider = opts.provider ?? "";
    this.model = opts.model ?? "";
    this.modelResponse = "";
    this.tags = opts.tags ? [...opts.tags] : [];
    this.traceId = opts.traceId ?? hexId(16);
    this.spanId = opts.spanId ?? hexId(8);
    this.parentSpanId = opts.parentSpanId ?? "";
    this._exporter = opts.exporter;
    this._startedAt = new Date();
    this._startPerf = performance.now();
  }

  setAttributes(values: Record<string, unknown>): this {
    for (const [key, value] of Object.entries(values)) {
      if (value !== undefined && value !== null) {
        this.attributes[key] = String(value).slice(0, 1024);
      }
    }
    return this;
  }

  setUsage(usage: Partial<UsageData>): this {
    this.inputTokens = usage.input_tokens ?? 0;
    this.outputTokens = usage.output_tokens ?? 0;
    this.cacheReadTokens = usage.cache_read_tokens ?? 0;
    this.cacheWriteTokens = usage.cache_write_tokens ?? 0;
    this.reasoningTokens = usage.reasoning_tokens ?? 0;
    return this;
  }

  setInput(text: string): this {
    this.inputPreview = String(text).slice(0, MAX_PREVIEW);
    return this;
  }

  setOutput(text: string): this {
    this.outputPreview = String(text).slice(0, MAX_PREVIEW);
    return this;
  }

  setModel(model: string, opts: { responseModel?: string; provider?: string } = {}): this {
    this.model = model;
    this.modelResponse = opts.responseModel ?? model;
    if (opts.provider) this.provider = opts.provider;
    return this;
  }

  recordError(error: Error): this {
    this.status = "error";
    this.errorType = error.name;
    return this;
  }

  /** 进入 span 上下文。后续创建的子 span 自动继承 trace_id + parent_span_id。 */
  enter(): this {
    const parent = _currentSpan.getStore();
    if (parent) {
      this.traceId = parent.traceId;
      if (!this.parentSpanId) this.parentSpanId = parent.spanId;
    }
    _currentSpan.enterWith(this);
    return this;
  }

  /** 结束 span 并提交到 exporter。幂等：重复调用不会重复上报。 */
  end(): void {
    if (this._ended) return;
    this._ended = true;
    if (this._exporter) {
      this._exporter.submit(this.toPayload());
    }
  }

  /** span 在上下文管理器中的自动结束模式。 */
  [Symbol.dispose](): void {
    this.end();
  }

  get durationMs(): number {
    return Math.round(performance.now() - this._startPerf);
  }

  toPayload(): SpanPayload {
    return {
      trace_id: this.traceId,
      span_id: this.spanId,
      parent_span_id: this.parentSpanId,
      name: this.name,
      kind: this.kind,
      operation: this.operation,
      provider: this.provider,
      model: this.model,
      model_response: this.modelResponse,
      started_at: this._startedAt.toISOString(),
      duration_ms: this.durationMs,
      status: this.status,
      error_type: this.errorType,
      usage: {
        input_tokens: this.inputTokens,
        output_tokens: this.outputTokens,
        cache_read_tokens: this.cacheReadTokens,
        cache_write_tokens: this.cacheWriteTokens,
        reasoning_tokens: this.reasoningTokens,
      },
      input_preview: this.inputPreview,
      output_preview: this.outputPreview,
      attributes: this.attributes,
      tags: this.tags,
    };
  }
}

// 前向引用类型
export type { SpanExporter } from "./exporter";
import type { SpanExporter } from "./exporter";
