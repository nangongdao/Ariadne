/**
 * Ariadne 客户端：SDK 主入口。
 *
 * 与 Python SDK 的 Ariadne 类对等。span() 创建 span，自动串联父子关系。
 */

import { SpanExporter } from "./exporter";
import { Span, currentSpan } from "./span";

const DEFAULT_ENDPOINT = "http://localhost:8000/v1/ingest/spans";

export class Ariadne {
  project: string;
  enabled: boolean;
  private _exporter: SpanExporter | undefined;

  constructor(
    apiKey?: string | undefined,
    opts: {
      project?: string;
      endpoint?: string;
      batchSize?: number;
      flushInterval?: number;
      queueSize?: number;
      enabled?: boolean;
    } = {},
  ) {
    this.project = opts.project ?? "default";
    const key = apiKey ?? process.env.ARIADNE_API_KEY ?? "";
    this.enabled = (opts.enabled ?? true) && !!key;

    if (this.enabled) {
      const endpoint =
        opts.endpoint ?? process.env.ARIADNE_ENDPOINT ?? DEFAULT_ENDPOINT;
      this._exporter = new SpanExporter({
        endpoint,
        apiKey: key,
        batchSize: opts.batchSize,
        flushInterval: opts.flushInterval,
        queueSize: opts.queueSize,
      });
      this._exporter.start();
    }
  }

  span(
    name: string,
    opts: {
      kind?: string;
      operation?: string;
      provider?: string;
      model?: string;
      attributes?: Record<string, unknown>;
      tags?: string[];
    } = {},
  ): Span {
    const parent = currentSpan();
    const span = new Span(name, {
      kind: opts.kind,
      operation: opts.operation,
      provider: opts.provider,
      model: opts.model,
      tags: opts.tags,
      exporter: this._exporter,
      traceId: parent?.traceId,
      parentSpanId: parent?.spanId,
    });
    if (opts.attributes) span.setAttributes(opts.attributes);
    return span;
  }

  flush(timeoutSeconds = 5.0): Promise<boolean> {
    return this._exporter
      ? this._exporter.flush(timeoutSeconds)
      : Promise.resolve(true);
  }

  shutdown(): void {
    this._exporter?.shutdown();
  }

  get stats(): Record<string, number> {
    return this._exporter ? { ...this._exporter.stats } : {};
  }
}

let _globalClient: Ariadne | undefined;

export function init(apiKey?: string, opts?: {
  project?: string;
  endpoint?: string;
}): Ariadne {
  _globalClient = new Ariadne(apiKey, opts);
  return _globalClient;
}

export function getClient(): Ariadne | undefined {
  return _globalClient;
}
