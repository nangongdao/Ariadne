/**
 * 后台批量上报。
 *
 * 与 Python SDK 对等的三条硬约束：
 * 1. 同步路径只做入队，目标 < 1ms
 * 2. 队列满时丢最旧的并计数，不反压业务线程
 * 3. 任何异常都在内部吞掉，永不向业务抛出
 */

import type { SpanPayload } from "./span";

const DEFAULT_QUEUE_SIZE = 10_000;
const DEFAULT_BATCH_SIZE = 100;
const DEFAULT_FLUSH_INTERVAL = 2.0;
const MAX_RETRIES = 3;
const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504]);

export interface ExporterStats {
  submitted: number;
  sent: number;
  dropped_queue_full: number;
  dropped_failed: number;
  http_errors: number;
}

export class SpanExporter {
  endpoint: string;
  apiKey: string;
  batchSize: number;
  flushInterval: number;
  queueSize: number;
  timeout: number;

  stats: ExporterStats = {
    submitted: 0,
    sent: 0,
    dropped_queue_full: 0,
    dropped_failed: 0,
    http_errors: 0,
  };

  private _queue: SpanPayload[] = [];
  private _timer: ReturnType<typeof setInterval> | undefined;
  private _flushing = false;
  private _stopped = false;

  constructor(opts: {
    endpoint: string;
    apiKey: string;
    batchSize?: number;
    flushInterval?: number;
    queueSize?: number;
    timeout?: number;
  }) {
    this.endpoint = opts.endpoint;
    this.apiKey = opts.apiKey;
    this.batchSize = opts.batchSize ?? DEFAULT_BATCH_SIZE;
    this.flushInterval = opts.flushInterval ?? DEFAULT_FLUSH_INTERVAL;
    this.queueSize = opts.queueSize ?? DEFAULT_QUEUE_SIZE;
    this.timeout = opts.timeout ?? 10_000;
  }

  start(): void {
    if (this._timer) return;
    this._timer = setInterval(
      () => void this._flush(),
      this.flushInterval * 1000,
    );
    this._timer.unref?.();
  }

  submit(payload: SpanPayload): void {
    if (this._stopped) return;
    this.stats.submitted++;
    if (this._queue.length >= this.queueSize) {
      this._queue.shift();
      this.stats.dropped_queue_full++;
    }
    this._queue.push(payload);
  }

  async flush(timeoutSeconds = 5.0): Promise<boolean> {
    const deadline = Date.now() + timeoutSeconds * 1000;
    while (this._queue.length > 0 && Date.now() < deadline) {
      await this._flush();
    }
    return this._queue.length === 0;
  }

  shutdown(): void {
    this._stopped = true;
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = undefined;
    }
    void this._flush();
  }

  private async _flush(): Promise<void> {
    if (this._flushing || this._queue.length === 0) return;
    this._flushing = true;
    const batch = this._queue.splice(0, this.batchSize);
    try {
      await this._sendBatch(batch);
      this.stats.sent += batch.length;
    } catch {
      this.stats.dropped_failed += batch.length;
      this.stats.http_errors++;
    } finally {
      this._flushing = false;
    }
  }

  private async _sendBatch(batch: SpanPayload[]): Promise<void> {
    const body = JSON.stringify(batch);
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        const resp = await fetch(this.endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Ariadne-Key": this.apiKey,
          },
          body,
          signal: AbortSignal.timeout(this.timeout),
        });
        if (resp.ok) return;
        if (!RETRYABLE_STATUS.has(resp.status)) return;
      } catch {
        if (attempt === MAX_RETRIES - 1) throw new Error("send failed");
      }
      await new Promise((r) => setTimeout(r, 2 ** attempt * 1000));
    }
  }
}
